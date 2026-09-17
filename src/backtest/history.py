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
"""

from __future__ import annotations

import sqlite3

import pandas as pd

POOLS = ("played", "roster")

# Columns a model may use from history. dk_points is the target and is fine to
# see for *past* weeks — that is what a trailing average is.
HISTORY_COLUMNS = (
    "player_id", "position", "season", "week", "team", "opponent", "game_id",
    "snaps", "snap_pct", "targets", "carries", "receptions", "pass_attempts",
    "dk_points",
)

_STATS_SQL = """
    SELECT s.player_id, p.position, s.season, s.week, s.team, s.opponent,
           s.game_id, s.snaps, s.snap_pct, s.targets, s.carries, s.receptions,
           s.pass_attempts, s.dk_points
    FROM player_week_stats s
    JOIN players p ON p.player_id = s.player_id
"""


def _frame(conn: sqlite3.Connection, sql: str, params: tuple) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def as_of(conn: sqlite3.Connection, season: int, week: int) -> pd.DataFrame:
    """Every stat row strictly before ``(season, week)``.

    Tuple ordering, so (2023, 18) precedes (2024, 1): a week-1 projection may
    use all of the prior season and none of the current one.
    """
    return _frame(
        conn,
        _STATS_SQL + " WHERE (s.season < ?) OR (s.season = ? AND s.week < ?)",
        (season, season, week),
    )


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


SLATE_COLUMNS = ["player_id", "position", "team", "opponent", "game_id", "implied_total"]


def slate(conn: sqlite3.Connection, season: int, week: int, pool: str = "played") -> pd.DataFrame:
    """Week W's pool, with only what was knowable before lock.

    ``pool="played"``: every player with a stat row that week — everyone who
    took the field. ``pool="roster"``: see :func:`roster_slate`.
    Columns: player_id, position, team, opponent, game_id, implied_total.
    Deliberately **no dk_points**; that is ``actuals()``.
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
    with a game, plus a DST for each of those teams. Position comes from the
    roster, so a rookie with no history is in the pool with the right
    position. Teams on bye have no game and drop out. Empty if no rosters are
    loaded for the week — the harness treats that as an error, not a pass.
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
        return pd.DataFrame(columns=SLATE_COLUMNS)

    home = games.rename(columns={"home_team": "team", "away_team": "opponent"})
    away = games.rename(columns={"away_team": "team", "home_team": "opponent"})
    playing = pd.concat([home, away], ignore_index=True)[["game_id", "team", "opponent"]]

    offense = dressed.merge(playing, on="team", how="inner")
    # games.* teams are already canonical, so this is exactly teams.dst_player_id.
    dst = playing.assign(player_id="DST_" + playing["team"], position="DST")
    pool = pd.concat([offense, dst], ignore_index=True)
    totals = implied_totals(conn, season, week)
    return pool.merge(totals, on=["game_id", "team"], how="left")[SLATE_COLUMNS]


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
