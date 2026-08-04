# Product Vision — Top-Tier DFS Platform

**Status:** Planning document (branch: `think-big`)  
**Companion docs:** [`phase1-spec.md`](phase1-spec.md) (current build), [`data-sources.md`](data-sources.md) (verified sources)

This document describes the north-star product: a multi-platform DFS decision engine with a
full web UI, simulation, ownership modeling, automation, and expansion to FanDuel, Yahoo, and
adjacent betting surfaces. Phase 1 (CLI + SQLite + DK NFL) is the **data spine** — roughly 5%
of the finished product.

---

## 1. North star

A top-tier DFS product is not "an optimizer with a website." It is a **decision engine**:

1. Ingest everything that moves prices and outcomes.
2. Simulate the field.
3. Tell users *what to play and why*.
4. Automate the grunt work.
5. Prove edge with rigorous, leakage-free backtests.

The moat is **simulation + ownership + provable backtest edge**, fed by a historical archive
nobody else built cleanly. Most competitors cut corners on data hygiene and cannot backtest
honestly.

**Design principles (carry forward from Phase 1):**

- Archive every raw file before parsing — immutable, versioned, auditable.
- Canonical player IDs with auditable crosswalk (`match_method` on every join).
- Leakage guards (`pulled_at` timestamps; no week-W data in week-W builds).
- Idempotent ingest (`INSERT OR REPLACE` / upserts) — safe to re-run.

---

## 2. What "top of the line" means — four layers

| Layer | What users pay for | Phase 1 today |
| --- | --- | --- |
| **Data** | Salaries, projections, ownership, news, weather, lines, beat reports | nflverse stats + DK salary loader |
| **Models** | Projections, ownership, correlation, boom/bust distributions | Not started |
| **Optimization** | Legal lineups under complex rules, late swap, multi-entry | Planned (naive) |
| **Simulation** | "If I play this, what's my ROI distribution vs the field?" | Not started |

The website is the shell. The product is the engine underneath.

---

## 3. Product surface — the website

One dashboard, many workflows. Not a single optimizer page.

### 3.1 Core tools (must-have for credibility)

| Tool | Description |
| --- | --- |
| **Slate hub** | Today's slates across DK / FanDuel / Yahoo. Lock times, weather, Vegas lines, news feed, inactive tracker. |
| **Projections workbench** | Blend vendor projections, internal models, and manual overrides. Floor / median / ceiling — not just a point estimate. Vendor comparison (MAE, rank correlation). |
| **Ownership lab** | Projected ownership by contest type (cash vs GPP vs single-entry). Stack ownership, bring-back ownership, leverage scores. |
| **Lineup builder** | Cash, single-entry GPP, MME (mass multi-entry). Stacks, bring-backs, max exposure, min uniqueness, correlation floors, game-environment rules. |
| **Simulator** | Monte Carlo or field-based sim: 10k–100k contest outcomes using correlated player distributions + ownership. Output: ROI, top-1% rate, duplication risk, expected profit. **This is the product.** |
| **Late swap** | Pre-compute swap trees before lock; one-click apply when inactives drop. Critical for NFL Sunday. |
| **Backtest & research** | "How would this strategy have done 2019–2025?" Leakage-free, contest-type aware. Phase 1 harness → research platform. |
| **Bankroll & contest selection** | Which contest types, buy-in levels, and field sizes fit your edge and roll? |

### 3.2 Premium / differentiated tools

| Tool | Description |
| --- | --- |
| **News & injury agent** | Ingest beat reporters, official inactives, weather APIs; auto-adjust projections and flag swap opportunities. LLM for parsing ambiguous news *with human-auditable diffs*. |
| **Correlation explorer** | Visual stack matrices: QB→WR air yards share, game environment, pace. Built from play-by-play, not heuristics. |
| **Field builder** | Simulate *the opponent field* from historical ownership + salary patterns. Lineups compete against a realistic field. |
| **Portfolio optimizer** | Optimize 150 lineups as a *portfolio* (maximize EV subject to duplication and correlation constraints). |
| **Live slate tools** | In-game for showdown: win probability, optimal pivot paths, correlation shifts. |
| **Cross-sport** | NBA, MLB, PGA, NHL. Same platform, sport-specific plugins. |
| **API + exports** | One-click export to DK/FD upload format. Power users and syndicates. |

### 3.3 Adjacent: sports betting module

DraftKings Sportsbook, FanDuel, etc. share the data pipeline (lines, player stats, news) but
produce different outputs (bet slips vs lineups). Same data layer; separate **betting EV
module** (compare model line vs book line, Kelly sizing, same-game parlay correlation).

---

## 4. Technical architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Web App (Next.js)                           │
│  Slate hub · Optimizer UI · Sim results · Backtest · Settings   │
└────────────────────────────┬────────────────────────────────────┘
                             │ GraphQL or tRPC
┌────────────────────────────▼────────────────────────────────────┐
│                  API Layer (FastAPI or Go)                        │
│  Auth · Jobs · Webhooks · Rate limits · Feature flags           │
└─────┬──────────────┬──────────────┬──────────────┬──────────────┘
      │              │              │              │
      ▼              ▼              ▼              ▼
