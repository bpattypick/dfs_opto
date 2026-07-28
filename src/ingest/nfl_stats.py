"""nflverse -> players, games, player_week_stats, dst_week_stats (spec §5.1).

Run ``python -m src.ingest.nfl_stats`` to load the configured season range.

Ingestion is idempotent: every write is ``INSERT OR REPLACE`` keyed on the
table's primary key, so re-running never duplicates. ``dk_points`` is computed
from raw stats at ingest time and stored.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from src import config as config_mod
from src import db, nflverse, scoring
from src import teams as teams_mod
from src.ingest.crosswalk import normalize_name, normalize_position

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")

# DK Classic roster positions we care about. Kickers are not in DK NFL Classic.
FANTASY_POSITIONS = ("QB", "RB", "WR", "TE")


# --- games --------------------------------------------------------------------


def _kickoff_utc(gameday: str, gametime: str | None) -> str | None:
    """nflverse stores kickoff as a local ET date + HH:MM. Convert to UTC ISO."""
    if not gameday or pd.isna(gameday):
        return None
    if not gametime or pd.isna(gametime):
        return None
    try:
        naive = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=_ET).astimezone(_UTC).isoformat()


def load_games(conn, seasons: list[int], refresh: bool = False, cfg=None) -> int:
    raw = nflverse.games(refresh=refresh, cfg=cfg)
    frame = raw[raw["season"].isin(seasons)].copy()

    frame["home_team"] = frame["home_team"].map(teams_mod.normalize_team)
    frame["away_team"] = frame["away_team"].map(teams_mod.normalize_team)
    frame["kickoff_utc"] = [
        _kickoff_utc(d, t) for d, t in zip(frame["gameday"], frame["gametime"])
    ]

    cols = [
        "game_id", "season", "week", "home_team", "away_team",
        "kickoff_utc", "spread_line", "total_line", "home_score", "away_score",
    ]
    written = db.upsert_df(conn, "games", frame[cols])
    log.info("games: %d rows", written)
    return written


# --- player week stats --------------------------------------------------------

# nflverse stats_player_week column -> our schema column.
_PLAYER_COLUMN_MAP = {
    "passing_yards": "pass_yards",
    "passing_tds": "pass_tds",
    "attempts": "pass_attempts",
    "passing_interceptions": "interceptions",
    "rushing_yards": "rush_yards",
    "rushing_tds": "rush_tds",
    "carries": "carries",
    "receiving_yards": "rec_yards",
    "receiving_tds": "rec_tds",
    "receptions": "receptions",
    "targets": "targets",
    "special_teams_tds": "st_tds",
}

_FUMBLE_COLUMNS = ("rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost")
_TWO_PT_COLUMNS = (
    "passing_2pt_conversions",
    "rushing_2pt_conversions",
    "receiving_2pt_conversions",
)


def _numeric(frame: pd.DataFrame, columns) -> pd.Series:
    """Row-wise sum of the columns that exist, treating missing/NaN as 0."""
    present = [c for c in columns if c in frame.columns]
    if not present:
        return pd.Series(0.0, index=frame.index)
    return frame[present].apply(pd.to_numeric, errors="coerce").fillna(0.0).sum(axis=1)


def _transform_players(raw: pd.DataFrame, season_types: list[str]) -> pd.DataFrame:
    frame = raw[raw["season_type"].isin(season_types)].copy()
    frame = frame[frame["position"].isin(FANTASY_POSITIONS)]

    out = pd.DataFrame(index=frame.index)
    out["player_id"] = frame["player_id"]
    out["season"] = frame["season"].astype(int)
    out["week"] = frame["week"].astype(int)
    out["team"] = frame["team"].map(teams_mod.normalize_team)
    out["opponent"] = frame["opponent_team"].map(teams_mod.normalize_team)
    out["game_id"] = frame.get("game_id")

    for src_col, dest_col in _PLAYER_COLUMN_MAP.items():
        out[dest_col] = (
            pd.to_numeric(frame[src_col], errors="coerce").fillna(0.0)
            if src_col in frame.columns
            else 0.0
        )

    out["fumbles_lost"] = _numeric(frame, _FUMBLE_COLUMNS)
    out["two_pt"] = _numeric(frame, _TWO_PT_COLUMNS)
    out["dk_points"] = scoring.score_offense_frame(out)

    # Carry names/positions through for the players table and snap join.
    out["_name"] = frame["player_display_name"].fillna(frame["player_name"])
    out["_position"] = frame["position"].map(normalize_position)
    return out.dropna(subset=["player_id"])


# --- DST ----------------------------------------------------------------------


def _transform_dst(team_raw: pd.DataFrame, games: pd.DataFrame, season_types) -> pd.DataFrame:
    """Team-week defensive stats + points allowed from the final score."""
    frame = team_raw[team_raw["season_type"].isin(season_types)].copy()

    out = pd.DataFrame(index=frame.index)
    out["team"] = frame["team"].map(teams_mod.normalize_team)
    out["opponent"] = frame["opponent_team"].map(teams_mod.normalize_team)
    out["season"] = frame["season"].astype(int)
    out["week"] = frame["week"].astype(int)
    out["game_id"] = frame["game_id"]

    out["sacks"] = pd.to_numeric(frame.get("def_sacks"), errors="coerce").fillna(0.0)
    out["interceptions"] = pd.to_numeric(
        frame.get("def_interceptions"), errors="coerce"
    ).fillna(0).astype(int)
    out["fumbles_rec"] = pd.to_numeric(
        frame.get("fumble_recovery_opp"), errors="coerce"
    ).fillna(0).astype(int)
    out["def_tds"] = pd.to_numeric(frame.get("def_tds"), errors="coerce").fillna(0).astype(int)
    out["special_tds"] = pd.to_numeric(
        frame.get("special_teams_tds"), errors="coerce"
    ).fillna(0).astype(int)
    out["safeties"] = pd.to_numeric(frame.get("def_safeties"), errors="coerce").fillna(0).astype(int)
    # nflverse exposes blocked FGs/punts on the kicking team's row, not the
    # blocking defense's. Left at 0 until play-by-play attribution is added.
    out["blocked_kicks"] = 0

    # Points allowed = opponent's final score. See scoring.score_dst for the
    # documented limitation.
    scores = games.set_index("game_id")[["home_team", "away_team", "home_score", "away_score"]]
    joined = out.join(scores, on="game_id")
    points_allowed = pd.Series(pd.NA, index=out.index, dtype="Float64")
    is_home = joined["home_team"] == joined["team"]
    points_allowed[is_home] = joined.loc[is_home, "away_score"]
    is_away = joined["away_team"] == joined["team"]
    points_allowed[is_away] = joined.loc[is_away, "home_score"]
    out["points_allowed"] = points_allowed

    # Games without a final score (not yet played) can't be scored.
    out = out[out["points_allowed"].notna()].copy()
    out["points_allowed"] = out["points_allowed"].astype(int)
    out["dk_points"] = scoring.score_dst_frame(out)
    return out.dropna(subset=["team"])


def _dst_as_player_rows(dst: pd.DataFrame) -> pd.DataFrame:
    """Mirror scored DST rows into player_week_stats so every roster slot is uniform."""
    out = pd.DataFrame(index=dst.index)
    out["player_id"] = dst["team"].map(teams_mod.dst_player_id)
    out["season"] = dst["season"]
    out["week"] = dst["week"]
    out["team"] = dst["team"]
    out["opponent"] = dst["opponent"]
    out["game_id"] = dst["game_id"]
    out["dk_points"] = dst["dk_points"]
    for col in ("snaps", "snap_pct", "targets", "carries", "receptions", "rec_yards",
                "rec_tds", "rush_yards", "rush_tds", "pass_attempts", "pass_yards",
                "pass_tds", "interceptions", "fumbles_lost", "two_pt", "st_tds"):
        out[col] = None
    out["_name"] = dst["team"].map(
        lambda t: teams_mod.BY_ABBR[t].full_name if t in teams_mod.BY_ABBR else t
    )
    out["_position"] = "DST"
    return out


# --- snap counts --------------------------------------------------------------


def _attach_snaps(stats: pd.DataFrame, seasons: list[int]) -> pd.DataFrame:
    """Join offensive snap counts on (normalized name, team, season, week).

    ``import_snap_counts`` keys on PFR IDs, and ``import_ids`` only covers
    ~7.5k mostly-active players, so a name+team+week join reaches further back
    than a pfr_id->gsis_id join would. Failures leave NULL snaps rather than
    blocking the ingest — snaps are a usage nicety, not a scoring input.
    """
    import nfl_data_py as nfl

    try:
        snaps = nfl.import_snap_counts(seasons)
    except Exception as exc:  # noqa: BLE001
        log.warning("snap counts unavailable (%s); leaving snaps NULL", exc)
        stats["snaps"] = None
        stats["snap_pct"] = None
        return stats

    snaps = snaps.copy()
    snaps["_key_name"] = snaps["player"].map(normalize_name)
    snaps["_key_team"] = snaps["team"].map(teams_mod.normalize_team)
    snaps = snaps.dropna(subset=["_key_team"])
    snaps = snaps.drop_duplicates(subset=["_key_name", "_key_team", "season", "week"])

    stats = stats.copy()
    stats["_key_name"] = stats["_name"].map(normalize_name)
    merged = stats.merge(
        snaps[["_key_name", "_key_team", "season", "week", "offense_snaps", "offense_pct"]],
        left_on=["_key_name", "team", "season", "week"],
        right_on=["_key_name", "_key_team", "season", "week"],
        how="left",
    )
    merged["snaps"] = merged["offense_snaps"]
    merged["snap_pct"] = merged["offense_pct"]

    hit = merged["snaps"].notna().sum()
    log.info("snap counts joined for %d/%d player-weeks (%.1f%%)",
             hit, len(merged), 100 * hit / max(len(merged), 1))
    return merged.drop(columns=["_key_name", "_key_team", "offense_snaps", "offense_pct"])


# --- players ------------------------------------------------------------------


def _players_table(stats: pd.DataFrame) -> pd.DataFrame:
    grouped = (
        stats.groupby("player_id")
        .agg(name=("_name", "last"), position=("_position", "last"),
             first_season=("season", "min"))
        .reset_index()
    )
    return grouped


# --- orchestration ------------------------------------------------------------


def ingest(conn, seasons: list[int], refresh: bool = False, cfg=None) -> dict[str, int]:
    cfg = cfg or config_mod.load()
    season_types = cfg.get("seasons.season_types", ["REG"])

    load_games(conn, seasons, refresh=refresh, cfg=cfg)
    games = pd.read_sql_query(
        "SELECT game_id, home_team, away_team, home_score, away_score FROM games", conn
    )

    player_frames, dst_frames = [], []
    for season in seasons:
        try:
            raw = nflverse.player_week(season, refresh=refresh, cfg=cfg)
        except nflverse.NflverseUnavailable as exc:
            log.warning("skipping player stats for %s: %s", season, exc)
        else:
            player_frames.append(_transform_players(raw, season_types))

        try:
            team_raw = nflverse.team_week(season, refresh=refresh, cfg=cfg)
        except nflverse.NflverseUnavailable as exc:
            log.warning("skipping team stats for %s: %s", season, exc)
        else:
            dst_frames.append(_transform_dst(team_raw, games, season_types))

    if not player_frames:
        raise RuntimeError(f"no player stats could be loaded for seasons {seasons}")

    stats = pd.concat(player_frames, ignore_index=True)
    stats = _attach_snaps(stats, seasons)

    dst = pd.concat(dst_frames, ignore_index=True) if dst_frames else pd.DataFrame()
    if len(dst):
        stats = pd.concat([stats, _dst_as_player_rows(dst)], ignore_index=True)

    counts = {
        "player_week_stats": db.upsert_df(conn, "player_week_stats", stats),
        "dst_week_stats": db.upsert_df(conn, "dst_week_stats", dst) if len(dst) else 0,
        "players": db.upsert_df(conn, "players", _players_table(stats)),
    }
    for table, n in counts.items():
        log.info("%s: %d rows", table, n)
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ingest nflverse stats into SQLite.")
    parser.add_argument("--season", type=int, action="append",
                        help="season to ingest (repeatable); defaults to config range")
    parser.add_argument("--refresh", action="store_true",
                        help="re-download instead of using data/raw/nflverse archives")
    parser.add_argument("--db", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    cfg = config_mod.load()
    seasons = args.season or cfg.seasons
    with db.session(args.db) as conn:
        db.create_schema(conn)
        ingest(conn, seasons, refresh=args.refresh, cfg=cfg)
        for table, count in db.table_counts(conn).items():
            print(f"  {table:<20} {count:>9,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
