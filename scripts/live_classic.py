"""Generate DK Classic candidate lineups from the validated pipeline — with
loud limits on what this format has NOT been validated on yet.

Reuses the exact pool-building chain scripts/live_showdown.py uses
(ingest -> pool.from_export -> src.resolve -> src.liveproj v4 -> committed
overrides, now shared as src.livepool): that layer is per-player and
per-position, not Showdown-shaped, and transfers to Classic as-is
(docs/decisions.md H10). Everything downstream of the projection does NOT
transfer and has not been built as of 2026-09-20:

  - No Classic ownership/field model exists (T28 in TASKS.md). This script
    computes and prints NO ownership estimate, NO simulated field, and NO
    duplication-adjusted ROI. Anything of that shape would be fabricated.
  - No real Classic contest standings are archived yet to calibrate against
    (T29, blocked on H11). Nothing here has been checked against a real
    result the way Showdown's chalk cluster and marginals were.

So the output is projection + measured-correlation ONLY: a shortlist of
optimizer-built candidate lineups (jittered reruns, same idea as Showdown's
chalk cluster, but with no claim of matching real field concentration),
ranked by each one's SIMULATED SCORE DISTRIBUTION from src.scoremodel (T13,
extended T27 for cross-game independence across the slate's many games) —
mean / p10 / p25 / p50 / p75 / p90. That number is real and honestly
computed. It is NOT an ROI ranking, NOT ownership-aware, and NOT validated
against a single real Classic slate result. Rank order here means "highest
simulated floor/ceiling among what the optimizer proposes" — not "most +EV
against the field", which this pipeline cannot compute for Classic yet.

    python scripts/live_classic.py data/raw/salaries/<export>.csv

Season and week come from the export's filename, same convention as
Showdown's, and the first step is always an ingest of that season (T21).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import classic  # noqa: E402
from src import db  # noqa: E402
from src.classic_optimizer import OptimizerError, optimal_lineup  # noqa: E402
from src.livepool import build_pool, refresh_history  # noqa: E402
from src.scoremodel import CorrelatedScores, load_marginals  # noqa: E402
from src.ingest.dk_salaries import parse_slate_filename  # noqa: E402

QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)


def team_game_map(season: int, week: int) -> dict[str, str]:
    """team -> game_id for every game in this week, straight from ``games``."""
    with db.session() as conn:
        rows = conn.execute(
            "SELECT game_id, home_team, away_team FROM games WHERE season = ? AND week = ?",
            (season, week),
        ).fetchall()
    m: dict[str, str] = {}
    for game_id, home, away in rows:
        m[home] = game_id
        m[away] = game_id
    return m


def candidate_builds(pool: pd.DataFrame, runs: int, jitter: float, seed: int) -> Counter:
    rng = np.random.default_rng(seed)
    base = pool["projection"].to_numpy(dtype=float)
    builds: Counter = Counter()
    errors = 0
    for _ in range(runs):
        j = np.clip(base * rng.normal(1, jitter, len(base)), 0, None)
        try:
            lu = optimal_lineup(pool, j)
        except OptimizerError:
            errors += 1
            continue
        builds[lu] += 1
    if errors:
        print(f"({errors}/{runs} jittered runs could not build a legal lineup — skipped)")
    return builds


def rank_by_score_distribution(
    lineups: list[classic.Lineup],
    players: list[str],
    draw_scores: CorrelatedScores,
    trials: int,
    seed: int | None,
) -> pd.DataFrame:
    """Each candidate's simulated mean/quantiles. No captain multiplier."""
    position = {pid: i for i, pid in enumerate(players)}
    try:
        idx = np.array([[position[p] for p in lu.players] for lu in lineups], dtype=int)
    except KeyError as exc:
        raise OptimizerError(f"lineup references player {exc} not in the pool") from exc
    rng = np.random.default_rng(seed)
    scores = draw_scores(rng, trials)            # (trials, n_players)
    totals = scores[:, idx].sum(axis=2)           # (trials, n_lineups)
    rows = {"lineup": lineups, "mean": totals.mean(axis=0)}
    for q in QUANTILES:
        rows[f"p{int(round(q * 100))}"] = np.quantile(totals, q, axis=0)
    return pd.DataFrame(rows)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("export")
    p.add_argument("--season", type=int, default=None)
    p.add_argument("--week", type=int, default=None)
    p.add_argument("--no-ingest", action="store_true")
    p.add_argument("--runs", type=int, default=200,
                    help="jittered optimizer reruns (each is an ILP solve over a much "
                         "larger pool than Showdown — kept lower by default for runtime)")
    p.add_argument("--jitter", type=float, default=0.30,
                    help="relative sd on projections per rerun; UNCALIBRATED for Classic "
                         "(Showdown's 0.5 was fit to real Showdown ownership, which does "
                         "not exist for this format yet — T28)")
    p.add_argument("--trials", type=int, default=4000, help="score-distribution sim trials")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--rank-by", choices=["mean", "p25", "p50", "p75", "p90"], default="p25",
                    help="which column to sort candidates by (default: floor, p25)")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)

    print("=" * 78)
    print("DK CLASSIC — projection + correlation only. NO ownership, field, or")
    print("duplication model exists for this format yet (T28/T29 in TASKS.md).")
    print("Ranking below is simulated score floor/ceiling among optimizer")
    print("candidates, NOT expected ROI against a field. Read docs/decisions.md")
    print("H10 before trusting this the way the Showdown output is trusted.")
    print("=" * 78 + "\n")

    meta = parse_slate_filename(args.export)
    season = args.season or meta["season"]
    week = args.week or meta["week"]
    if args.no_ingest:
        print("--no-ingest: using whatever history is already in the database")
    else:
        refresh_history(season)
    print(f"slate {meta['slate_id']}: projections use history strictly before "
          f"{season} week {week}\n")

    pool = build_pool(args.export, season, week)
    name = dict(zip(pool.player_id, pool.name))
    src = dict(zip(pool.player_id, pool.proj_source))
    position = dict(zip(pool.player_id.astype(str), pool.position.astype(str)))
    salary = dict(zip(pool.player_id.astype(str), pool.salary.astype(float)))

    games = team_game_map(season, week)
    missing_team = sorted(set(pool["team"]) - set(games))
    if missing_team:
        raise SystemExit(f"no game found this week for team(s): {missing_team} "
                          f"— check the ingest covers this week's schedule")
    pool = pool.copy()
    pool["game_id"] = pool["team"].map(games)
    n_games = pool["game_id"].nunique()
    print(f"pool spans {n_games} game(s): "
          f"{', '.join(sorted(pool['team'].unique()))}\n")

    builds = candidate_builds(pool, args.runs, args.jitter, args.seed)
    if not builds:
        raise SystemExit("no legal lineup could be built from this pool at any jitter draw")
    print(f"{args.runs} jittered optimizer runs -> {len(builds)} distinct candidates")

    base_proj = pool["projection"].to_numpy(dtype=float)
    try:
        anchor = optimal_lineup(pool, base_proj)
        builds[anchor] += 0   # the un-jittered optimum is always on the board
    except OptimizerError as exc:
        print(f"(un-jittered optimum could not be built: {exc})")

    marginals = load_marginals()
    scores = CorrelatedScores(
        pool[["player_id", "position", "team", "projection", "game_id"]],
        marginals=marginals,
    )
    print(f"score marginals: {scores.kind}"
          + (f" (fitted on {marginals.fitted_on})" if marginals is not None else
             " -- data/score_marginals.json missing; floors read ~10 percentile points too kind (T24)"))

    ranked = rank_by_score_distribution(
        list(builds), scores.players, scores, trials=args.trials, seed=args.seed,
    )
    ranked = ranked.sort_values(args.rank_by, ascending=False).reset_index(drop=True)

    print(f"\ntop {min(args.top, len(ranked))} of {len(ranked)} distinct candidates, "
          f"ranked by simulated {args.rank_by} (no ROI/ownership claim)\n")
    print(f"{'rank':<5}{'proj':>7}{'mean':>7}{'p10':>6}{'p25':>6}{'p50':>6}{'p75':>6}{'p90':>6}  lineup")
    print("-" * 100)
    proj_lookup = dict(zip(pool.player_id, pool.projection))
    for i, row in ranked.head(args.top).iterrows():
        lu = row["lineup"]
        proj = sum(proj_lookup[p] for p in lu.players)
        flags = " ".join(f"[{src[p]}]" for p in lu.players if src[p] != "v4")
        print(f"{i+1:<5}{proj:>7.1f}{row['mean']:>7.1f}{row['p10']:>6.1f}{row['p25']:>6.1f}"
              f"{row['p50']:>6.1f}{row['p75']:>6.1f}{row['p90']:>6.1f}  QB {name[lu.qb]}")
        print(f"      RB {name[lu.rb[0]]} / {name[lu.rb[1]]}   "
              f"WR {' / '.join(name[x] for x in lu.wr)}")
        print(f"      TE {name[lu.te]}   FLEX {name[lu.flex]} ({position[lu.flex]})   "
              f"DST {name[lu.dst]}"
              + (f"   {flags}" if flags else ""))
        print(f"      salary {classic.lineup_salary(lu, salary):,.0f} / {classic.SALARY_CAP:,}")
    print("\n[avg_points] = AvgPointsPerGame, not v4 (unresolved to history, or a kicker).")
    print("[override] = committed owner override in data/projection_overrides.csv, wins over v4.")
    print("\nReminder: this is projection+correlation only. No ownership, field, or")
    print("duplication-adjusted ROI is computed for Classic yet (T28/T29). Use judgment")
    print("before entering — this has not earned the confidence the Showdown output has.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