┌──────────┐  ┌────────────┐  ┌───────────┐  ┌─────────────┐
│ Postgres │  │ Redis      │  │ Object    │  │ ClickHouse  │
│ (Supabase│  │ (queues,   │  │ Storage   │  │ or DuckDB   │
│ or Neon) │  │  cache)    │  │ (raw CSV, │  │ (analytics, │
│          │  │            │  │  parquet) │  │  backtests) │
└──────────┘  └────────────┘  └───────────┘  └─────────────┘
      ▲              ▲
      │              │
┌─────┴──────────────┴────────────────────────────────────────────┐
│                   Worker Fleet (async jobs)                     │
│  ingest · crosswalk · project · sim · backtest · news agent     │
└─────────────────────────────────────────────────────────────────┘
      ▲
      │ feeds, scrapers, APIs, manual uploads
┌─────┴───────────────────────────────────────────────────────────┐
│  DK · FanDuel · Yahoo · Odds API · nflverse · vendors · news    │
└─────────────────────────────────────────────────────────────────┘
```

### 4.1 Multi-store database strategy

| Store | Role |
| --- | --- |
| **Postgres** (Supabase / Neon) | Users, subscriptions, slate metadata, saved lineups, contest configs, crosswalk overrides, job status |
| **Object storage** (S3 / Supabase Storage / R2) | Every raw file ever received — the moat. Immutable, versioned, auditable |
| **ClickHouse or DuckDB** | Player-week facts, sim outputs, backtest runs — columnar, fast aggregations over millions of rows |
| **Redis** | Job queues, sim result cache, live slate state, rate limiting |
| **SQLite** (local dev only) | Phase 1 CLI, personal backtests, fast iteration |

Supabase is a strong **Phase 2 launchpad** (Auth + Postgres + Storage). At scale, add dedicated
workers and a columnar analytics DB — not everything in one Supabase instance.

### 4.2 Infrastructure at scale

| Concern | Choice |
| --- | --- |
| Frontend | Next.js, React, Tailwind |
| API | FastAPI (reuse Python stack) or Go for sim latency |
| Auth / billing | Supabase Auth or Clerk + Stripe |
| Workers | Fly.io, Railway, or AWS ECS — scale sim workers independently |
| CI/CD | GitHub Actions — ingest smoke tests, scoring regression, crosswalk coverage |
| Observability | Sentry, Datadog — sim job failures, ingest coverage alerts (<97%) |
| Feature flags | LaunchDarkly or PostHog |
| Job orchestration | Temporal, Celery + Redis, or Inngest |

---

## 5. Multi-platform abstraction

Platform differences are a **plugin layer**, not copy-paste scoring functions.

```
Platform
  ├── ScoringRules       (DK full PPR vs FD half PPR vs Yahoo...)
  ├── RosterRules        (slots, salary cap, max per team)
  ├── SalaryAdapter      (CSV/API format → canonical)
  ├── ExportFormat       (upload CSV for each site)
  └── ContestTypes       (classic, showdown, single-game, pick'em)
```

Current code (`scoring.py`, `dk_salaries.py`) becomes DK/NFL plugins. Each platform adds:

- `source = 'fd' | 'yahoo' | 'dk'` rows in `id_crosswalk`
- Platform-specific DST naming
- Different flex rules, showdown CPT multiplier (1.5× on DK showdown)
- Platform-specific `fantasy_points` columns (not just `dk_points`)

**FanDuel (NFL classic):** half-PPR, different roster construction, different contest ecosystem.  
**Yahoo:** different scoring and roster rules.  
**Betting:** same player/game entities; different output schema (`bets`, `parlays`).

Add `platform` and `sport` columns everywhere from day one in the production schema, even if
only DK/NFL ships first.

---

## 6. Core engines

### 6.1 Projection ensemble

Blend 3–5 vendor sources + internal model with weights learned from backtest MAE by
position/slate type. Store `pulled_at` for leakage. Output: mean, stdev, percentiles
(floor/median/ceiling).

### 6.2 Ownership model

Historical contest results (when obtainable) + salary + projection + stack popularity → predicted
ownership. Separate models for cash vs GPP vs single-entry.

**Leverage** = projection rank − ownership rank.

### 6.3 Correlation / copula layer

Independent player projections are wrong. Model QB–WR, game stack, bring-back, RB vs game script
from play-by-play or sim from team totals. Feed into simulator and portfolio optimizer.

### 6.4 Simulator (crown jewel)

For each slate:

1. Draw N correlated outcome scenarios.
2. Score all lineups in each scenario.
3. Build synthetic field from ownership model.
4. Compute finish distribution, ROI, duplication.

Python orchestrates; hot path in Rust/C++ or GPU if needed for sub-minute reruns on news breaks.

### 6.5 Optimizer

Evolution path:

1. `pydfs-lineup-optimizer` (already in requirements) — naive baseline.
2. Custom MILP (PuLP / OR-Tools) for exotic constraints.
3. Portfolio-level optimization across 150 lineups.
4. Late-swap dynamic programming.

### 6.6 Backtest harness

Every strategy change runs through leakage-free replay. Store `run_id`, config hash, metrics.
Marketing asset: "backtested 2019–2025."

**Leakage rules (non-negotiable):**

- No stat, projection, or line from ≥ week W enters week W's lineup build.
- Naive-model features use trailing windows ending at week W−1.
- Approximate pre-lock inactives: zero players with 0 snaps and projection < threshold.

---

## 7. Automation layer

| Trigger | Automated action |
| --- | --- |
| Thursday lines post | Pull odds, update game environments |
| Salary drop | Ingest, crosswalk, flag unmatched, notify user |
| Projection vendor publish | Ingest, diff vs yesterday, alert big movers |
| Inactives (90 min pre-kick) | Zero projections, regenerate swap recommendations |
| User saved "build" | Run sim + export lineups to DK/FD CSV |
| Post-slate | Score lineups, update bankroll, archive results |

LLMs fit **parsing ambiguous news** and **natural-language build rules** ("3 stacks, no more
than 2 from any game under 42 total") — not core math. Human-auditable diffs before projections
change.

---

## 8. Full schema

Schema is split into **Phase 1 (implemented)** and **Production (target)**. Phase 1 tables
live in `src/db.py` today. Production extends them with `platform`, `sport`, and new domains.

### 8.1 Conventions

- **Canonical player ID:** GSIS ID (`00-0036322`) for NFL; sport-specific IDs for other sports.
- **DST synthetic ID:** `DST_<TEAM>` (e.g. `DST_KC`).
- **Slate ID format:** `{platform}-{sport}-{season}-w{week:02d}-{slate_type}`  
  Example: `dk-nfl-2025-w08-main`, `fd-nfl-2025-w08-classic`.
- **Timestamps:** ISO 8601 UTC. Every time-sensitive row has `pulled_at` or `created_at`.
- **Soft deletes:** `deleted_at` on user-owned rows where applicable.

---

### 8.2 Phase 1 schema (implemented — SQLite)

These tables exist today. See `src/db.py` for the source of truth.

```sql
-- ── Identity ──────────────────────────────────────────────────────────────

CREATE TABLE players (
    player_id    TEXT PRIMARY KEY,   -- GSIS ID, or DST_<TEAM>
    name         TEXT,
    position     TEXT,               -- QB/RB/WR/TE/DST
    first_season INTEGER
);

CREATE TABLE id_crosswalk (
    player_id    TEXT,
    source       TEXT,               -- 'dk', 'vendorA', 'oddsapi', ...
    source_id    TEXT,
    source_name  TEXT,
    match_method TEXT,               -- manual|dst_map|id_map|exact|exact_name_pos|fuzzy
    PRIMARY KEY (source, source_name, source_id)
);

-- ── Schedule & lines ──────────────────────────────────────────────────────

CREATE TABLE games (
    game_id     TEXT PRIMARY KEY,    -- nflverse: 2025_01_GB_CHI
    season      INTEGER,
    week        INTEGER,
    home_team   TEXT,
    away_team   TEXT,
    kickoff_utc TEXT,
    spread_line REAL,
    total_line  REAL,
    home_score  INTEGER,
    away_score  INTEGER
);

-- ── Historical performance ─────────────────────────────────────────────────

CREATE TABLE player_week_stats (
    player_id      TEXT,
    season         INTEGER,
    week           INTEGER,
    team           TEXT,
    opponent       TEXT,
    game_id        TEXT,
    snaps          INTEGER,
    snap_pct       REAL,
    targets        INTEGER,
    carries        INTEGER,
    receptions     INTEGER,
    rec_yards      REAL,
    rec_tds        INTEGER,
    rush_yards     REAL,
    rush_tds       INTEGER,
    pass_attempts  INTEGER,
    pass_yards     REAL,
    pass_tds       INTEGER,
    interceptions  INTEGER,
    fumbles_lost   INTEGER,
    two_pt         INTEGER,
    st_tds         INTEGER,
    dk_points      REAL,
    PRIMARY KEY (player_id, season, week)
);

CREATE TABLE dst_week_stats (
    team           TEXT,
    season         INTEGER,
    week           INTEGER,
    opponent       TEXT,
    game_id        TEXT,
    points_allowed INTEGER,
    sacks          REAL,
    interceptions  INTEGER,
    fumbles_rec    INTEGER,
    def_tds        INTEGER,
    special_tds    INTEGER,
    safeties       INTEGER,
    blocked_kicks  INTEGER,
    dk_points      REAL,
    PRIMARY KEY (team, season, week)
);

-- ── Slate inputs ───────────────────────────────────────────────────────────

CREATE TABLE salaries (
    slate_id        TEXT,
    season          INTEGER,
    week            INTEGER,
    player_id       TEXT,
    dk_name         TEXT,
    dk_salary       INTEGER,
    roster_position TEXT,
    team            TEXT,
    opponent        TEXT,
    PRIMARY KEY (slate_id, dk_name)
);

CREATE TABLE projections (
    source         TEXT,
    season         INTEGER,
    week           INTEGER,
    player_id      TEXT,
    proj_points    REAL,
    proj_ownership REAL,
    pulled_at      TEXT,
    PRIMARY KEY (source, season, week, player_id)
);

CREATE TABLE odds_snapshots (
    game_id   TEXT,
    book      TEXT,
    market    TEXT,
    line      REAL,
    price     INTEGER,
    pulled_at TEXT,
    PRIMARY KEY (game_id, book, market, pulled_at)
);

-- ── Research ─────────────────────────────────────────────────────────────────

CREATE TABLE backtest_runs (
    run_id       TEXT PRIMARY KEY,
    created_at   TEXT,
    config_json  TEXT,
    metrics_json TEXT
);
```

**Phase 1 indexes:**

```sql
CREATE INDEX idx_pws_season_week  ON player_week_stats (season, week);
CREATE INDEX idx_pws_team         ON player_week_stats (season, week, team);
CREATE INDEX idx_games_season_wk  ON games (season, week);
CREATE INDEX idx_salaries_slate   ON salaries (season, week);
CREATE INDEX idx_proj_season_week ON projections (season, week);
CREATE INDEX idx_xwalk_player     ON id_crosswalk (player_id);
```

---

### 8.3 Production schema — platform & sport core

```sql
-- ── Reference data ───────────────────────────────────────────────────────────

CREATE TABLE platforms (
    platform_id   TEXT PRIMARY KEY,  -- 'dk', 'fd', 'yahoo', 'underdog'
    display_name  TEXT NOT NULL,
    website_url   TEXT
);

CREATE TABLE sports (
    sport_id      TEXT PRIMARY KEY,  -- 'nfl', 'nba', 'mlb', 'nhl', 'pga'
    display_name  TEXT NOT NULL
);

CREATE TABLE platform_sports (
    platform_id   TEXT REFERENCES platforms(platform_id),
    sport_id      TEXT REFERENCES sports(sport_id),
    is_active     BOOLEAN DEFAULT true,
    PRIMARY KEY (platform_id, sport_id)
);

CREATE TABLE scoring_rules (
    rule_set_id   TEXT PRIMARY KEY,  -- 'dk-nfl-classic', 'fd-nfl-classic'
    platform_id   TEXT NOT NULL,
    sport_id      TEXT NOT NULL,
    slate_type    TEXT NOT NULL,     -- 'classic', 'showdown', 'single_game'
    rules_json    JSONB NOT NULL,    -- full scoring + roster config
    effective_from DATE,
    UNIQUE (platform_id, sport_id, slate_type, effective_from)
);

-- Extend players for multi-sport
ALTER TABLE players ADD COLUMN sport_id TEXT DEFAULT 'nfl';
ALTER TABLE players ADD COLUMN status TEXT;          -- active, retired, ...
ALTER TABLE players ADD COLUMN updated_at TIMESTAMPTZ;
```

---

### 8.4 Production schema — slates & salaries (multi-platform)

Replaces / supersedes Phase 1 `salaries` table.

```sql
CREATE TABLE slates (
    slate_id        TEXT PRIMARY KEY,
    platform_id     TEXT NOT NULL,
    sport_id        TEXT NOT NULL,
    season          INTEGER NOT NULL,
    week            INTEGER,              -- NULL for daily sports (NBA/MLB)
    slate_date      DATE,                 -- for daily slates
    slate_type      TEXT NOT NULL,        -- classic, showdown, turbo, ...
    name            TEXT,
    lock_time_utc   TIMESTAMPTZ NOT NULL,
    salary_cap      INTEGER,
    roster_slots_json JSONB NOT NULL,     -- [{slot:'QB',count:1}, ...]
    game_ids        TEXT[],               -- games on this slate
    status          TEXT DEFAULT 'upcoming',  -- upcoming|locked|complete
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE slate_players (
    slate_id          TEXT REFERENCES slates(slate_id),
    platform_player_id TEXT,              -- ID from platform export if present
    player_id         TEXT,               -- canonical; NULL if unmatched
    platform_name     TEXT NOT NULL,      -- name as platform spells it
    salary            INTEGER NOT NULL,
    roster_position   TEXT NOT NULL,      -- QB, RB, WR, FLEX, CPT, ...
    team              TEXT,
    opponent          TEXT,
    avg_points        REAL,               -- from platform export
    is_disabled       BOOLEAN DEFAULT false,
    match_method      TEXT,               -- crosswalk audit trail
    PRIMARY KEY (slate_id, platform_name)
);

CREATE INDEX idx_slates_lock ON slates (lock_time_utc);
CREATE INDEX idx_slates_platform_sport ON slates (platform_id, sport_id, season);
CREATE INDEX idx_slate_players_player ON slate_players (player_id);
```

---

### 8.5 Production schema — projections & distributions

```sql
CREATE TABLE projection_sources (
    source_id     TEXT PRIMARY KEY,  -- 'vendorA', 'ensemble', 'trailing_4wk'
    display_name  TEXT,
    source_type   TEXT,              -- vendor, internal, naive_baseline
    is_active     BOOLEAN DEFAULT true
);

CREATE TABLE projections_v2 (
    source_id       TEXT NOT NULL,
    slate_id        TEXT,              -- preferred join key
    season          INTEGER,
    week            INTEGER,
    player_id       TEXT NOT NULL,
    proj_mean       REAL,
    proj_floor      REAL,
    proj_ceiling    REAL,
    proj_stdev      REAL,
    proj_ownership  REAL,            -- NULL if not provided
    proj_snap_pct   REAL,
    raw_json        JSONB,           -- vendor-specific extras
    pulled_at       TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source_id, slate_id, player_id)
);

CREATE TABLE projection_ensemble_weights (
    ensemble_id   TEXT,
    source_id     TEXT,
    position      TEXT,              -- NULL = all positions
    weight        REAL,
    learned_from  TEXT,              -- backtest run_id that tuned this
    PRIMARY KEY (ensemble_id, source_id, position)
);

CREATE TABLE ownership_projections (
    slate_id        TEXT NOT NULL,
    player_id       TEXT NOT NULL,
    contest_type    TEXT NOT NULL,   -- cash, gpp_se, gpp_mme
    proj_ownership  REAL NOT NULL,
    model_version   TEXT,
    pulled_at       TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (slate_id, player_id, contest_type, model_version)
);
```

---

### 8.6 Production schema — correlation & simulation

```sql
CREATE TABLE player_correlations (
    sport_id        TEXT,
    season          INTEGER,
    -- pairwise or parametric; store what the sim engine needs
    player_id_a     TEXT,
    player_id_b     TEXT,
    correlation     REAL,
    context         TEXT,            -- 'same_game_stack', 'bring_back', ...
    model_version   TEXT,
    computed_at     TIMESTAMPTZ,
    PRIMARY KEY (sport_id, player_id_a, player_id_b, context, model_version)
);

CREATE TABLE sim_runs (
    sim_run_id      TEXT PRIMARY KEY,
    slate_id        TEXT NOT NULL,
    user_id         UUID,            -- NULL for system runs
    build_config_json JSONB NOT NULL,
    n_iterations    INTEGER,
    field_size      INTEGER,
    contest_type    TEXT,
    status          TEXT,            -- queued, running, complete, failed
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    error_message   TEXT
);

CREATE TABLE sim_lineup_results (
    sim_run_id      TEXT REFERENCES sim_runs(sim_run_id),
    lineup_id       TEXT,
    iteration       INTEGER,
    total_points    REAL,
    finish_rank     INTEGER,
    is_cash         BOOLEAN,
    PRIMARY KEY (sim_run_id, lineup_id, iteration)
);

CREATE TABLE sim_run_summary (
    sim_run_id      TEXT PRIMARY KEY,
    mean_roi        REAL,
    p25_roi         REAL,
    p75_roi         REAL,
    top1_pct_rate   REAL,
    avg_duplication REAL,
    summary_json    JSONB
);
```

---

### 8.7 Production schema — lineups & builds

```sql
CREATE TABLE users (
    user_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT UNIQUE NOT NULL,
    display_name    TEXT,
    tier            TEXT DEFAULT 'free',  -- free, pro, syndicate
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE saved_builds (
    build_id        TEXT PRIMARY KEY,
    user_id         UUID REFERENCES users(user_id),
    name            TEXT NOT NULL,
    platform_id     TEXT NOT NULL,
    sport_id        TEXT NOT NULL,
    slate_type      TEXT,
    rules_json      JSONB NOT NULL,    -- stacks, exposure, uniqueness, NL-parsed rules
    is_template     BOOLEAN DEFAULT false,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE lineups (
    lineup_id       TEXT PRIMARY KEY,
    build_id        TEXT REFERENCES saved_builds(build_id),
    slate_id        TEXT NOT NULL,
    user_id         UUID REFERENCES users(user_id),
    lineup_json     JSONB NOT NULL,    -- [{player_id, slot, salary}, ...]
    projected_points REAL,
    projected_ownership REAL,
    sim_ev           REAL,
    exported_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE lineup_players (
    lineup_id       TEXT REFERENCES lineups(lineup_id),
    player_id       TEXT NOT NULL,
    roster_slot     TEXT NOT NULL,
    salary          INTEGER,
    is_captain      BOOLEAN DEFAULT false,
    PRIMARY KEY (lineup_id, roster_slot)
);
```

---

### 8.8 Production schema — backtest & research

Extends Phase 1 `backtest_runs`.

```sql
CREATE TABLE backtest_runs_v2 (
    run_id          TEXT PRIMARY KEY,
    name            TEXT,
    platform_id     TEXT NOT NULL,
    sport_id        TEXT NOT NULL,
    season_start    INTEGER,
    season_end      INTEGER,
    build_config_json JSONB NOT NULL,
    projection_source TEXT,
    optimizer_mode  TEXT,            -- cash, gpp_se, gpp_mme
    created_at      TIMESTAMPTZ DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    status          TEXT
);

CREATE TABLE backtest_week_results (
    run_id          TEXT REFERENCES backtest_runs_v2(run_id),
    season          INTEGER,
    week            INTEGER,
    slate_id        TEXT,
    lineup_score    REAL,
    cash_line_est   REAL,
    beat_cash       BOOLEAN,
    gpp_best_score  REAL,
    gpp_best_pctile REAL,
    join_coverage   REAL,
    PRIMARY KEY (run_id, season, week)
);

CREATE TABLE backtest_metrics (
    run_id          TEXT PRIMARY KEY,
    projection_mae_json  JSONB,      -- by position
    projection_spearman  REAL,
    cash_median_score    REAL,
    cash_clear_rate      REAL,
    gpp_top1_pct_rate    REAL,
    metrics_json         JSONB
);
```

---

### 8.9 Production schema — raw archive & jobs

```sql
CREATE TABLE raw_files (
    file_id         TEXT PRIMARY KEY,
    storage_path    TEXT NOT NULL,   -- s3://... or supabase storage path
    source          TEXT NOT NULL,   -- dk, fd, vendorA, nflverse, oddsapi
    file_type       TEXT,            -- salary_csv, projection_csv, odds_json
    platform_id     TEXT,
    sport_id        TEXT,
    season          INTEGER,
    week            INTEGER,
    slate_id        TEXT,
    sha256          TEXT NOT NULL,
    byte_size       INTEGER,
    received_at     TIMESTAMPTZ NOT NULL,
    parsed_at       TIMESTAMPTZ,
    parse_status    TEXT             -- pending, ok, failed
);

CREATE TABLE ingest_jobs (
    job_id          TEXT PRIMARY KEY,
    job_type        TEXT NOT NULL,   -- nfl_stats, salaries, projections, odds
    status          TEXT NOT NULL,   -- queued, running, complete, failed
    config_json     JSONB,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    rows_written    INTEGER,
    error_message   TEXT,
    triggered_by    TEXT             -- cron, webhook, manual
);

CREATE TABLE news_items (
    news_id         TEXT PRIMARY KEY,
    player_id       TEXT,
    team            TEXT,
    headline        TEXT NOT NULL,
    body            TEXT,
    source          TEXT,
    source_url      TEXT,
    severity        TEXT,            -- out, doubtful, question, news
    published_at    TIMESTAMPTZ,
    ingested_at     TIMESTAMPTZ DEFAULT now(),
    applied_to_proj BOOLEAN DEFAULT false
);
```

---

### 8.10 Production schema — odds & betting (adjacent module)

```sql
CREATE TABLE odds_snapshots_v2 (
    snapshot_id     TEXT PRIMARY KEY,
    game_id         TEXT,
    player_id       TEXT,            -- NULL for game markets
    platform_id     TEXT,            -- sportsbook: 'dk_sb', 'fd_sb'
    book            TEXT,
    market          TEXT,            -- spread, total, player_pass_yds, ...
    line            REAL,
    price_american  INTEGER,
    implied_prob    REAL,
    pulled_at       TIMESTAMPTZ NOT NULL
);

CREATE TABLE model_lines (
    model_id        TEXT,
    game_id         TEXT,
    player_id       TEXT,
    market          TEXT,
    fair_line       REAL,
    fair_prob       REAL,
    edge_vs_book    REAL,
    computed_at     TIMESTAMPTZ,
    PRIMARY KEY (model_id, game_id, player_id, market, computed_at)
);

CREATE TABLE saved_bets (
    bet_id          TEXT PRIMARY KEY,
    user_id         UUID REFERENCES users(user_id),
    platform_id     TEXT,
    legs_json       JSONB NOT NULL,
    stake           REAL,
    model_ev        REAL,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

---

### 8.11 Production schema — bankroll & contests

```sql
CREATE TABLE user_bankrolls (
    user_id         UUID PRIMARY KEY REFERENCES users(user_id),
    balance         REAL NOT NULL DEFAULT 0,
    currency        TEXT DEFAULT 'USD',
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE contest_entries (
    entry_id        TEXT PRIMARY KEY,
    user_id         UUID REFERENCES users(user_id),
    slate_id        TEXT NOT NULL,
    platform_id     TEXT NOT NULL,
    contest_type    TEXT,
    buy_in          REAL,
    field_size      INTEGER,
    lineup_id       TEXT REFERENCES lineups(lineup_id),
    entered_at      TIMESTAMPTZ,
    result_place    INTEGER,
    result_payout   REAL,
    recorded_at     TIMESTAMPTZ DEFAULT now()
);
```

---

## 9. Repo structure (target)

```
dfs_opto/
├── config.yaml
├── config.local.yaml          # gitignored secrets
├── data/                      # local dev only
│   ├── dfs.sqlite
│   └── raw/
├── docs/
│   ├── phase1-spec.md
│   ├── data-sources.md
│   └── product-vision.md      # this file
├── apps/
│   └── web/                   # Next.js frontend
├── packages/
│   └── api/                   # FastAPI backend
├── src/                       # Python data/engine layer (Phase 1 → shared lib)
│   ├── db.py
│   ├── scoring/
│   │   ├── dk_nfl.py
│   │   ├── fd_nfl.py
│   │   └── base.py
│   ├── platforms/
│   │   ├── dk/
│   │   ├── fd/
│   │   └── yahoo/
│   ├── ingest/
│   ├── models/                # projection, ownership, correlation
│   ├── optimizer/
│   ├── simulator/
│   ├── backtest/
│   └── agents/                # news parser, scheduled jobs
├── workers/                   # Celery/Temporal task definitions
├── migrations/                # Postgres Alembic migrations
└── tests/
```

---

## 10. Roadmap

### Phase 1 — Data spine (current)

**Goal:** Leakage-free backtest baseline for DK NFL main slates.

| Milestone | Deliverable | Status |
| --- | --- | --- |
| 1.1 | Repo, venv, config, SQLite schema | ✅ Done |
| 1.2 | nflverse ingest + DK scoring + unit tests | ✅ Done |
| 1.3 | Crosswalk + manual override loop (≥97% coverage) | ✅ Done |
| 1.4 | Projection adapters + salary archiving habit | 🟡 DK salaries done |
| 1.5 | Naive optimizer (cash + 20 GPP lineups) | ⬜ Not started |
| 1.6 | Backtest harness + metrics notebook | ⬜ Not started |

**Exit criteria:** One command replays a full season with zero look-ahead. Baseline metrics
written down.

---

### Phase 2 — Web foundation

**Goal:** Deployed app with auth; slate data visible in a browser.

| Milestone | Deliverable | Est. |
| --- | --- | --- |
| 2.1 | Postgres schema migration (Phase 1 tables + `platforms`, `sports`, `slates`) | 1 week |
| 2.2 | Supabase Auth + Stripe billing skeleton | 1 week |
| 2.3 | FastAPI: slate list, player pool, salary endpoints | 1 week |
| 2.4 | Next.js: slate hub, player table, basic filters | 2 weeks |
| 2.5 | Object storage for raw files; ingest jobs table + worker | 1 week |
| 2.6 | Sync pipeline: local ingest → Postgres | 1 week |

**Exit criteria:** User logs in, sees current week's DK NFL slate with salaries and Vegas lines.

---

### Phase 3 — Optimizer UI + multi-platform scoring

**Goal:** Build and export lineups from the website; FanDuel adapter.

| Milestone | Deliverable | Est. |
| --- | --- | --- |
| 3.1 | Lineup builder UI (constraints form) | 2 weeks |
| 3.2 | Optimizer service (cash + MME via pydfs-lineup-optimizer) | 1 week |
| 3.3 | DK CSV export | 3 days |
| 3.4 | FanDuel scoring rules + salary adapter + export | 2 weeks |
| 3.5 | `scoring_rules` table + platform plugin architecture | 1 week |
| 3.6 | Saved builds (persist rules per user) | 1 week |

**Exit criteria:** User builds 20 DK or FD lineups, exports upload CSV.

---

### Phase 4 — Projections & ownership v1

**Goal:** Blend vendors; show leverage in the UI.

| Milestone | Deliverable | Est. |
| --- | --- | --- |
| 4.1 | Vendor projection adapters (2–3 sources) | 2 weeks |
| 4.2 | Projections workbench UI | 1 week |
| 4.3 | Trailing-average naive baseline (backtest bridge) | 1 week |
| 4.4 | Ownership model v1 (heuristic from salary + projection rank) | 2 weeks |
| 4.5 | Ownership lab UI + leverage column | 1 week |
| 4.6 | Projection ensemble v1 + backtest-weighted blending | 2 weeks |

**Exit criteria:** Ownership and blended projections visible per slate; MAE tracked by position.

---

### Phase 5 — Simulator v1 (the product becomes real)

**Goal:** ROI distribution for lineup sets vs a synthetic field.

| Milestone | Deliverable | Est. |
| --- | --- | --- |
| 5.1 | Player distribution model (floor/mean/ceiling from history) | 2 weeks |
| 5.2 | Correlation layer v1 (same-game stack heuristics) | 2 weeks |
| 5.3 | Monte Carlo sim engine | 3 weeks |
| 5.4 | Field builder (ownership-based synthetic opponents) | 2 weeks |
| 5.5 | Sim results UI (ROI, top-1%, duplication) | 2 weeks |
| 5.6 | ClickHouse/DuckDB for sim output storage | 1 week |

**Exit criteria:** User runs 10k-iteration sim on a 20-lineup portfolio; sees ROI distribution
in <2 minutes.

---

### Phase 6 — Automation & late swap

**Goal:** Hands-off weekly workflow; Sunday swap support.

| Milestone | Deliverable | Est. |
| --- | --- | --- |
| 6.1 | Scheduled ingest (salaries, projections, odds) | 1 week |
| 6.2 | Alert system (unmatched players, big projection movers) | 1 week |
| 6.3 | News/injury ingest + LLM parser with audit diff | 3 weeks |
| 6.4 | Auto-projection adjustments on news | 2 weeks |
| 6.5 | Late swap: swap tree pre-compute + one-click apply | 3 weeks |
| 6.6 | Saved build → auto sim + export on trigger | 1 week |

**Exit criteria:** User receives notification when salaries drop; swap recommendations within
5 minutes of inactive report.

---

### Phase 7 — Research platform & backtest at scale

**Goal:** Prove edge publicly; internal strategy iteration.

| Milestone | Deliverable | Est. |
| --- | --- | --- |
| 7.1 | Backtest harness v2 (multi-season, multi-platform) | 3 weeks |
| 7.2 | Backtest UI + metrics dashboard | 2 weeks |
| 7.3 | Historical salary archive (RotoGuru backfill or manual) | ongoing |
| 7.4 | Public backtest report (marketing) | 1 week |
| 7.5 | Portfolio optimizer (150 lineups as one decision) | 4 weeks |

**Exit criteria:** Full 2019–2025 DK NFL backtest published with documented assumptions.

---

### Phase 8 — Scale, syndicate, cross-sport

**Goal:** Multi-sport; API tier; betting module.

| Milestone | Deliverable | Est. |
| --- | --- | --- |
| 8.1 | NBA plugin (daily slates, late swap) | 4 weeks |
| 8.2 | MLB plugin | 4 weeks |
| 8.3 | Headless API + rate limits (syndicate tier) | 2 weeks |
| 8.4 | Betting EV module (props, SGP) | 4 weeks |
| 8.5 | Yahoo adapter | 2 weeks |
| 8.6 | Worker auto-scaling for sim jobs | 2 weeks |

**Exit criteria:** NBA slate live; API documented; one betting market showing model vs book edge.

---

## 11. Roadmap timeline (visual)

```
2026 Q1-Q2   Phase 1 complete (backtest baseline)
2026 Q2      Phase 2 (web + Postgres)
2026 Q3      Phase 3 (optimizer UI) + Phase 4 start (projections)
2026 Q4      Phase 4 complete + Phase 5 start (simulator)
2027 Q1      Phase 5 complete (MVP product)
2027 Q2      Phase 6 (automation + late swap)
2027 Q3      Phase 7 (research platform)
2027 Q4+     Phase 8 (multi-sport, API, betting)
```

---

## 12. Business model

| Tier | Features |
| --- | --- |
| **Free** | One slate per week, basic optimizer, delayed ownership |
| **Pro** ($30–80/mo) | Full sim, MME, late swap, all NFL slates, backtest access |
| **Syndicate / API** ($200+/mo) | Bulk export, headless runs, higher rate limits, multi-user seats |
| **Content flywheel** | Public backtest blog posts, weekly leverage articles — SEO → subs |

---

## 13. What to optimize for

1. **Simulation + ownership** — not another vanilla optimizer.
2. **Your own historical archive** — salaries, ownership, contest results; vendors won't sell it.
3. **Multi-platform in the schema from day one** — `platform` column everywhere.
4. **Prove edge publicly** — backtest transparency builds trust.
5. **Speed on Sunday** — sub-minute sim reruns when news breaks.
6. **Portfolio thinking** — 150 lineups as one decision, not 150 clicks.

---

## 14. Compliance & platform terms

- DFS research/optimizer tools are generally legal in the US; do not facilitate underage
  gambling.
- Review each platform's Terms of Service regarding automated entry. CSV upload is typically
  allowed; botting the entry flow is not.
- Display disclaimers: past backtest performance does not guarantee future results.
- Store only user-consented data; GDPR/CCPA if serving EU/CA users.

---

## 15. Mapping from Phase 1 code to production

| Phase 1 module | Production role |
| --- | --- |
| `src/db.py` | SQLite dev adapter; schema source for Postgres migrations |
| `src/scoring.py` | Becomes `src/scoring/dk_nfl.py` |
| `src/ingest/crosswalk.py` | Unchanged core; adds platform-aware sources |
| `src/ingest/dk_salaries.py` | Becomes `src/platforms/dk/salaries.py` |
| `src/ingest/nfl_stats.py` | Stays; feeds analytics DB |
| `src/nflverse.py` | Stays; add sport-specific fetch modules later |
| `config.yaml` | Split: public config + `config.local.yaml` + env vars |

---

## 16. Definition of done (full product)

- [ ] Multi-platform (DK + FD minimum) NFL classic slates
- [ ] Simulator with ownership-aware field generation
- [ ] Sub-2-minute sim rerun on news break
- [ ] Automated weekly ingest with alerts
- [ ] Late swap support for NFL main slates
- [ ] Leakage-free backtest across ≥3 seasons
- [ ] 97%+ crosswalk coverage with automated warnings
- [ ] User auth, billing, tiered access
- [ ] Raw file archive for every source, every week
- [ ] Public backtest report with documented limitations

---

*This document is the planning north star. Implementation details live in phase-specific specs
as each phase begins.*
