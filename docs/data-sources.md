# Data sources: what's free, what's stale, what's blocked

Findings from building Phase 1 milestones 1–3. Everything here was verified by
fetching the real endpoints unless explicitly marked otherwise.

## Summary

| Source | Status | Notes |
| --- | --- | --- |
| nflverse player week stats | ✅ working, 2019–2025 | Not via `nfl_data_py` — see below |
| nflverse team week stats | ✅ working, 2019–2025 | The DST inputs |
| nflverse schedules + closing lines | ✅ working | Free historical spreads/totals |
| `nfl_data_py.import_ids()` | ✅ working | ~12.5k players, 7.5k with both GSIS + PFR |
| `nfl_data_py.import_snap_counts()` | ✅ working | 99.1% joined to our stat rows |
| DK salary export (forward) | ⚠️ manual download | The durable path; no API |
| DK salary history (RotoGuru) | ❓ **unverified** | Blocked by sandbox egress, not confirmed dead |
| The Odds API | ⚠️ not needed in Phase 1 | Backtest uses free nflverse closing lines |

## `nfl_data_py` has two stale URLs

The spec's §5.1 snippet (`nfl.import_weekly_data(range(2019, 2026))`) does not
work as written. Two problems, both routed around in `src/nflverse.py`:

**1. `import_weekly_data` cannot see 2025.** It reads the `player_stats` release
tag, which nflverse froze at 2024. Current data lives under the `stats_player`
tag with a different filename:

```
# stale (library default) — 404 for 2025
.../releases/download/player_stats/player_stats_{year}.parquet     # 2019-2024 only

# current — what we use
.../releases/download/stats_player/stats_player_week_{year}.parquet  # 2019-2025 ✓
```

Verified: both tags return byte-identical schemas (145 columns) for overlapping
years, so switching costs nothing. `src/nflverse.py` tries the current tag first
and falls back to the legacy one.

**2. `import_schedules` single-sources over plain HTTP.** It reads
`http://www.habitatring.com/games.csv`. That host is unreachable from restricted
networks. The identical file is mirrored on GitHub over HTTPS, which we prefer,
keeping habitatring as a fallback:

```
https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv
```

This file is the reason Phase 1 needs **no purchased historical odds** — it
carries `spread_line` and `total_line` (closing) for every game back to 1999.

We still use `nfl_data_py` for `import_ids()` and `import_snap_counts()`, which
both work fine.

## Column name gotchas

- The interceptions-thrown column is `passing_interceptions`, **not**
  `interceptions`. Reading the wrong one silently yields zero for every QB.
- Fumbles lost are split across three columns that must be summed:
  `rushing_fumbles_lost`, `receiving_fumbles_lost`, `sack_fumbles_lost`.
- Two-point conversions are likewise split three ways (passing/rushing/receiving).
- `stats_player_week` carries `game_id` directly — no schedule join needed.

## DST: assembled, not provided

nflverse publishes no DST fantasy rows, so `src/ingest/nfl_stats.py` builds them
from `stats_team_week` (sacks, INTs, fumble recoveries, defensive TDs, safeties,
return TDs) plus points allowed from the game's final score.

Two known limitations, both stated rather than silently absorbed:

1. **Points allowed = opponent's final score.** DK excludes points scored by the
   opposing *defense* against your own offense (a pick-six you threw shouldn't
   count against your DST). Correcting this needs play-by-play attribution.
   Affects a minority of games; it's the most likely source of DST drift.
2. **Blocked kicks are recorded as 0.** nflverse books blocked FGs/punts on the
   kicking team's row, not the blocking defense's. Worth +2 each when it happens.

Spot-checked against real results: Dallas W1 2023 (40-0 shutout, 7 sacks, 2 INT,
1 fumble recovery, 1 defensive TD, 1 return TD) scores 35.0 — correct.

## DK salaries: the one input that isn't free

**Forward (durable):** DK's draft screen has *Export to CSV*. Download it every
week, drop it into `data/raw/salaries/` named `2026-w01_dk_main.csv`.
`src/ingest/dk_salaries.py` parses it. There is no public DK API for this — it's
a manual download, which is exactly why the spec says archiving is the durable
answer.

**Historical: unresolved.** The sandbox this was built in allows egress to
GitHub and package registries only; `rotoguru1.com`, `draftkings.com`, and
`api.the-odds-api.com` all return `Host not in allowlist`. So **RotoGuru was not
evaluated** — it may work perfectly from an unrestricted machine. Do not read
this as "RotoGuru is dead."

A RotoGuru adapter is implemented from its documented semicolon export format
but is **unverified against a live response**. Before trusting any backfill:

```bash
python -m src.ingest.dk_salaries probe --season 2023 --week 1
```

That prints `OK` with a parsed preview, `REACHABLE but UNPARSEABLE` with the raw
response (format changed — fix the adapter), or `UNREACHABLE` (still blocked, or
genuinely gone). Parsing is header-driven, so a column reorder is survivable; a
wholesale format change fails loudly rather than backfilling garbage salaries.

Until historical salaries exist, the backtest (Milestone 6) cannot replay past
slates. The spec's §5.3 option 3 — a naive trailing-average projection — still
exercises the rest of the pipeline.

## Schema additions

Four documented departures from the spec's §3 DDL, all additive:

| Table | Addition | Why |
| --- | --- | --- |
| `games` | `home_score`, `away_score` | Needed to derive DST points allowed; free in the same row |
| `player_week_stats` | `st_tds` | Return TDs are 6 real DK points; `dk_points` is wrong without it |
| `dst_week_stats` | whole table | Team defense stats don't fit the player columns |
| `id_crosswalk` | `match_method` values `dst_map`, `exact_name_pos` | Keeps join provenance auditable |

Scored DST rows are also mirrored into `player_week_stats` under
`player_id = 'DST_<TEAM>'` so the optimizer can treat every DK roster slot
uniformly.
