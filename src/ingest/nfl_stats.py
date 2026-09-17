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

    def column(name: str) -> pd.Series:
        # A missing optional column is all-zero, not a scalar NaN that breaks .fillna.
        raw = frame[name] if name in frame.columns else pd.Series(pd.NA, index=frame.index)
        return pd.to_numeric(raw, errors="coerce").fillna(0)

    out["sacks"] = column("def_sacks").astype(float)
    out["interceptions"] = column("def_interceptions").astype(int)
    out["fumbles_rec"] = column("fumble_recovery_opp").astype(int)
    out["def_tds"] = column("def_tds").astype(int)
    out["special_tds"] = column("special_teams_tds").astype(int)
    out["safeties"] = column("def_safeties").astype(int)
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


def _attach_snaps(
    stats: pd.DataFrame, seasons: list[int], refresh: bool = False, cfg=None
) -> pd.DataFrame:
    """Join offensive snap counts on (normalized name, team, season, week).

    The snap-count release keys on PFR IDs, and ``import_ids`` only covers
    ~7.5k mostly-active players, so a name+team+week join reaches further back
    than a pfr_id->gsis_id join would. Fetched one season at a time through
    :mod:`src.nflverse` (archived, dated for a live season) so that a single
    unpublished season leaves only *its own* rows NULL — the previous
    all-seasons call would have NULLed every season's snaps on the next
    re-ingest if any one year failed. Snaps are a usage input, not a scoring
    input, so a miss warns rather than blocks.
    """
    frames = []
    for season in seasons:
        try:
            frames.append(nflverse.snap_counts(season, refresh=refresh, cfg=cfg))
        except nflverse.NflverseUnavailable as exc:
            log.warning("snap counts unavailable for %s (%s); leaving that season's "
                        "snaps NULL", season, exc)
    if not frames:
        stats["snaps"] = None
        stats["snap_pct"] = None
        return stats

    snaps = pd.concat(frames, ignore_index=True)
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


# --- rosters (T23) ------------------------------------------------------------

# The dressed game-day roster. Every stat-recording player-week in 2024 carried
# this status, so it is exactly the set of players who could have scored.
ROSTER_STATUS_ACTIVE = "ACT"


def _transform_rosters(raw: pd.DataFrame, season_types: list[str]) -> pd.DataFrame:
    frame = raw[raw["game_type"].isin(season_types)].copy()
    frame["position"] = frame["position"].map(normalize_position)
    frame = frame[frame["position"].isin(FANTASY_POSITIONS)]
    frame = frame.dropna(subset=["gsis_id"])

    out = pd.DataFrame({
        "player_id": frame["gsis_id"],
        "season": frame["season"].astype(int),
        "week": frame["week"].astype(int),
        "team": frame["team"].map(teams_mod.normalize_team),
        "position": frame["position"],
        "status": frame["status"],
        "depth_position": frame["depth_chart_position"]
        if "depth_chart_position" in frame.columns else None,
        "name": frame["full_name"],
    })
    # A player dresses for one team in a week. The 2024 feed has no duplicate
    # (player, week) rows; if a mid-week trade ever produces one, keep the last
    # so the primary key holds rather than failing the whole season.
    out = out.drop_duplicates(subset=["player_id", "season", "week"], keep="last")
    return out.dropna(subset=["team"])


def load_rosters(conn, seasons: list[int], refresh: bool = False, cfg=None) -> int:
    """Weekly roster status for ``seasons``. A missing season warns and is skipped."""
    cfg = cfg or config_mod.load()
    season_types = cfg.get("seasons.season_types", ["REG"])
    frames = []
    for season in seasons:
        try:
            raw = nflverse.weekly_rosters(season, refresh=refresh, cfg=cfg)
        except nflverse.NflverseUnavailable as exc:
            log.warning("skipping rosters for %s: %s", season, exc)
            continue
        frames.append(_transform_rosters(raw, season_types))
    if not frames:
        return 0
    return db.upsert_df(conn, "rosters", pd.concat(frames, ignore_index=True))


