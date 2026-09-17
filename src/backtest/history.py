"""Time-boxed access to history — the leakage guarantee, made structural.

CLAUDE.md principle 1: at week W, only data knowable before W's lock may
influence W's output. Rather than trusting every model to respect that, the
harness hands a model two frames and nothing else:

- ``as_of(season, week)`` — every stat row *strictly before* (season, week).
- ``slate(season, week)``  — who is playing in week W and what was knowable
  pre-lock: position, team, opponent, and the game's Vegas implied total.
  **No scores.** ``actuals()`` is a separate call the model never sees.

A model that only receives these cannot look ahead. The spec's spot-check
(delete week W and later, confirm week W's output is unchanged) is a test in
tests/test_backtest.py rather than a manual step.

Two pools (T23). ``pool="played"`` is the original: everyone with a stat row
in week W — which quietly conditions the whole backtest on having played, the
one thing a pre-lock projection cannot know. ``pool="roster"`` is every
QB/RB/WR/TE who *dressed* (roster status ACT) for a team with a game that
week, plus both DSTs: the pool a Showdown entrant actually faces at lock,
inactives already announced. A rostered player with no stat line scored 0,
and the harness scores the model on that.

Roles (T22). In roster mode both frames carry the role a player held going
into each week: ``depth_rank`` (the feed's number), ``eff_rank`` (his ordinal
among *dressed* teammates at the position, ties broken by prior snap share —
the comparable quantity across the two depth-chart formats) and
``injury_status``. Roster-mode history also contains a **zero row** for every
week a player dressed and recorded no stat line, so a backup's trailing
average is what he actually produced when dressed, not only the games he
happened to play. Everything here is pre-lock: the depth chart is the last
one published before kickoff, the injury report is the week's, and prior
snap share is strictly-before.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

POOLS = ("played", "roster")

# Columns a model may use from history. dk_points is the target and is fine to
# see for *past* weeks — that is what a trailing average is.
HISTORY_COLUMNS = (
    "player_id", "position", "season", "week", "team", "opponent", "game_id",
    "snaps", "snap_pct", "targets", "carries", "receptions", "pass_attempts",
    "dk_points",
)
ROLE_COLUMNS = ("depth_rank", "eff_rank", "injury_status")
SLATE_COLUMNS = ["player_id", "position", "team", "opponent", "game_id", "implied_total"]
ROSTER_SLATE_COLUMNS = SLATE_COLUMNS + list(ROLE_COLUMNS)

_STATS_SQL = """
    SELECT s.player_id, p.position, s.season, s.week, s.team, s.opponent,
           s.game_id, s.snaps, s.snap_pct, s.targets, s.carries, s.receptions,
           s.pass_attempts, s.dk_points
    FROM player_week_stats s
    JOIN players p ON p.player_id = s.player_id
