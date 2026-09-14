"""Generate Showdown candidate lineups from the validated pipeline.

The first script to wire the whole chain together rather than one piece at a
time: pool.from_export (T11/T12) -> src.resolve (T14 wedge) ->
src.liveproj (v3, T18) -> src.ownership (T15, jitter calibrated to 0.5) ->
src.field -> src.scoremodel (T13) -> src.duplication.

Output ranks candidates by dup-adjusted ROI against a SYNTHETIC payout table —
there is no real one until a specific contest is chosen, so the ROI numbers
are for ranking candidates against each other, not a prediction of real
winnings. Says so in the output rather than only in this docstring.

    python scripts/live_showdown.py data/raw/salaries/<export>.csv

Everything below is genuinely first-run-on-real-data:
  - v3 projections have never driven a lineup before (only backtested)
  - the correlated score model has never driven a lineup before (only
    validated against historical stack covariance)
  - the field/duplication numbers still carry T15's ~6x understatement
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import config as config_mod  # noqa: E402
from src import db  # noqa: E402
from src.duplication import compare_duplication, most_duplicated  # noqa: E402
from src.contest import PayoutTable  # noqa: E402
from src.field import generate_field  # noqa: E402
from src.liveproj import project_live_pool  # noqa: E402
from src.ownership import estimate_ownership, optimal_lineup  # noqa: E402
from src.pool import from_export, load_overrides, apply_overrides, questionable  # noqa: E402
from src.resolve import resolve_pool  # noqa: E402
from src.scoremodel import CorrelatedScores  # noqa: E402
from src.showdown import Lineup, lineup_points, lineup_salary  # noqa: E402
from src.ingest.dk_salaries import parse_dk_export, parse_slate_filename  # noqa: E402

SALARY_FLOOR = 1200      # T17: excludes the DK "will not play" pricing tier


def build_pool(export_path: str, season: int, week: int) -> pd.DataFrame:
    parsed = parse_dk_export(export_path)
    unavailable = len(parsed) - len(
        parsed[~parsed["status"].fillna("").str.upper().isin(("OUT", "IR", "IR-R", "SUSP", "NA"))]
    )
    if unavailable:
        print(f"excluding {unavailable} OUT/IR players")
    flagged = questionable(parsed)
    if len(flagged):
        print(f"questionable (kept): {', '.join(sorted(flagged['dk_name']))}")

    pool = from_export(export_path)
    slate_id = parse_slate_filename(export_path)["slate_id"]
    overrides_path = config_mod.load().path("projection_overrides")
    try:
        overrides = load_overrides(overrides_path, slate_id)
        if len(overrides):
            print(f"applying {len(overrides)} committed projection override(s)")
        pool = apply_overrides(pool, overrides)
    except FileNotFoundError:
        pass

    with db.session() as conn:
        resolved = resolve_pool(conn, pool)
        resolved_n = resolved["gsis_id"].notna().sum()
        projected = project_live_pool(conn, resolved, season=season, week=week)
    v3_n = (projected["proj_source"] == "v3").sum()
    print(f"resolved {resolved_n}/{len(projected)} to history; "
          f"{v3_n} projected with v3, {len(projected) - v3_n} on AvgPointsPerGame\n")

    floored = projected[projected["salary"] >= SALARY_FLOOR].reset_index(drop=True)
    if len(floored) < len(projected):
        print(f"salary floor ${SALARY_FLOOR}: excluded "
              f"{len(projected) - len(floored)} minimum-priced players (T17)\n")
    return floored


def candidate_builds(pool: pd.DataFrame, runs: int, jitter: float, seed: int):
    rng = np.random.default_rng(seed)
    base = pool["projection"].to_numpy(dtype=float)
    builds: Counter = Counter()
    for _ in range(runs):
        j = np.clip(base * rng.normal(1, jitter, len(base)), 0, None)
        c, f = optimal_lineup(pool, j)
        builds[Lineup(c, tuple(sorted(f)))] += 1
    return builds


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("export")
    p.add_argument("--season", type=int, default=2026)
    p.add_argument("--week", type=int, default=1)
    p.add_argument("--runs", type=int, default=500)
    p.add_argument("--field-size", type=int, default=5000)
    p.add_argument("--jitter", type=float, default=0.5, help="T15-calibrated default")
    p.add_argument("--trials", type=int, default=2000, help="contest sim trials per candidate")
    p.add_argument("--top", type=int, default=8)
    p.add_argument("--compare-pool", type=int, default=40,
                   help="candidates (by raw projection) to run full ROI comparison on")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)

    pool = build_pool(args.export, args.season, args.week)
    name = dict(zip(pool.player_id, pool.name))
    src = dict(zip(pool.player_id, pool.proj_source))

    own = estimate_ownership(pool, n=args.runs, jitter=args.jitter, seed=args.seed)
    builds = candidate_builds(pool, args.runs, args.jitter, args.seed)
    field = generate_field(pool[["player_id", "team", "salary"]], own,
                           size=args.field_size, seed=args.seed + 1)
    distinct = len(set(l.key() for l in field))
    print(f"{args.runs} optimizer runs -> {len(builds)} distinct candidates")
    print(f"field of {args.field_size}: {distinct} distinct "
          f"({distinct/args.field_size:.1%}), most-entered "
          f"{most_duplicated(field, top=1).iloc[0]['dup_rate']*100:.2f}%\n")

    # compare_duplication runs two full contest simulations per candidate, so
    # comparing all distinct builds (hundreds, from --runs optimizer calls) is
    # wasted work: only the highest-projected ones are ever going to rank near
    # the top on dup-adjusted ROI either. --compare-pool trims to a shortlist
    # first, by raw projection, then compares just that shortlist properly.
    pts = dict(zip(pool.player_id, pool.projection))
    ranked = sorted(builds, key=lambda l: lineup_points(l, pts), reverse=True)
    candidates = ranked[:args.compare_pool]
    print(f"comparing the top {len(candidates)} of {len(builds)} distinct candidates "
          f"by projection (--compare-pool to widen)\n")
    scores = CorrelatedScores(pool[["player_id", "position", "team", "projection"]])
    # A synthetic, clearly-labelled payout table — ranking tool only.
    payouts = PayoutTable.from_tiers([
        (1, 1, 2000.0), (2, 2, 1000.0), (3, 3, 600.0), (4, 5, 300.0), (6, 10, 150.0),
        (11, 25, 60.0), (26, 60, 25.0), (61, 150, 12.0), (151, 350, 8.0), (351, 600, 5.0),
    ])
    fee = payouts.total_prizes / (args.field_size * (1 - 0.10))  # ~10% synthetic rake

    table = compare_duplication(
        candidates, field, pool.player_id.tolist(), scores, payouts,
        entry_fee=fee, trials=args.trials, seed=args.seed,
        labels=[f"c{i}" for i in range(len(candidates))],
    )

    print(f"SYNTHETIC contest: {args.field_size:,} entries, ~${fee:.2f} fee — "
          f"ranking only, not a real payout table\n")
    print(f"{'rank':<5}{'proj':>7}{'dup%':>7}{'dupROI':>9}{'rawROI':>9}  lineup")
    print("-" * 90)
    for i, row in table.head(args.top).iterrows():
        lu = row["lineup"]
        proj = lineup_points(lu, dict(zip(pool.player_id, pool.projection)))
        flags = " ".join(f"[{src[p]}]" for p in lu.players if src[p] == "avg_points")
        print(f"{i+1:<5}{proj:>7.1f}{row['dup_rate']*100:>6.2f}%{row['dup_roi']:>+9.1%}"
              f"{row['raw_roi']:>+9.1%}  CPT {name[lu.captain]}")
        print(f"      {' / '.join(name[x] for x in lu.flex)}"
              + (f"   {flags}" if flags else ""))
    print("\n[avg_points] marks a player whose slot in that lineup used "
          "AvgPointsPerGame, not v3 (unresolved to history).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