# --- roles: depth charts + injury reports (T22) --------------------------------

INJURY_STATUSES = ("Out", "Doubtful", "Questionable")
# A daily depth-chart snapshot is attached to a kickoff at most this far ahead.
DEPTH_SNAPSHOT_MAX_AGE = pd.Timedelta(days=8)


def _depth_from_weekly_format(frame: pd.DataFrame, season_types: list[str]) -> pd.DataFrame:
    """Through 2024: one row per (week, player, slot); depth_team is the rank.

    Starters at a multi-slot position tie (two WRs at depth_team 1). A player
    listed in several slots keeps his best rank.
    """
    f = frame[frame["game_type"].isin(season_types)].copy()
    f = f[f["formation"].astype(str).str.strip().str.lower() == "offense"]
    f["position"] = f["position"].map(normalize_position)
    f = f[f["position"].isin(FANTASY_POSITIONS)].dropna(subset=["gsis_id", "week"])
    f["depth_rank"] = pd.to_numeric(f["depth_team"], errors="coerce")
    f = f.dropna(subset=["depth_rank"])
    out = pd.DataFrame({
        "player_id": f["gsis_id"],
        "season": f["season"].astype(int),
        "week": f["week"].astype(int),
        "team": f["club_code"].map(teams_mod.normalize_team),
        "position": f["position"],
        "depth_rank": f["depth_rank"].astype(int),
        "depth_as_of": "week",
    })
    return (out.sort_values("depth_rank")
               .drop_duplicates(subset=["player_id", "season", "week"], keep="first"))


