"""Fit the score model's empirical marginals (T24) and print the calibration.

    python scripts/fit_score_marginals.py [--seasons 2020 2025] [--holdout 2]

Runs the live projection model (v4, RoleAware) through the roster-pool
harness for the seasons given, fits actual/projection curves on all but the
last ``holdout`` seasons, prints held-out coverage for the lognormal and the
empirical marginal side by side (all evaluated rows, and the projected
top-12 per game), then refits on every season and writes
``data/score_marginals.json`` -- the file ``src.scoremodel.load_marginals``
serves to the live path. Re-run it when the projection model changes: the
residual shape belongs to the model that made the projections.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import db  # noqa: E402
from src.backtest import harness  # noqa: E402
from src.projection import RoleAware  # noqa: E402
from src.scoremodel import (DEFAULT_MARGINALS_PATH, EmpiricalMarginals,  # noqa: E402
                            coverage_table)


def top12(rows: pd.DataFrame) -> pd.DataFrame:
    rank = rows.groupby(["season", "week", "game_id"])["projection"].rank(ascending=False, method="first")
    return rows[rank <= 12]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--seasons", nargs=2, type=int, default=(2020, 2025))
    p.add_argument("--holdout", type=int, default=2)
    p.add_argument("--bands", type=int, default=5)
    p.add_argument("--out", default=str(DEFAULT_MARGINALS_PATH))
    p.add_argument("--db", default=None)
    args = p.parse_args(argv)

    seasons = list(range(args.seasons[0], args.seasons[1] + 1))
    with db.session(args.db) as conn:
        res = harness.run(conn, RoleAware(), seasons, pool="roster", min_games=0, persist=False)
    rows = res.rows
    fit_seasons = seasons[:-args.holdout] if args.holdout else seasons
    test_seasons = seasons[-args.holdout:] if args.holdout else []
    print(f"{len(rows):,} evaluated player-weeks; fit on {fit_seasons}, hold out {test_seasons}")

    pd.set_option("display.width", 160)
    if test_seasons:
        fitted = EmpiricalMarginals.fit(rows[rows.season.isin(fit_seasons)], bands=args.bands,
                                        fitted_on=f"{fit_seasons[0]}-{fit_seasons[-1]}")
        held = rows[rows.season.isin(test_seasons)]
        for label, frame in (("ALL evaluated", held), ("projected TOP-12 per game", top12(held))):
            print(f"\n=== HELD-OUT {test_seasons}: {label} (share of actuals <= model quantile; ideal = quantile) ===")
            print("lognormal:");  print(coverage_table(frame, None).round(3).to_string())
            print("empirical:");  print(coverage_table(frame, fitted).round(3).to_string())

    final = EmpiricalMarginals.fit(rows, bands=args.bands, fitted_on=f"{seasons[0]}-{seasons[-1]}")
    path = final.save(args.out)
    print(f"\nwrote {path}")
    for pos in ["ALL"] + final.positions():
        print(f"  {pos:<4} bands at proj {[round(c, 1) for c in final.centers[pos]]}  "
              f"zero share {[round(z, 3) for z in final.zero_share[pos]]}  n {final.counts[pos].tolist()}")
    print("\nin-sample TOP-12 coverage of the shipped fit:")
    print(coverage_table(top12(rows), final).round(3).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
