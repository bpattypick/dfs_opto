"""python -m src.backtest --model shrunk_vegas --seasons 2020 2025"""

from __future__ import annotations

import argparse
import sys

from src import db
from src.backtest import harness
from src.projection import MODELS


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Replay historical weeks and score a projection model.")
    p.add_argument("--model", action="append", choices=sorted(MODELS),
                   help="repeatable; every model listed is run and compared")
    p.add_argument("--seasons", nargs=2, type=int, metavar=("FIRST", "LAST"),
                   default=(2020, 2025))
    p.add_argument("--weeks", nargs="*", type=int, help="restrict to these week numbers")
    p.add_argument("--min-games", type=int, default=3)
    p.add_argument("--window", type=int, default=17)
    p.add_argument("--k", type=float, default=4.0, help="shrinkage strength (shrunk_vegas)")
    p.add_argument("--no-vegas", action="store_true")
    p.add_argument("--db", default=None)
    p.add_argument("--no-persist", action="store_true")
    args = p.parse_args(argv)

    names = args.model or sorted(MODELS)
    seasons = list(range(args.seasons[0], args.seasons[1] + 1))
    results = []
    with db.session(args.db) as conn:
        for name in names:
            cls = MODELS[name]
            kwargs = {"window": args.window}
            if name == "shrunk_vegas":
                kwargs.update(k=args.k, vegas=not args.no_vegas)
            model = cls(**kwargs)
            try:
                result = harness.run(conn, model, seasons, min_games=args.min_games,
                                     weeks=args.weeks, persist=not args.no_persist)
            except harness.BacktestError as exc:
                print(f"!! {exc}", file=sys.stderr)
                return 1
            print(result.summary()); print()
            results.append(result)
    if len(results) > 1:
        print(harness.compare(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
