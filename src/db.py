"""SQLite connection, schema creation, and idempotent upsert helpers.

Run ``python -m src.db`` to build an empty ``data/dfs.sqlite`` (Milestone 1).

Canonical player ID is the GSIS ID (e.g. ``00-0036322``). Team defenses have no
GSIS ID, so they use the synthetic key ``DST_<TEAM>`` (e.g. ``DST_KC``) — see
docs/data-sources.md.
"""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from src import config as config_mod

# Spec §3, with two documented additions marked below.
SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    player_id    TEXT PRIMARY KEY,   -- GSIS ID, or DST_<TEAM> for defenses
    name         TEXT,
    position     TEXT,               -- QB/RB/WR/TE/DST
    first_season INTEGER
);

CREATE TABLE IF NOT EXISTS id_crosswalk (
    player_id    TEXT,               -- canonical GSIS
    source       TEXT,               -- 'dk', 'vendorA', 'oddsapi', 'espn', ...
    source_id    TEXT,               -- vendor's ID if they have one ('' if none)
    source_name  TEXT,               -- name exactly as the vendor spells it
    match_method TEXT,               -- 'manual'|'id_map'|'exact'|'fuzzy'
    PRIMARY KEY (source, source_name, source_id)
);

CREATE TABLE IF NOT EXISTS games (
    game_id     TEXT PRIMARY KEY,    -- nflverse game_id, e.g. 2025_01_GB_CHI
    season      INTEGER,
    week        INTEGER,
    home_team   TEXT,
    away_team   TEXT,
    kickoff_utc TEXT,
    spread_line REAL,                -- closing spread (home perspective)
    total_line  REAL,
    -- ADDITION (not in spec §3): final scores. Needed to derive DST points
    -- allowed, and free in the same source row.
    home_score  INTEGER,
    away_score  INTEGER
);

CREATE TABLE IF NOT EXISTS player_week_stats (
    player_id      TEXT,
    season         INTEGER,
    week           INTEGER,
    team           TEXT,
    opponent       TEXT,
    game_id        TEXT,
    -- usage
    snaps          INTEGER,
    snap_pct       REAL,
    targets        INTEGER,
    carries        INTEGER,
    -- production
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
    -- ADDITION (not in spec §3): return TDs are worth 6 real DK points, so
    -- dk_points is wrong for return men without this column.
    st_tds         INTEGER,
    -- computed
    dk_points      REAL,
    PRIMARY KEY (player_id, season, week)
);

