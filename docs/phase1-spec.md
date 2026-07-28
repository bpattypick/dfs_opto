# DFS Pipeline — Phase 1 Spec

**Goal:** Prove the plumbing and establish a backtested baseline. At the end of Phase 1 you can
answer: *"If I had used purchased projections + a naive optimizer every week for the past N
seasons, how would I have done?"* Every future improvement (ownership model, simulator) gets
measured against this number.

**Scope:** NFL main slates only. No modeling yet. No LLM yet.

**Estimated effort:** 2–3 weeks of evenings.

---

## 1. Stack

| Component | Choice | Why |
| --- | --- | --- |
| Language | Python 3.11+ | Your existing skillset |
| Database | SQLite (single file) | Zero setup; migrate to Postgres only if/when you need concurrent access |
| Core libs | pandas, nfl_data_py, requests, rapidfuzz | Ingestion + name matching |
| Optimizer | pydfs-lineup-optimizer (or PuLP directly) | Battle-tested DK constraint handling |
| Env | venv + requirements.txt, git repo from day one | Reproducibility |

```
pip install pandas nfl_data_py requests rapidfuzz pydfs-lineup-optimizer pyyaml
```

---

## 2. Repo structure

```
dfs-pipeline/
├── config.yaml               # API keys, paths, scoring settings
├── data/
│   ├── dfs.sqlite            # the database
│   ├── raw/                  # every source file archived as-received
│   │   ├── projections/      #   2026-w01_vendorA.csv ...
│   │   ├── salaries/         #   2026-w01_dk_main.csv ...
│   │   └── odds/             #   JSON snapshots
│   └── manual_overrides.csv  # hand-fixed ID matches
├── src/
│   ├── db.py                 # connection, schema creation, upsert helpers
│   ├── scoring.py            # DK fantasy point calculation
│   ├── ingest/
│   │   ├── nfl_stats.py      # nfl_data_py → player_week_stats, games
│   │   ├── crosswalk.py      # canonical ID mapping
│   │   ├── projections.py    # vendor CSV adapters
│   │   ├── dk_salaries.py    # DK salary CSV loader
│   │   └── odds.py           # The Odds API snapshots (forward-looking)
│   └── backtest/
│       ├── optimizer.py      # naive lineup builder
│       └── run_backtest.py   # the weekly replay loop + metrics
└── notebooks/
    └── 01_baseline_results.ipynb
```

**Rule:** archive every raw file exactly as received in `data/raw/` before parsing. Parsers have
bugs; source data disappears. Raw files are your insurance.

---

## 3. Schema

Canonical player ID = the **GSIS ID** (e.g. `00-0036322`), because `nfl_data_py` uses it natively
and ships a crosswalk to most other ID systems.

```sql
CREATE TABLE players (
    player_id TEXT PRIMARY KEY,        -- GSIS ID
    name TEXT,
    position TEXT,                     -- QB/RB/WR/TE/DST
    first_season INTEGER
);

CREATE TABLE id_crosswalk (
    player_id TEXT,                    -- canonical GSIS
    source TEXT,                       -- 'dk', 'vendorA', 'oddsapi', 'espn', ...
    source_id TEXT,                    -- vendor's ID if they have one
    source_name TEXT,                  -- name exactly as the vendor spells it
    match_method TEXT,                 -- 'id_map' | 'exact' | 'fuzzy' | 'manual'
    PRIMARY KEY (source, source_name, source_id)
);

CREATE TABLE games (
    game_id TEXT PRIMARY KEY,          -- nflverse game_id, e.g. 2025_01_GB_CHI
    season INTEGER, week INTEGER,
    home_team TEXT, away_team TEXT,
    kickoff_utc TEXT,
    spread_line REAL,                  -- closing spread (home perspective)
    total_line REAL                    -- closing total
);

CREATE TABLE player_week_stats (
    player_id TEXT, season INTEGER, week INTEGER,
    team TEXT, opponent TEXT, game_id TEXT,
    -- usage
    snaps INTEGER, snap_pct REAL, targets INTEGER, carries INTEGER,
    -- production
    receptions INTEGER, rec_yards REAL, rec_tds INTEGER,
    rush_yards REAL, rush_tds INTEGER,
    pass_attempts INTEGER, pass_yards REAL, pass_tds INTEGER, interceptions INTEGER,
    fumbles_lost INTEGER, two_pt INTEGER,
    -- computed
    dk_points REAL,
    PRIMARY KEY (player_id, season, week)
);

CREATE TABLE salaries (
    slate_id TEXT,                     -- e.g. '2025-w08-main'
    season INTEGER, week INTEGER,
    player_id TEXT,                    -- canonical (via crosswalk)
    dk_name TEXT,                      -- name as DK spells it
    dk_salary INTEGER,
    roster_position TEXT,              -- QB/RB/WR/TE/FLEX/DST
    team TEXT, opponent TEXT,
    PRIMARY KEY (slate_id, dk_name)
);

CREATE TABLE projections (
    source TEXT, season INTEGER, week INTEGER,
    player_id TEXT,
    proj_points REAL,
    proj_ownership REAL,               -- NULL if vendor doesn't provide
    pulled_at TEXT,                    -- ISO timestamp — leakage guard
    PRIMARY KEY (source, season, week, player_id)
);

CREATE TABLE odds_snapshots (
    game_id TEXT, book TEXT, market TEXT,   -- 'spread' | 'total' | player props later
    line REAL, price INTEGER,
    pulled_at TEXT,
    PRIMARY KEY (game_id, book, market, pulled_at)
);

CREATE TABLE backtest_runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT,
    config_json TEXT,                  -- full config for reproducibility
    metrics_json TEXT
);
```

