"""Resolve a live DK pool to canonical GSIS ids.

T14, fixed. The crosswalk's reference used to be built from the slate's own
stat rows, which do not exist until the games are played, so a live export
could only be joined by this module's stop-gap: name + most-recent-team
against the ``players`` table. The reference now unions the week's
**rosters** (published before the games), so ``resolve_pool`` can hand a
live pool to the real waterfall (``src.ingest.crosswalk``) when told the
slate's week: manual overrides, the DST map, the persisted DK-id map, exact
name+team+position, name+position, then fuzzy within team and position --
and persist what it matched, so a DK id resolved once resolves by id from
then on.

Without ``season``/``week`` the original stop-gap runs unchanged (name +
most-recent stat team), which is what a caller without a schedule gets.
Either way ``gsis_id`` is ``None`` where nothing matched -- kickers always,
since they are not in the stats feed and there is nothing to project them
from -- and callers fall back to the pool's own projection rather than
erroring, because a live slate always has a few.

``team_changed`` is kept in both modes: True where the player's most recent
*stat* row is on a different team from the export. RoleAware projects from
the new team's depth chart, so it is information for the CLI, not a refusal.
"""

from __future__ import annotations

import pandas as pd

from src import db
from src import teams as teams_mod
from src.ingest import crosswalk
from src.ingest.crosswalk import normalize_name

SOURCE = "dk"


def _latest_team_by_player(conn) -> pd.DataFrame:
    """Each offensive player's most recent (season, week, team) on record."""
    return pd.read_sql_query(
        """
        SELECT s.player_id, p.name, p.position, s.team, s.season, s.week
        FROM player_week_stats s
        JOIN players p ON p.player_id = s.player_id
        WHERE p.position IN ('QB','RB','WR','TE')
        """,
        conn,
    )


def _latest_teams(conn) -> pd.DataFrame:
    history = _latest_team_by_player(conn)
    if history.empty:
        return history
    history = history.sort_values(["player_id", "season", "week"])
    latest = history.groupby("player_id").tail(1)
    return latest.assign(_norm_name=latest["name"].map(normalize_name),
                         _team=latest["team"].map(teams_mod.normalize_team))


def _flag_team_changes(out: pd.DataFrame, latest: pd.DataFrame) -> pd.DataFrame:
    if latest.empty:
        return out
    last_team = latest.drop_duplicates("player_id").set_index("player_id")["_team"]
    export_team = out["team"].map(teams_mod.normalize_team)
    known = out["gsis_id"].map(last_team)
    out["team_changed"] = (known.notna() & (known != export_team)).astype(bool)
    return out


def _resolve_by_crosswalk(conn, out: pd.DataFrame, season: int, week: int) -> pd.DataFrame:
    reference = crosswalk.build_reference(conn, season, week)
    if reference[reference["position"] != "DST"].empty:
        return out
    rows = pd.DataFrame({
        "source_name": out["name"], "source_id": out["player_id"].astype(str),
        "team": out["team"], "position": out["position"],
    }, index=out.index)
    result = crosswalk.resolve(rows, reference, source=SOURCE,
                               id_map=crosswalk.stored_id_map(conn, SOURCE),
                               manual=crosswalk.load_manual_overrides())
    out.loc[result.matched.index, "gsis_id"] = result.matched["player_id"]
    crosswalk.persist(conn, SOURCE, result.matched)
    return out


def _resolve_by_latest_team(conn, out: pd.DataFrame, latest: pd.DataFrame) -> pd.DataFrame:
    is_dst = out["position"] == "DST" if "position" in out.columns else pd.Series(False, index=out.index)
    offense = out[~is_dst].copy()
    if offense.empty or latest.empty:
        return out
    offense["_norm_name"] = offense["name"].map(normalize_name)
    offense["_team"] = offense["team"].map(teams_mod.normalize_team)
    by_name_team = latest.set_index(["_norm_name", "_team"])["player_id"]
    by_name = latest.drop_duplicates("_norm_name", keep=False).set_index("_norm_name")["player_id"]

    def match(row):
        key = (row["_norm_name"], row["_team"])
        if key in by_name_team.index:
            return by_name_team.loc[key]
        return by_name.get(row["_norm_name"])

    resolved = offense.apply(match, axis=1)
    out.loc[resolved.index, "gsis_id"] = resolved
    return out


def resolve_pool(conn, pool: pd.DataFrame, season: int | None = None,
                 week: int | None = None) -> pd.DataFrame:
    """Add ``gsis_id`` and ``team_changed`` to a pool frame.

    With ``season`` and ``week``: the real crosswalk against that week's
    rosters (T14). Without: the name + most-recent-team stop-gap.
    """
    out = pool.copy()
    out["gsis_id"] = None
    out["team_changed"] = False
    if out.empty:
        return out

    # DST resolves exactly in both modes: DK's team name -> canonical code ->
    # the synthetic id db.py's schema uses for defenses.
    is_dst = out["position"] == "DST" if "position" in out.columns else pd.Series(False, index=out.index)
    if is_dst.any():
        team = out.loc[is_dst, "team"].map(teams_mod.normalize_team)
        out.loc[is_dst, "gsis_id"] = "DST_" + team

    latest = _latest_teams(conn)
    if season is not None and week is not None:
        out = _resolve_by_crosswalk(conn, out, season, week)
    else:
        out = _resolve_by_latest_team(conn, out, latest)
    return _flag_team_changes(out, latest)