CREATE TABLE IF NOT EXISTS dst_week_stats (
    -- ADDITION (not in spec §3): DK requires a DST roster slot, and team
    -- defense stats do not fit the player_week_stats columns. Scored rows are
    -- mirrored into player_week_stats under player_id = 'DST_<TEAM>' so the
    -- optimizer and backtest can treat every roster slot uniformly.
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

CREATE TABLE IF NOT EXISTS salaries (
    slate_id        TEXT,            -- e.g. '2025-w08-main'
    season          INTEGER,
    week            INTEGER,
    player_id       TEXT,            -- canonical (via crosswalk); NULL if unmatched
    dk_name         TEXT,            -- name as DK spells it
    dk_salary       INTEGER,
    roster_position TEXT,            -- QB/RB/WR/TE/FLEX/DST
    team            TEXT,
    opponent        TEXT,
    -- ADDITIONS: DK ships both in every export and both were being discarded.
    -- status is OUT/IR/Q (also the free late-swap signal, roadmap Step 4);
    -- avg_points is DK's AvgPointsPerGame, the interim projection (H7).
    status          TEXT,
    avg_points      REAL,
    PRIMARY KEY (slate_id, dk_name)
);

CREATE TABLE IF NOT EXISTS projections (
    source         TEXT,
    season         INTEGER,
    week           INTEGER,
    player_id      TEXT,
    proj_points    REAL,
    proj_ownership REAL,             -- NULL if vendor doesn't provide
    pulled_at      TEXT,             -- ISO timestamp — leakage guard
    PRIMARY KEY (source, season, week, player_id)
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    game_id   TEXT,
    book      TEXT,
    market    TEXT,                  -- 'spread' | 'total' | player props later
    line      REAL,
    price     INTEGER,
    pulled_at TEXT,
    PRIMARY KEY (game_id, book, market, pulled_at)
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id       TEXT PRIMARY KEY,
    created_at   TEXT,
    config_json  TEXT,               -- full config for reproducibility
    metrics_json TEXT
);

CREATE TABLE IF NOT EXISTS entries (
    -- Experiment ledger, one row per contest entry (roadmap v2 Step 1). This
    -- is the measurement layer: without it, process improvements and variance
    -- are indistinguishable. Written by src/ledger.py.
    entry_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date                TEXT NOT NULL,   -- entry date, ISO
    slate_id            TEXT NOT NULL,   -- same convention as salaries.slate_id
    contest_id          TEXT,
    contest_type        TEXT NOT NULL,   -- showdown_gpp/showdown_cash/single_entry/...
    field_size          INTEGER,
    entry_fee           REAL,            -- 0 or NULL for free contests
    payout_structure_id TEXT,            -- links to a saved payout table (T6)
    lineup              TEXT NOT NULL,   -- json list of names; first element is CPT
    model_version       TEXT NOT NULL,   -- git commit hash that built the lineup
    sim_mean            REAL,
    sim_ceiling         REAL,
    chalk_score         REAL,
    dup_estimate        REAL,            -- populated by T7
    -- settled after the contest
    actual_score        REAL,
    finish_rank         INTEGER,
    payout              REAL,
    roi                 REAL
);

CREATE INDEX IF NOT EXISTS idx_pws_season_week  ON player_week_stats (season, week);
CREATE INDEX IF NOT EXISTS idx_pws_team         ON player_week_stats (season, week, team);
CREATE INDEX IF NOT EXISTS idx_games_season_wk  ON games (season, week);
CREATE INDEX IF NOT EXISTS idx_salaries_slate   ON salaries (season, week);
CREATE INDEX IF NOT EXISTS idx_proj_season_week ON projections (season, week);
CREATE INDEX IF NOT EXISTS idx_xwalk_player     ON id_crosswalk (player_id);
CREATE INDEX IF NOT EXISTS idx_entries_report   ON entries (contest_type, model_version);
"""

TABLES = (
    "players",
    "id_crosswalk",
    "games",
    "player_week_stats",
    "dst_week_stats",
    "salaries",
    "projections",
    "odds_snapshots",
    "backtest_runs",
    "entries",
)


def db_path(cfg: config_mod.Config | None = None) -> Path:
    cfg = cfg or config_mod.load()
    return cfg.path("db")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection with sane pragmas and dict-like rows."""
    path = Path(path) if path is not None else db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """Connection context manager that commits on success, rolls back on error."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added to tables after the first release. CREATE TABLE IF NOT EXISTS
# will not add them to a database that already exists, and the resulting
# "no such column" at ingest time is a confusing way to find that out.
_ADDED_COLUMNS = {
    "salaries": (("status", "TEXT"), ("avg_points", "REAL")),
}


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, columns in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")


def upsert(
    conn: sqlite3.Connection,
    table: str,
    rows: Sequence[dict] | Iterable[dict],
    columns: Sequence[str] | None = None,
) -> int:
    """``INSERT OR REPLACE`` a batch of dict rows. Idempotent by primary key.

    Re-running any ingest must never duplicate (spec §5.1), which is what makes
    the weekly refresh safe to run repeatedly.
    """
    rows = list(rows)
    if not rows:
        return 0
    cols = list(columns) if columns else list(rows[0].keys())
    placeholders = ", ".join("?" for _ in cols)
    sql = (
        f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    )
    payload = [tuple(row.get(c) for c in cols) for row in rows]
    conn.executemany(sql, payload)
    return len(payload)


def upsert_df(conn: sqlite3.Connection, table: str, df, columns: Sequence[str] | None = None) -> int:
    """``upsert`` for a pandas DataFrame, keeping only real table columns.

    NaN becomes NULL so SQLite never stores the float sentinel.
    """
    import pandas as pd

    if df is None or len(df) == 0:
        return 0
    table_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    cols = list(columns) if columns else [c for c in df.columns if c in table_cols]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"{table}: DataFrame is missing columns {missing}")
    frame = df[cols].astype(object).where(pd.notnull(df[cols]), None)
    return upsert(conn, table, frame.to_dict("records"), cols)


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create the DFS SQLite schema.")
    parser.add_argument("--db", default=None, help="override the configured db path")
    args = parser.parse_args(argv)

    path = Path(args.db) if args.db else db_path()
    with session(path) as conn:
        create_schema(conn)
        counts = table_counts(conn)

    print(f"schema ready: {path}")
    for table, count in counts.items():
        print(f"  {table:<20} {count:>9,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