Every table with time-relevant data carries `pulled_at`. This is your **leakage guard**: the
backtest may only use rows with `pulled_at` earlier than that slate's lock time.

---

## 4. DK scoring (`scoring.py`)

Classic DK NFL scoring — implement once, unit test it:

- Passing: 0.04/yd, 4/TD, −1/INT, +3 bonus at 300+ yards
- Rushing: 0.1/yd, 6/TD, +3 bonus at 100+ yards
- Receiving: 0.1/yd, 1/reception (full PPR), 6/TD, +3 bonus at 100+ yards
- −1 fumble lost, +2 two-point conversion
- DST: scoring table by points allowed + sacks/turnovers/TDs (implement from DK's rules page)

Write 5–6 unit tests with hand-calculated stat lines (include a 300-yard passer and a 100-yard
rusher to catch the bonuses). This function silently corrupts everything downstream if wrong.

---

## 5. Ingestion scripts

### 5.1 `nfl_stats.py` — the free backbone

```python
import nfl_data_py as nfl
weekly = nfl.import_weekly_data(range(2019, 2026))   # per-player per-week stats
sched  = nfl.import_schedules(range(2019, 2026))     # includes spread_line, total_line!
snaps  = nfl.import_snap_counts(range(2019, 2026))
ids    = nfl.import_ids()                            # gsis ↔ espn ↔ sleeper ↔ pfr ...
```

Key facts that save you money and time:

- `import_schedules` includes **historical closing Vegas spreads and totals for free**. You do not
  need to buy historical odds for the backtest. The Odds API is only needed going forward for live
  lines and props.
- `import_ids` is **80% of your crosswalk**, pre-built.
- Compute `dk_points` from raw stats via `scoring.py` at ingest time; store it.

Make ingestion **idempotent**: `INSERT OR REPLACE` keyed on primary keys, so re-running never
duplicates.

### 5.2 `crosswalk.py` — the name-matching gauntlet

Matching waterfall, in order; record `match_method` for each:

1. **ID map:** join on `import_ids()` if the source provides any known ID.
2. **Exact:** normalized name + team + position. Normalize = lowercase, strip punctuation, strip
   suffixes (Jr, Sr, II, III, IV), collapse whitespace. (`D.J. Moore` → `dj moore`.)
3. **Fuzzy:** `rapidfuzz.fuzz.token_sort_ratio >= 90` within same team + position only. **Never
   fuzzy-match across teams.**
4. **Manual:** anything unmatched → written to `unmatched_review.csv`; you resolve into
   `manual_overrides.csv`, which loads first on every run.

Known landmines: DST naming (DK uses team names, stats use abbreviations — hardcode a 32-row map),
players traded mid-season (match on name+position, then validate team against that week's roster),
rookie duplicates (two Josh Allens exist — position disambiguates), name changes.

Expect ~2–5% of rows to hit the manual queue in week 1, dropping near zero once overrides
accumulate. This is the highest-leverage grunt work in the whole project — bad joins poison every
model downstream.

### 5.3 `projections.py` — vendor adapter pattern

One small adapter function per vendor mapping their CSV columns → the standard `projections`
schema, plus a shared loader that runs the crosswalk and stamps `pulled_at`.

**Critical reality check:** most projection vendors do NOT sell historical archives. Your options
for backtest data:

1. **Start archiving now.** Subscribe, download every Thursday + Sunday morning, file into
   `data/raw/projections/`. In 4 months you have a real holdout season.
2. **Buy history where it exists.** A few analytics sites sell or bundle historical projections —
   worth one evening of research before the season.
3. **Bridge with a naive model.** For seasons with no purchased history, backtest a trivial
   baseline (e.g., trailing 4-week DK-points average, opponent-adjusted). It's deliberately weak,
   but it exercises the entire pipeline and gives a floor to beat.

Do (1) regardless. It costs nothing but discipline and becomes the most valuable dataset you own.

### 5.4 `dk_salaries.py`

Forward-looking: DK exposes a salary CSV per contest (*Export to CSV* on the draft screen) —
download and archive weekly. Historical DK salaries are scattered across community archives
(RotoGuru historically covered this; availability varies) — take whatever you can find for past
seasons, but as with projections, the durable answer is **archive going forward**. Parse slate
metadata (season/week/slate type) from your own filename convention: `2026-w01_dk_main.csv`.

### 5.5 `odds.py`

Forward-looking only in Phase 1 (backtest uses free nflverse closing lines). Snapshot
spreads/totals from The Odds API Thursday and Sunday morning; append-only into `odds_snapshots`.
Free tier (500 credits/mo) covers weekly NFL snapshots fine; player props cost more credits —
defer to Phase 2/3.

---

## 6. Naive optimizer (`backtest/optimizer.py`)

Using `pydfs-lineup-optimizer` with the DraftKings NFL preset:

- **Cash mode:** single optimal lineup by projected points.
- **Naive GPP mode:** 20 lineups via built-in randomness/exposure controls (max 50% exposure per
  player), plus one dumb-but-effective rule: force QB + same-team pass catcher.

No ownership, no sim — that's later. This is the baseline; its naivety is the point.

---

## 7. Backtest design (`backtest/run_backtest.py`)

The **replay loop**, per historical week W:

1. Load salaries for W's main slate.
2. Load projections where `pulled_at` < slate lock (or the naive model's output computed only from
   weeks < W).
3. Join via crosswalk; log join coverage % (target ≥ 97% of salary rows matched — alert below
   that).
4. Run optimizer → cash lineup + 20 GPP lineups.
5. Score every lineup against actual `dk_points`.
6. Store results keyed to `run_id`.

**Leakage rules (non-negotiable):**

- No stat, projection, or line from ≥ week W enters week W's lineup build.
- Naive-model features use trailing windows ending at week W−1.
- Injury reality check: historical backtests can't perfectly know who was announced out pre-lock.
  Approximation: zero out players with 0 snaps and a projection < 2 (likely known inactives). Note
  this as a stated limitation — it slightly flatters the backtest.

Metrics to compute per season and overall:

| Metric | What it tells you |
| --- | --- |
| Projection MAE / RMSE by position | Raw projection quality (compare vendors here later) |
| Spearman correlation, projection vs actual | Rank quality — matters more than MAE for lineup building |
| Cash lineup score distribution | Median, p25, p75 per week |
| Est. cash-line clear rate | % of weeks the cash lineup beats ~an estimated cash line (a workable proxy: the score of the ~median optimizer lineup + a fudge you calibrate later against real contest data) |
| GPP best-of-20 percentile | Where your best lineup would have landed vs. the field (rough until you have real contest data) |
| Join coverage % | Pipeline health |

Don't over-invest in contest-placement precision yet — real historical contest standings data
arrives in Phase 2 with the ownership work. Phase 1's job is a **consistent, leakage-free
harness**, not a perfect P&L.

**Deliverable:** `01_baseline_results.ipynb` — metrics table + three charts (weekly cash scores
over time, projection error by position, projected vs. actual scatter).

---

## 8. Milestones

| # | Milestone | Est. | Done when |
| --- | --- | --- | --- |
| 1 | Repo, venv, config, schema created | 1 evening | `python -m src.db` builds empty `dfs.sqlite` |
| 2 | nfl_data_py ingest + DK scoring + unit tests | 2 evenings | 2019–2025 stats queryable with `dk_points` |
| 3 | Crosswalk + manual override loop | 2–3 evenings | ≥97% match rate on a sample DK salary file |
| 4 | Projections + salaries adapters; archiving habit starts | 2 evenings | Current week loads end-to-end |
| 5 | Naive optimizer produces legal lineups | 1–2 evenings | 20 valid DK lineups from current slate |
| 6 | Backtest harness + metrics notebook | 3–4 evenings | Baseline numbers for ≥1 full season |

---

## 9. Definition of done

- [ ] One command ingests a new week end-to-end (`python -m src.ingest.weekly --week 8`)
- [ ] Raw files archived for every source, every week
- [ ] Join coverage ≥ 97% with automated warning below threshold
- [ ] DK scoring unit tests pass, including bonus thresholds and DST
- [ ] Backtest runs a full season with zero look-ahead access (spot-check: delete week W data,
      confirm week W lineup output unchanged)
- [ ] Baseline metrics written down. This number is what Phase 2 must beat.
