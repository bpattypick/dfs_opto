# DFS Pipeline

NFL DraftKings pipeline: ingest, canonical ID matching, DK scoring, and
(eventually) a leakage-free backtest harness.

Full spec: [`docs/phase1-spec.md`](docs/phase1-spec.md).
Data-source findings: [`docs/data-sources.md`](docs/data-sources.md).

## Status

| # | Milestone | State |
| --- | --- | --- |
| 1 | Repo, venv, config, schema | ✅ done |
| 2 | nfl_data_py ingest + DK scoring + unit tests | ✅ done |
| 3 | Crosswalk + manual override loop | ✅ done |
| 4 | Projections + salaries adapters | 🟡 DK salaries done; vendor projections not started |
| 5 | Naive optimizer | ⬜ not started |
| 6 | Backtest harness + metrics notebook | ⬜ not started |

**Currently loaded:** 2019–2025 regular season — 44,036 player-weeks, 3,742
team-defense weeks, 1,960 games with closing spreads/totals. 112 tests passing.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

```bash
# Build an empty database (Milestone 1)
python -m src.db

# Ingest nflverse stats for the configured season range (Milestone 2)
python -m src.ingest.nfl_stats                 # all of seasons.start..seasons.end
python -m src.ingest.nfl_stats --season 2025   # one season
python -m src.ingest.nfl_stats --refresh       # re-download instead of using archives

# Load DK salary files (Milestone 3/4)
python -m src.ingest.dk_salaries load                                  # everything in data/raw/salaries/
python -m src.ingest.dk_salaries load path/to/2026-w01_dk_main.csv

# Check whether RotoGuru still serves historical DK salaries
python -m src.ingest.dk_salaries probe --season 2023 --week 1

pytest
```

## The weekly habit

The single most valuable thing to start now, per the spec: **archive every raw
file, every week, before parsing it.** Vendors don't sell history and DK doesn't
expose an API, so the archive you build is the dataset nobody can take away.

1. DK draft screen → *Export to CSV* → save as
   `data/raw/salaries/2026-w01_dk_main.csv` (the filename convention is parsed,
   and an off-convention name is rejected rather than guessed).
2. Projection vendor CSV → `data/raw/projections/2026-w01_vendorA.csv`.
3. `python -m src.ingest.dk_salaries load`

## The crosswalk loop

Bad joins poison every downstream model, so unmatched rows are queued rather
than guessed:

1. A load run writes anything it couldn't match to `data/unmatched_review.csv`.
2. You resolve each row into `data/manual_overrides.csv` (`source`,
   `source_name`, `source_id`, `player_id`, `note`).
3. Overrides load first on every subsequent run and win over every other tier.

Coverage is logged on every load and warns below the 97% threshold. On the
bundled fixture: 99.2% cold, 99.6% after one override.

Match methods, in waterfall order — `manual`, `dst_map`, `id_map`, `exact`,
`exact_name_pos`, `fuzzy` — are stored per row in `id_crosswalk`, so you can
always audit *how* a join was made. Fuzzy matching never crosses teams.

## Layout

```
config.yaml              # paths, seasons, thresholds (no secrets)
config.local.yaml        # gitignored; secrets + overrides, deep-merged on top
data/
  dfs.sqlite             # rebuildable; not versioned
  raw/                   # every source file as received; not versioned
  manual_overrides.csv   # hand-resolved ID matches; versioned
src/
  config.py              # config loading
  db.py                  # schema, connection, idempotent upserts
  scoring.py             # DK scoring — the function that must not be wrong
  teams.py               # 32-team map, historical aliases, DST names
  nflverse.py            # data fetch + raw archiving
  ingest/
    nfl_stats.py         # nflverse -> players, games, player_week_stats, dst_week_stats
    crosswalk.py         # the name-matching waterfall
    dk_salaries.py       # DK export + RotoGuru parsing
tests/
```

## Known limitations

- **DST points allowed** uses the opponent's final score; DK excludes points the
  opposing defense scored against your own offense. Blocked kicks score 0 —
  nflverse books them on the kicking team. Both are documented in
  `docs/data-sources.md`.
- **The RotoGuru adapter is unverified.** It was written from the documented
  format but never run against a live response, because the build environment
  allowed egress to GitHub only. Run the `probe` command before trusting a
  backfill.
- **No historical DK salaries yet**, which is what blocks the Milestone 6
  backtest from replaying real past slates.
- `nfl_data_py` pins `pandas<2`, which is why pandas is held at 1.5.3.

## Next

Milestone 4 proper (vendor projection adapters), then the naive optimizer, then
the backtest harness. The spec's §5.3 option 3 — a trailing 4-week DK-points
average — is the way to exercise the full pipeline before any purchased
projections exist.