"""
_BEFORE = "(season < ?) OR (season = ? AND week < ?)"


def _frame(conn: sqlite3.Connection, sql: str, params: tuple) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def _dressed(conn: sqlite3.Connection, where: str, params: tuple) -> pd.DataFrame:
    """ACT roster rows on teams with a game, with game_id and opponent."""
    return _frame(
        conn,
        f"""
        SELECT r.player_id, r.position, r.season, r.week, r.team, g.game_id,
               CASE WHEN g.home_team = r.team THEN g.away_team ELSE g.home_team END AS opponent
        FROM rosters r
        JOIN games g ON g.season = r.season AND g.week = r.week
                    AND (g.home_team = r.team OR g.away_team = r.team)
        WHERE r.status = 'ACT' AND ({where.replace('season', 'r.season').replace('week', 'r.week')})
        """,
        params,
    )


def _roles(conn: sqlite3.Connection, where: str, params: tuple) -> pd.DataFrame:
    return _frame(
        conn,
        f"SELECT player_id, season, week, depth_rank, injury_status FROM roles WHERE {where}",
        params,
    )


def effective_ranks(frame: pd.DataFrame) -> pd.Series:
    """Ordinal rank among the rows sharing (season, week, team, position).

    Ordered by depth_rank (unknown last), then prior snap share (higher
    first, unknown last), then player_id for determinism. Old-format depth
    charts tie starters (two WRs at 1); the snap share breaks the tie with
    what was knowable. Returns a Series aligned to ``frame.index``.
    """
    f = pd.DataFrame({
        "season": frame["season"], "week": frame["week"], "team": frame["team"],
        "position": frame["position"], "player_id": frame["player_id"],
        "_dr": pd.to_numeric(frame["depth_rank"], errors="coerce").fillna(99),
        "_ps": -pd.to_numeric(frame.get("prior_snap"), errors="coerce").fillna(-1.0),
    }, index=frame.index)
    f = f.sort_values(["season", "week", "team", "position", "_dr", "_ps", "player_id"])
    ranks = f.groupby(["season", "week", "team", "position"]).cumcount() + 1
    return ranks.reindex(frame.index).astype(int)


def as_of(conn: sqlite3.Connection, season: int, week: int, pool: str = "played") -> pd.DataFrame:
    """Every stat row strictly before ``(season, week)``.

    Tuple ordering, so (2023, 18) precedes (2024, 1): a week-1 projection may
    use all of the prior season and none of the current one.

    ``pool="roster"`` adds a zero row for each week a player dressed without a
    stat line, and the role columns (see module docstring). ``pool="played"``
    is byte-for-byte the original frame.
    """
    params = (season, season, week)
    stats = _frame(conn, _STATS_SQL + " WHERE (s.season < ?) OR (s.season = ? AND s.week < ?)",
                   params)
    if pool == "played":
        return stats
    if pool != "roster":
        raise ValueError(f"unknown pool {pool!r}; expected one of {POOLS}")

    dressed = _dressed(conn, _BEFORE, params)
    keys = ["player_id", "season", "week"]
    silent = dressed.merge(stats[keys].assign(_has=1), on=keys, how="left")
    silent = silent[silent["_has"].isna()].drop(columns="_has")
    zeros = pd.DataFrame({c: np.nan for c in HISTORY_COLUMNS}, index=silent.index)
    for c in ("player_id", "position", "season", "week", "team", "opponent", "game_id"):
        zeros[c] = silent[c]
    for c in ("snaps", "snap_pct", "targets", "carries", "receptions", "pass_attempts", "dk_points"):
        zeros[c] = 0.0
    hist = pd.concat([stats, zeros], ignore_index=True) if len(zeros) else stats.copy()
    if hist.empty:
        return pd.DataFrame(columns=list(HISTORY_COLUMNS) + list(ROLE_COLUMNS))

    roles = _roles(conn, _BEFORE, params)
    hist = hist.merge(roles, on=keys, how="left")
    hist = hist.sort_values(["player_id", "season", "week"]).reset_index(drop=True)
    # Prior snap share: mean snap_pct over the player's earlier rows (zeros count).
    sp = hist["snap_pct"].fillna(0.0)
    by = hist.groupby("player_id")
    hist["prior_snap"] = (sp.groupby(hist["player_id"]).cumsum() - sp) / by.cumcount().replace(0, np.nan)
    hist["eff_rank"] = effective_ranks(hist)
    return hist[list(HISTORY_COLUMNS) + list(ROLE_COLUMNS)]


def implied_totals(conn: sqlite3.Connection, season: int, week: int) -> pd.DataFrame:
    """Per (game_id, team): the Vegas implied team total for week W's games.

    Lines are published before lock, so they are legitimate week-W inputs.
    spread_line is home-favoured-positive (verified against 1,960 results):
    home = total/2 + spread/2, away = total/2 - spread/2.
    """
    games = _frame(
        conn,
        "SELECT game_id, home_team, away_team, spread_line, total_line "
        "FROM games WHERE season = ? AND week = ?",
        (season, week),
    )
    rows = []
    for g in games.itertuples():
        if pd.isna(g.total_line) or pd.isna(g.spread_line):
            continue
        rows.append({"game_id": g.game_id, "team": g.home_team,
                     "implied_total": g.total_line / 2 + g.spread_line / 2})
        rows.append({"game_id": g.game_id, "team": g.away_team,
                     "implied_total": g.total_line / 2 - g.spread_line / 2})
    return pd.DataFrame(rows, columns=["game_id", "team", "implied_total"])


def slate(conn: sqlite3.Connection, season: int, week: int, pool: str = "played") -> pd.DataFrame:
    """Week W's pool, with only what was knowable before lock.

    ``pool="played"``: every player with a stat row that week — everyone who
    took the field. ``pool="roster"``: see :func:`roster_slate`.
    Columns: player_id, position, team, opponent, game_id, implied_total
    (+ the role columns in roster mode). Deliberately **no dk_points**; that
    is ``actuals()``.
    """
    if pool == "roster":
        return roster_slate(conn, season, week)
    if pool != "played":
        raise ValueError(f"unknown pool {pool!r}; expected one of {POOLS}")
    played = _frame(
        conn,
        "SELECT s.player_id, p.position, s.team, s.opponent, s.game_id "
        "FROM player_week_stats s JOIN players p ON p.player_id = s.player_id "
        "WHERE s.season = ? AND s.week = ?",
        (season, week),
    )
    totals = implied_totals(conn, season, week)
    return played.merge(totals, on=["game_id", "team"], how="left")[SLATE_COLUMNS]


def roster_slate(conn: sqlite3.Connection, season: int, week: int) -> pd.DataFrame:
    """Week W's pool as a Showdown entrant sees it at lock.

    Every QB/RB/WR/TE whose roster status that week is ACT (dressed) on a team
    with a game, plus a DST for each of those teams, with the role each holds
    going into the week. Position comes from the roster, so a rookie with no
    history is in the pool with the right position. Teams on bye have no game
    and drop out. Empty if no rosters are loaded for the week — the harness
    treats that as an error, not a pass.
    """
    games = _frame(
        conn,
        "SELECT game_id, home_team, away_team FROM games WHERE season = ? AND week = ?",
        (season, week),
    )
    dressed = _frame(
        conn,
        "SELECT player_id, position, team FROM rosters "
        "WHERE season = ? AND week = ? AND status = 'ACT'",
        (season, week),
    )
    if games.empty or dressed.empty:
        return pd.DataFrame(columns=ROSTER_SLATE_COLUMNS)

    home = games.rename(columns={"home_team": "team", "away_team": "opponent"})
    away = games.rename(columns={"away_team": "team", "home_team": "opponent"})
    playing = pd.concat([home, away], ignore_index=True)[["game_id", "team", "opponent"]]

    offense = dressed.merge(playing, on="team", how="inner")
    # games.* teams are already canonical, so this is exactly teams.dst_player_id.
    dst = playing.assign(player_id="DST_" + playing["team"], position="DST")
    pool = pd.concat([offense, dst], ignore_index=True)
    totals = implied_totals(conn, season, week)
    pool = pool.merge(totals, on=["game_id", "team"], how="left")

    return attach_roles(conn, pool, season, week)[ROSTER_SLATE_COLUMNS]


def week_roles(conn: sqlite3.Connection, season: int, week: int) -> pd.DataFrame:
    """depth_rank and injury_status for week W itself — pre-lock by construction."""
    return _roles(conn, "season = ? AND week = ?", (season, week)).drop(columns=["season", "week"])


def prior_snap_share(conn: sqlite3.Connection, season: int, week: int) -> pd.DataFrame:
    """Mean snap share over dressed weeks strictly before W, zeros counted —
    the same definition as as_of() uses, so ties break the same way."""
    return _frame(
        conn,
        """
        SELECT r.player_id, AVG(COALESCE(s.snap_pct, 0)) AS prior_snap
        FROM rosters r
        JOIN games g ON g.season = r.season AND g.week = r.week
                    AND (g.home_team = r.team OR g.away_team = r.team)
        LEFT JOIN player_week_stats s ON s.player_id = r.player_id
                    AND s.season = r.season AND s.week = r.week
        WHERE r.status = 'ACT' AND ((r.season < ?) OR (r.season = ? AND r.week < ?))
        GROUP BY r.player_id
        """,
        (season, season, week),
    )


def attach_roles(conn: sqlite3.Connection, pool: pd.DataFrame, season: int, week: int) -> pd.DataFrame:
    """Add depth_rank, injury_status and eff_rank to a week-W pool frame.

    ``pool`` needs player_id, team, position. eff_rank is the ordinal among the
    rows of ``pool`` sharing (team, position) — so the pool must be the set of
    players actually available (dressed for a backtest, not-OUT for a live
    DK export), which is what makes "rank among the available" meaningful.
    """
    out = pool.merge(week_roles(conn, season, week), on="player_id", how="left")
    out = out.merge(prior_snap_share(conn, season, week), on="player_id", how="left")
    out["season"] = season
    out["week"] = week
    out["eff_rank"] = effective_ranks(out) if len(out) else pd.Series(dtype=int)
    return out.drop(columns=["season", "week", "prior_snap"])


def actuals(conn: sqlite3.Connection, season: int, week: int) -> pd.DataFrame:
    """The answer key for week W: player_id, dk_points, snaps. Harness-only."""
    return _frame(
        conn,
        "SELECT player_id, dk_points, snaps FROM player_week_stats "
        "WHERE season = ? AND week = ?",
        (season, week),
    )


def weeks_available(conn: sqlite3.Connection, seasons) -> list[tuple[int, int]]:
    """(season, week) pairs that have stat rows, in order."""
    rows = _frame(
        conn,
        "SELECT DISTINCT season, week FROM player_week_stats "
        f"WHERE season IN ({','.join('?' * len(seasons))}) ORDER BY season, week",
        tuple(int(s) for s in seasons),
    )
    return [(int(r.season), int(r.week)) for r in rows.itertuples()]