def _depth_from_daily_format(frame: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    """From 2025: daily dated snapshots. Use the last one before each team's kickoff.

    pos_rank is an ordinal within the position group across slots, so a
    player's rank is his minimum over the rows of one snapshot. Teams on bye
    have no kickoff that week and get no rows -- they are not in any pool.
    """
    f = frame.copy()
    f["dt"] = pd.to_datetime(f["dt"], utc=True, errors="coerce")
    f["position"] = f["pos_abb"].map(normalize_position)
    f = f[f["position"].isin(FANTASY_POSITIONS)].dropna(subset=["gsis_id", "dt"])
    f["team"] = f["team"].map(teams_mod.normalize_team)
    f = f.dropna(subset=["team"])

    kicks = games.dropna(subset=["kickoff_utc"]).copy()
    kicks["kickoff"] = pd.to_datetime(kicks["kickoff_utc"], utc=True, errors="coerce")
    kicks = pd.concat([
        kicks[["season", "week", "home_team", "kickoff"]].rename(columns={"home_team": "team"}),
        kicks[["season", "week", "away_team", "kickoff"]].rename(columns={"away_team": "team"}),
    ]).dropna(subset=["kickoff"]).sort_values("kickoff")

    snaps = f[["team", "dt"]].drop_duplicates().sort_values("dt")
    # For each (team, game): the newest snapshot strictly before kickoff, and
    # no older than a week -- a snapshot describes the coming game, not every
    # game left on the schedule. (The live season's newest snapshot would
    # otherwise be pinned to all remaining weeks until each is republished.)
    chosen = pd.merge_asof(kicks, snaps, left_on="kickoff", right_on="dt", by="team",
                           direction="backward", allow_exact_matches=False,
                           tolerance=DEPTH_SNAPSHOT_MAX_AGE)
    chosen = chosen.dropna(subset=["dt"])[["season", "week", "team", "dt"]]

    rows = f.merge(chosen, on=["team", "dt"], how="inner")
    rows["depth_rank"] = pd.to_numeric(rows["pos_rank"], errors="coerce")
    rows = rows.dropna(subset=["depth_rank"])
    out = pd.DataFrame({
        "player_id": rows["gsis_id"],
        "season": rows["season"].astype(int),
        "week": rows["week"].astype(int),
        "team": rows["team"],
        "position": rows["position"],
        "depth_rank": rows["depth_rank"].astype(int),
        "depth_as_of": rows["dt"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    return (out.sort_values("depth_rank")
               .drop_duplicates(subset=["player_id", "season", "week"], keep="first"))


def _transform_depth_charts(raw: pd.DataFrame, games: pd.DataFrame, season_types) -> pd.DataFrame:
    if "dt" in raw.columns:
        return _depth_from_daily_format(raw, games)
    return _depth_from_weekly_format(raw, season_types)


def _transform_injuries(raw: pd.DataFrame, season_types: list[str]) -> pd.DataFrame:
    f = raw[raw["game_type"].isin(season_types)].copy()
    f = f.dropna(subset=["gsis_id", "week"])
    f["report_status"] = f["report_status"].where(f["report_status"].isin(INJURY_STATUSES))
    out = pd.DataFrame({
        "player_id": f["gsis_id"],
        "season": f["season"].astype(int),
        "week": f["week"].astype(int),
        "team": f["team"].map(teams_mod.normalize_team),
        "position": f["position"].map(normalize_position),
        "injury_status": f["report_status"],
        "practice_status": f["practice_status"].astype(str).str.strip().replace({"": None, "None": None, "nan": None}),
    })
    # Keep the most severe listing if a player appears twice in a week.
    severity = {"Out": 0, "Doubtful": 1, "Questionable": 2}
    out["_sev"] = out["injury_status"].map(severity).fillna(3)
    return (out.sort_values("_sev").drop_duplicates(subset=["player_id", "season", "week"], keep="first")
               .drop(columns="_sev"))


def _merge_roles(depth: pd.DataFrame, injuries: pd.DataFrame) -> pd.DataFrame:
    """One row per (player, week): depth chart rank and/or injury listing."""
    keys = ["player_id", "season", "week"]
    merged = depth.merge(injuries, on=keys, how="outer", suffixes=("", "_inj"))
    for col in ("team", "position"):
        if f"{col}_inj" in merged.columns:
            merged[col] = merged[col].fillna(merged[f"{col}_inj"])
            merged = merged.drop(columns=f"{col}_inj")
    for col in ("depth_rank", "depth_as_of", "injury_status", "practice_status"):
        if col not in merged.columns:
            merged[col] = None
    merged = merged[merged["position"].isin(FANTASY_POSITIONS)]
    return merged.dropna(subset=["team"])[
        keys + ["team", "position", "depth_rank", "depth_as_of", "injury_status", "practice_status"]
    ]


def load_roles(conn, seasons: list[int], refresh: bool = False, cfg=None) -> int:
    """Depth chart + injury report per (player, week). A missing file warns and is skipped."""
    cfg = cfg or config_mod.load()
    season_types = cfg.get("seasons.season_types", ["REG"])
    games = pd.read_sql_query(
        "SELECT season, week, home_team, away_team, kickoff_utc FROM games", conn
    )
    frames = []
    for season in seasons:
        try:
            depth = _transform_depth_charts(
                nflverse.depth_charts(season, refresh=refresh, cfg=cfg),
                games[games["season"] == season], season_types,
            )
        except nflverse.NflverseUnavailable as exc:
            log.warning("skipping depth charts for %s: %s", season, exc)
            depth = pd.DataFrame(columns=["player_id", "season", "week", "team", "position",
                                          "depth_rank", "depth_as_of"])
        try:
            inj = _transform_injuries(nflverse.injuries(season, refresh=refresh, cfg=cfg),
                                      season_types)
        except nflverse.NflverseUnavailable as exc:
            log.warning("skipping injuries for %s: %s", season, exc)
            inj = pd.DataFrame(columns=["player_id", "season", "week", "team", "position",
                                        "injury_status", "practice_status"])
        if depth.empty and inj.empty:
            continue
        frames.append(_merge_roles(depth, inj))
    if not frames:
        return 0
    return db.upsert_df(conn, "roles", pd.concat(frames, ignore_index=True))


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
    stats = _attach_snaps(stats, seasons, refresh=refresh, cfg=cfg)

    dst = pd.concat(dst_frames, ignore_index=True) if dst_frames else pd.DataFrame()
    if len(dst):
        stats = pd.concat([stats, _dst_as_player_rows(dst)], ignore_index=True)

    counts = {
        "player_week_stats": db.upsert_df(conn, "player_week_stats", stats),
        "dst_week_stats": db.upsert_df(conn, "dst_week_stats", dst) if len(dst) else 0,
        "players": db.upsert_df(conn, "players", _players_table(stats)),
        "rosters": load_rosters(conn, seasons, refresh=refresh, cfg=cfg),
        "roles": load_roles(conn, seasons, refresh=refresh, cfg=cfg),
    }
    for table, n in counts.items():
        log.info("%s: %d rows", table, n)
    return counts


# --- currency check -----------------------------------------------------------

# A week is complete once its last kickoff is this far in the past.
_GAME_LENGTH = timedelta(hours=4)


def ingest_status(conn, season: int, now: datetime | None = None) -> dict:
    """What the database holds for ``season`` versus what has been played.

    T21: the projection is only as current as the last ingested week, and
    nflverse publishes a week's stats a day or two after it ends. This is
    the check the live path runs before building anything, so a completed
    week that is still unpublished is said out loud rather than silently
    left out of every trailing average.
    """
    now = now or datetime.now(tz=_UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_UTC)

    weeks = pd.read_sql_query(
        "SELECT DISTINCT week FROM player_week_stats WHERE season = ? ORDER BY week",
        conn, params=(season,),
    )
    ingested = [int(w) for w in weeks["week"]]

    games = pd.read_sql_query(
        "SELECT week, kickoff_utc FROM games WHERE season = ?", conn, params=(season,)
    )
    completed: list[int] = []
    if len(games):
        games["kick"] = pd.to_datetime(games["kickoff_utc"], utc=True, errors="coerce")
        last_kick = games.groupby("week")["kick"].max()
        completed = sorted(
            int(w) for w, k in last_kick.items() if pd.notna(k) and k + _GAME_LENGTH < now
        )

    rosters = pd.read_sql_query(
        "SELECT MAX(week) AS w FROM rosters WHERE season = ?", conn, params=(season,)
    )
    last_roster = rosters["w"].iloc[0]
    roles = pd.read_sql_query(
        "SELECT MAX(week) AS w FROM roles WHERE season = ?", conn, params=(season,)
    )
    last_role = roles["w"].iloc[0]

    return {
        "season": season,
        "ingested_weeks": ingested,
        "last_ingested_week": max(ingested) if ingested else None,
        "last_completed_week": max(completed) if completed else None,
        "missing_weeks": [w for w in completed if w not in ingested],
        "last_roster_week": int(last_roster) if pd.notna(last_roster) else None,
        "last_role_week": int(last_role) if pd.notna(last_role) else None,
    }


def format_status(status: dict) -> str:
    li = status["last_ingested_week"]
    lc = status["last_completed_week"]
    lr = status.get("last_roster_week")
    lo = status.get("last_role_week")
    line = (f"{status['season']}: ingested through week {li if li is not None else '-'}; "
            f"completed through week {lc if lc is not None else '-'}; "
            f"rosters through week {lr if lr is not None else '-'}; "
            f"depth charts/injuries through week {lo if lo is not None else '-'}")
    if status["missing_weeks"]:
        line += (f"\n!! completed week(s) {status['missing_weeks']} not yet published by "
                 "nflverse (usually by Tuesday) -- re-run before building a lineup")
    return line


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
        for season in seasons:
            if nflverse.is_live_season(season):
                print(format_status(ingest_status(conn, season)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
