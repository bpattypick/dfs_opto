"""Resolve a live DK pool to canonical GSIS ids — the minimal live-slate wedge.

T14: the crosswalk cannot resolve a forward-looking slate, because its
reference is built from ``player_week_stats`` for the slate's own week, which
does not exist until the games are played. That is still true and still needs
a real fix. This module is a narrower, stated-scope workaround for one purpose:
join a DK Showdown pool to GSIS ids so the validated projection (T18) and score
model (T13) can be used on tonight's slate, rather than falling back to
``AvgPointsPerGame`` for players who resolve fine.

Matching is name + most-recent-team, using the same normalization the real
crosswalk uses (``crosswalk.normalize_name``), against the ``players`` table —
season-agnostic, so it does not depend on the target week's own stats existing.
DST resolves exactly, by construction: DK's team name maps through
``teams.normalize_team`` to the ``DST_<TEAM>`` synthetic id from db.py's
schema, no fuzzy matching involved. Kickers are not in the stats feed
(FANTASY_POSITIONS excludes K) and are never resolved — every caller must
handle an unresolved player by falling back to the pool's own projection,
not by erroring, since K and a real fraction of WR/RB depth chart churn are
expected to miss.
"""

from __future__ import annotations

import pandas as pd

from src import db
from src import teams as teams_mod
from src.ingest.crosswalk import normalize_name


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


def resolve_pool(conn, pool: pd.DataFrame) -> pd.DataFrame:
    """Add a ``gsis_id`` column to a pool frame (``player_id, name, team, ...``).

    ``gsis_id`` is ``None`` where nothing matched — kickers always, plus any
    offensive player whose name+team didn't join (a mid-season trade, a name
    spelled differently by DK, a rookie's first snap before their first
    ``player_week_stats`` row exists). Callers use the pool's own projection
    for those rather than treating a miss as fatal: a live slate always has a
    few, and refusing to build a lineup over a kicker is not the goal.
    """
    out = pool.copy()
    out["gsis_id"] = None
    out["team_changed"] = False

    # DST resolves exactly: DK's team name -> canonical abbreviation -> the
    # synthetic id db.py's schema uses for defenses. No name matching at all.
    is_dst = out["position"] == "DST" if "position" in out.columns else pd.Series(False, index=out.index)
    if is_dst.any():
        team = out.loc[is_dst, "team"].map(teams_mod.normalize_team)
        out.loc[is_dst, "gsis_id"] = "DST_" + team

    offense = out[~is_dst].copy() if is_dst.any() else out.copy()
    if offense.empty:
        return out

    history = _latest_team_by_player(conn)
    if history.empty:
        return out
    history = history.sort_values(["player_id", "season", "week"])
    latest = history.groupby("player_id").tail(1)
    latest = latest.assign(_norm_name=latest["name"].map(normalize_name),
                           _team=latest["team"].map(teams_mod.normalize_team))

    offense["_norm_name"] = offense["name"].map(normalize_name)
    offense["_team"] = offense["team"].map(teams_mod.normalize_team)

    # Prefer a name+team match (handles two players sharing a surname); fall
    # back to name-only for a player whose team in our history is stale
    # relative to a preseason trade the export already reflects.
    by_name_team = latest.set_index(["_norm_name", "_team"])["player_id"]
    by_name = latest.drop_duplicates("_norm_name", keep=False).set_index("_norm_name")["player_id"]

    def match(row):
        key = (row["_norm_name"], row["_team"])
        if key in by_name_team.index:
            return by_name_team.loc[key]
        return by_name.get(row["_norm_name"])

    resolved = offense.apply(match, axis=1)
    out.loc[resolved.index, "gsis_id"] = resolved

    # A trailing average is a snapshot of the ROLE a player held when the
    # history was recorded, not just their name. A player who has changed
    # teams since our last ingested season carries that old role's production
    # into the average with nothing to say it no longer applies. This is not
    # hypothetical: on the first live slate this ran on, it silently gave a
    # bench QB (a full-time starter elsewhere through late 2025, now a
    # third-string arm) a plausible 14.6-point projection, and a second
    # bench QB 7.9 points off a single 2022 start at a different team. Flag it
    # rather than trust it — `out["team_changed"]` is True wherever the
    # player's most recent known team differs from the export's team, so
    # every caller can decide, but none can silently miss it.
    matched_latest = latest.set_index("player_id")[["_team"]]
    for idx, gsis in resolved.items():
        if pd.isna(gsis) or gsis not in matched_latest.index:
            continue
        last_team = matched_latest.loc[gsis, "_team"]
        if isinstance(last_team, pd.Series):  # duplicate rows, defensively
            last_team = last_team.iloc[0]
        out.loc[idx, "team_changed"] = last_team != offense.loc[idx, "_team"]
    return out
