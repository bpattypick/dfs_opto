"""One-off: rank Showdown candidate lineups by projection against duplication.

Not part of the task queue. T7 is the real duplication model; this is the cheap
version of the same idea, built to look at a single slate before lock.

What it does: run the optimizer repeatedly over jittered projections to get both
a candidate pool (the lineups a rational field would build) and the optimal-rate
ownership estimate, generate a synthetic field from those rates, then count how
often each candidate appears in that field verbatim.

Why duplication matters: in a small Showdown pool the "optimal" build gets
entered by many people, and a duplicated first place splits the prize. A lineup
one point worse but half as duplicated can be worth more.

    python scripts/showdown_dup_report.py data/raw/salaries/<export>.csv

Caveats, in order of how much they should temper the output:
  - The field is generated from optimal-rate ownership, so it is a field of
    rational builds. Real contests contain casual lineups that never appear
    here, which biases these duplication rates HIGH for chalky lineups. They
    are uncalibrated until real standings exist (H2/T3).
  - Projections are DK's AvgPointsPerGame. In Week 1 that is last season's
    average, which is a weak input, and everything downstream inherits it.
  - There is no score simulator or payout table yet, so this ranks on projected
    points and duplication. It is NOT expected ROI (that is T6).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import showdown  # noqa: E402
from src.field import generate_field  # noqa: E402
from src.ownership import optimal_lineup  # noqa: E402
from src.showdown import Lineup  # noqa: E402

UNAVAILABLE = ("OUT", "IR")


def load_pool(path: str) -> pd.DataFrame:
    """Build a player pool from a DK Showdown export.

    Reads the FLEX row for base salary (the CPT row is the same player at 1.5x)
    and drops players DK has flagged unavailable.
    """
    raw = pd.read_csv(path)
    flex = raw[raw["Roster Position"].astype(str).str.upper() == "FLEX"].copy()
    if flex.empty:
        raise SystemExit(f"{path}: no FLEX rows — is this a Showdown export?")

    status = flex["Status"].fillna("").astype(str).str.upper()
    pool = pd.DataFrame({
        "player_id": flex["ID"].astype(str),
        "name": flex["Name"].astype(str).str.strip(),
        "team": flex["TeamAbbrev"].astype(str),
        "salary": flex["Salary"].astype(float),
        "projection": flex["AvgPointsPerGame"].astype(float).clip(lower=0.0),
    })
    dropped = pool[status.isin(UNAVAILABLE)]
    if len(dropped):
        print(f"excluding {len(dropped)} OUT/IR players: "
              f"{', '.join(sorted(dropped['name'])[:6])}"
              f"{' ...' if len(dropped) > 6 else ''}\n")
    return pool[~status.isin(UNAVAILABLE)].reset_index(drop=True)


def candidates_and_ownership(pool, runs, jitter, seed):
    """Optimizer runs give the candidate builds and the ownership rates at once."""
    rng = np.random.default_rng(seed)
    base = pool["projection"].to_numpy(dtype=float)
    ids = pool["player_id"].tolist()
    cpt, flex = Counter(), Counter()
    seen: Counter = Counter()

    for _ in range(runs):
        noise = rng.normal(1.0, jitter, size=len(base))
        captain, others = optimal_lineup(pool, np.clip(base * noise, 0.0, None))
        lineup = Lineup(captain, tuple(others))
        seen[lineup.key()] += 1
        cpt[captain] += 1
        for pid in others:
            flex[pid] += 1

    ownership = pd.DataFrame({
        "player_id": ids,
        "cpt_pct": [cpt[i] / runs for i in ids],
        "flex_pct": [flex[i] / runs for i in ids],
    })
    ownership["total_pct"] = ownership["cpt_pct"] + ownership["flex_pct"]
    return seen, ownership


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("export", help="DK Showdown salary CSV")
    p.add_argument("--runs", type=int, default=500, help="optimizer runs")
    p.add_argument("--field", type=int, default=5000, help="synthetic field size")
    p.add_argument("--jitter", type=float, default=0.15)
    p.add_argument("--top", type=int, default=12, help="candidates to display")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)

    pool = load_pool(args.export)
    name = dict(zip(pool["player_id"], pool["name"]))
    salary = dict(zip(pool["player_id"], pool["salary"].astype(float)))
    points = dict(zip(pool["player_id"], pool["projection"].astype(float)))
    print(f"pool: {len(pool)} playable players, teams "
          f"{sorted(pool['team'].unique())}\n")

    built, ownership = candidates_and_ownership(pool, args.runs, args.jitter, args.seed)
    print(f"{args.runs} optimizer runs produced {len(built)} distinct builds")

    field = generate_field(pool[["player_id", "team", "salary"]], ownership,
                           size=args.field, seed=args.seed + 1)
    field_counts = Counter(l.key() for l in field)
    print(f"field of {args.field}: {len(field_counts)} distinct lineups, "
          f"most common appears {field_counts.most_common(1)[0][1]}x\n")

    rows = []
    for key, built_n in built.items():
        captain, flex_set = key
        lineup = Lineup(captain, tuple(sorted(flex_set)))
        dup_n = field_counts.get(key, 0)
        rows.append({
            "lineup": lineup,
            "proj": showdown.lineup_points(lineup, points),
            "salary": showdown.lineup_salary(lineup, salary),
            "dup_pct": dup_n / args.field * 100,
            "built_pct": built_n / args.runs * 100,
        })
    table = pd.DataFrame(rows).sort_values("proj", ascending=False).reset_index(drop=True)

    def show(frame, title):
        print(title)
        print("-" * len(title))
        for _, r in frame.iterrows():
            lu = r["lineup"]
            print(f"  {r['proj']:6.1f} pts  ${r['salary']:>6,.0f}  "
                  f"dup {r['dup_pct']:5.2f}%   CPT {name[lu.captain]}")
            print(f"        {' / '.join(name[p] for p in lu.flex)}")
        print()

    show(table.head(args.top), f"Top {args.top} candidates by projected points")

    # The trade the roadmap is after: near-optimal points, far less duplicated.
    best = table["proj"].max()
    near = table[table["proj"] >= best * 0.97]
    leverage = near.sort_values("dup_pct").head(5)
    show(leverage, "Within 3% of top projection, least duplicated")

    chalk = table.nlargest(3, "dup_pct")
    print("Most duplicated builds (the ones to differentiate from)")
    print("-" * 54)
    for _, r in chalk.iterrows():
        print(f"  dup {r['dup_pct']:5.2f}%  {r['proj']:6.1f} pts  "
              f"CPT {name[r['lineup'].captain]}")
    print("\nDuplication rates are uncalibrated (no real standings yet) and biased")
    print("high for chalk. This is projection vs duplication, NOT expected ROI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
