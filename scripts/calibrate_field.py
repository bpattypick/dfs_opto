"""Calibrate the field generator's chalk cluster against real standings (T15).

    python scripts/calibrate_field.py [--fit ne-sea] [--check sf-lar]

For each archived slate with both a DK export and a contest standings file:
build the pool the way the live script does, read the real field, and compare
the generated field's *shape* (distinct share, most-entered build share,
top-5 share) and its ownership error with the real one, over a grid of
cluster jitter and cluster share. Two ownership inputs are run: the real
ownership itself (isolates the sampler: given correct marginals, does the
cluster reproduce the joint structure?) and the live estimate (what the
pipeline actually feeds it). The share that fits the first slate is then
reported on the second, untouched.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import db, standings  # noqa: E402
from src.field import chalk_builds, field_shape, generate_field, realized_ownership  # noqa: E402
from src.liveproj import project_live_pool  # noqa: E402
from src.ownership import estimate_ownership  # noqa: E402
from src.pool import from_export  # noqa: E402
from src.resolve import resolve_pool  # noqa: E402

SLATES = {
    "ne-sea": ("data/raw/salaries/2026-w01_dk_showdown-ne-sea.csv",
               "data/raw/standings/2026-w01_dk_showdown-ne-sea_contest195488502.csv"),
    "sf-lar": ("data/raw/salaries/2026-w01_dk_showdown-sf-lar.csv",
               "data/raw/standings/2026-w01_dk_showdown-sf-lar_contest195523059.csv"),
}
SALARY_FLOOR = 1200      # T17: the optimizer's pool, not the field's


def load_slate(key: str, season: int, week: int):
    export, standing = SLATES[key]
    pool = from_export(export)
    with db.session() as conn:
        pool = project_live_pool(conn, resolve_pool(conn, pool), season=season, week=week)
    entries = standings.read_standings(standing)
    real, unmatched = standings.to_lineups(entries, pool)
    return pool, real, unmatched


def ownership_mae(field, pool, real_own) -> float:
    got = realized_ownership(field, pool).set_index("player_id")["total_pct"]
    want = real_own.set_index("player_id")["total_pct"]
    return float((got.reindex(want.index).fillna(0) - want).abs().mean() * 100)


def run_grid(pool, real, jitters, shares, size, seed=0):
    real_own = realized_ownership(real, pool)
    est_own = estimate_ownership(pool[pool.salary >= SALARY_FLOOR], n=300, jitter=0.5, seed=seed)
    # the field is drawn over the full pool; players the estimate never saw get 0
    est_own = est_own.set_index("player_id").reindex(pool.player_id).fillna(0).reset_index()
    floored = pool[pool.salary >= SALARY_FLOOR].reset_index(drop=True)
    rows = []
    for cj in jitters:
        chalk = chalk_builds(floored, runs=300, jitter=cj, seed=seed)
        top_chalk = max(chalk.values()) / sum(chalk.values())
        for share in shares:
            for label, own in (("real", real_own), ("estimated", est_own)):
                field = generate_field(pool[["player_id", "team", "salary"]], own[["player_id", "cpt_pct", "flex_pct"]],
                                       size=size, seed=seed + 1, chalk=chalk if share else None, chalk_share=share)
                shape = field_shape(field)
                rows.append({"chalk_jitter": cj, "share": share, "ownership": label,
                             "cluster_top": top_chalk, "distinct%": shape["distinct_share"] * 100,
                             "top_build%": shape["top_build_share"] * 100, "top5%": shape["top5_share"] * 100,
                             "own_MAE": ownership_mae(field, pool, real_own)})
    return pd.DataFrame(rows)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--fit", default="ne-sea", choices=sorted(SLATES))
    p.add_argument("--check", default="sf-lar", choices=sorted(SLATES))
    p.add_argument("--season", type=int, default=2026)
    p.add_argument("--week", type=int, default=2)
    p.add_argument("--jitters", nargs="*", type=float, default=[0.15, 0.30])
    p.add_argument("--shares", nargs="*", type=float, default=[0.0, 0.10, 0.20, 0.30, 0.40])
    args = p.parse_args(argv)
    pd.set_option("display.width", 160)

    results = {}
    for key in (args.fit, args.check):
        pool, real, unmatched = load_slate(key, args.season, args.week)
        shape = field_shape(real)
        print(f"\n=== {key}: {shape['entries']:,} real entries "
              f"({len(unmatched)} unmatched names dropped: {unmatched[:6]}) ===")
        print(f"real field: distinct {shape['distinct_share']:.1%}  top build {shape['top_build_share']:.2%}  "
              f"top-5 {shape['top5_share']:.2%}")
        grid = run_grid(pool, real, args.jitters, args.shares, size=shape["entries"])
        grid["err"] = (grid["distinct%"] - shape["distinct_share"] * 100).abs() / 10 \
            + (grid["top_build%"] - shape["top_build_share"] * 100).abs()
        results[key] = (shape, grid)
        print(grid.round(2).to_string(index=False))

    shape, grid = results[args.fit]
    best = grid[grid.ownership == "real"].sort_values("err").iloc[0]
    print(f"\nbest on {args.fit} (real ownership in): chalk_jitter {best.chalk_jitter}  share {best.share}  "
          f"-> distinct {best['distinct%']:.1f}%  top build {best['top_build%']:.2f}%  (real {shape['distinct_share']:.1%} / {shape['top_build_share']:.2%})")
    cshape, cgrid = results[args.check]
    at = cgrid[(cgrid.chalk_jitter == best.chalk_jitter) & (cgrid.share == best.share)]
    print(f"same setting on {args.check} (real {cshape['distinct_share']:.1%} / {cshape['top_build_share']:.2%}):")
    print(at.round(2).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
