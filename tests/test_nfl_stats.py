"""Ingest orchestration: idempotent, tolerant of a live season, and honest about
how current the database is (T21). Sources are faked; no network.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from src import db, nflverse
from src.ingest import nfl_stats

SEASON = 2026


def raw_games() -> pd.DataFrame:
    # Week 1 played (scored), week 2 scheduled (no score yet).
    return pd.DataFrame([
        {"game_id": "2026_01_NE_SEA", "season": 2026, "week": 1, "home_team": "SEA",
         "away_team": "NE", "gameday": "2026-09-09", "gametime": "20:20",
         "spread_line": 3.0, "total_line": 44.5, "home_score": 13, "away_score": 10},
        {"game_id": "2026_02_DEN_KC", "season": 2026, "week": 2, "home_team": "KC",
         "away_team": "DEN", "gameday": "2026-09-20", "gametime": "16:25",
         "spread_line": 6.5, "total_line": 47.0, "home_score": None, "away_score": None},
        {"game_id": "2025_18_NE_SEA", "season": 2025, "week": 18, "home_team": "SEA",
         "away_team": "NE", "gameday": "2026-01-04", "gametime": "13:00",
         "spread_line": 1.0, "total_line": 40.0, "home_score": 20, "away_score": 17},
    ])


def raw_players(season: int) -> pd.DataFrame:
    weeks = [1] if season == 2026 else [17, 18]
    rows = []
    for w in weeks:
        rows += [
            {"player_id": "00-qb", "player_display_name": "Sam Darnold", "player_name": "S.Darnold",
             "position": "QB", "team": "SEA", "opponent_team": "NE", "season": season, "week": w,
             "season_type": "REG", "game_id": f"{season}_{w:02d}_NE_SEA",
             "passing_yards": 250, "passing_tds": 2, "attempts": 30},
            {"player_id": "00-wr", "player_display_name": "Jaxon Smith-Njigba", "player_name": "J.Smith-Njigba",
             "position": "WR", "team": "SEA", "opponent_team": "NE", "season": season, "week": w,
             "season_type": "REG", "game_id": f"{season}_{w:02d}_NE_SEA",
             "receiving_yards": 90, "receptions": 7, "targets": 10},
        ]
    return pd.DataFrame(rows)


def raw_team(season: int) -> pd.DataFrame:
    weeks = [1] if season == 2026 else [17, 18]
    rows = []
    for w in weeks:
        for team, opp in (("SEA", "NE"), ("NE", "SEA")):
            rows.append({"team": team, "opponent_team": opp, "season": season, "week": w,
                         "season_type": "REG", "game_id": f"{season}_{w:02d}_NE_SEA",
                         "def_sacks": 2.0, "def_interceptions": 1})
    return pd.DataFrame(rows)


def raw_snaps(season: int) -> pd.DataFrame:
    weeks = [1] if season == 2026 else [17, 18]
    return pd.DataFrame([
        {"player": "Sam Darnold", "team": "SEA", "season": season, "week": w,
         "offense_snaps": 60, "offense_pct": 1.0}
        for w in weeks
    ] + [
        {"player": "Jaxon Smith-Njigba", "team": "SEA", "season": season, "week": w,
         "offense_snaps": 55, "offense_pct": 0.92}
        for w in weeks
    ])


def raw_rosters(season: int) -> pd.DataFrame:
    weeks = [1, 2] if season == 2026 else [17, 18]   # the upcoming week is published early
    rows = []
    for w in weeks:
        rows += [
            {"season": season, "week": w, "game_type": "REG", "team": "SEA", "position": "QB",
             "status": "ACT", "gsis_id": "00-qb", "full_name": "Sam Darnold",
             "depth_chart_position": "QB"},
            {"season": season, "week": w, "game_type": "REG", "team": "SEA", "position": "WR",
             "status": "ACT", "gsis_id": "00-wr", "full_name": "Jaxon Smith-Njigba",
             "depth_chart_position": "WR"},
            {"season": season, "week": w, "game_type": "REG", "team": "LA", "position": "FB",
             "status": "INA", "gsis_id": "00-fb", "full_name": "Some Fullback",
             "depth_chart_position": "RB"},
            {"season": season, "week": w, "game_type": "REG", "team": "SEA", "position": "OL",
             "status": "ACT", "gsis_id": "00-ol", "full_name": "A Lineman",
             "depth_chart_position": "T"},
            {"season": season, "week": w, "game_type": "REG", "team": "SEA", "position": "TE",
             "status": "DEV", "gsis_id": None, "full_name": "No Id Yet",
             "depth_chart_position": None},
        ]
    rows.append({"season": season, "week": 19, "game_type": "WC", "team": "SEA", "position": "QB",
                 "status": "ACT", "gsis_id": "00-qb", "full_name": "Sam Darnold",
                 "depth_chart_position": "QB"})
    return pd.DataFrame(rows)


def raw_depth(season: int) -> pd.DataFrame:
    weeks = [1, 2] if season == 2026 else [17, 18]
    return pd.DataFrame([
        dict(season=season, club_code="SEA", week=float(w), game_type="REG", depth_team="1",
             formation="Offense", gsis_id="00-qb", position="QB", depth_position="QB",
             full_name="Sam Darnold")
        for w in weeks
    ])


def raw_injuries(season: int) -> pd.DataFrame:
    weeks = [1, 2] if season == 2026 else [17, 18]
    return pd.DataFrame([
        dict(season=season, game_type="REG", team="SEA", week=w, gsis_id="00-wr", position="WR",
             full_name="Jaxon Smith-Njigba", report_status="Questionable",
             practice_status="Limited Participation in Practice")
        for w in weeks
    ])


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    db.create_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def sources(monkeypatch):
    monkeypatch.setattr(nflverse, "games", lambda refresh=False, cfg=None, today=None: raw_games())
    monkeypatch.setattr(nflverse, "player_week",
                        lambda season, refresh=False, cfg=None, today=None: raw_players(season))
    monkeypatch.setattr(nflverse, "team_week",
                        lambda season, refresh=False, cfg=None, today=None: raw_team(season))
    monkeypatch.setattr(nflverse, "snap_counts",
                        lambda season, refresh=False, cfg=None, today=None: raw_snaps(season))
    monkeypatch.setattr(nflverse, "weekly_rosters",
                        lambda season, refresh=False, cfg=None, today=None: raw_rosters(season))
    monkeypatch.setattr(nflverse, "depth_charts",
                        lambda season, refresh=False, cfg=None, today=None: raw_depth(season))
    monkeypatch.setattr(nflverse, "injuries",
                        lambda season, refresh=False, cfg=None, today=None: raw_injuries(season))


def stats(conn) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM player_week_stats ORDER BY season, week, player_id", conn)


class TestIngest:
    def test_loads_the_live_season(self, conn, sources):
        nfl_stats.ingest(conn, [SEASON])
        frame = stats(conn)
        assert set(frame.season) == {SEASON}
        # 2 offensive players + 2 DSTs for the one played week
        assert len(frame) == 4
        assert frame[frame.player_id == "00-qb"].dk_points.iloc[0] == pytest.approx(250 * 0.04 + 2 * 4)

    def test_rerun_is_idempotent(self, conn, sources):
        first = nfl_stats.ingest(conn, [2025, SEASON])
        counts_after_first = db.table_counts(conn)
        second = nfl_stats.ingest(conn, [2025, SEASON])
        assert first == second
        assert db.table_counts(conn) == counts_after_first

    def test_unplayed_games_are_kept_for_their_lines_but_not_scored(self, conn, sources):
        nfl_stats.ingest(conn, [SEASON])
        games = pd.read_sql_query("SELECT * FROM games WHERE season = 2026 ORDER BY week", conn)
        assert list(games.week) == [1, 2]
        assert pd.isna(games.loc[1, "home_score"])
        assert games.loc[1, "spread_line"] == 6.5           # next week's line is usable
        assert games.loc[1, "kickoff_utc"].startswith("2026-09-20T20:25")  # 16:25 ET -> UTC
        dst = pd.read_sql_query("SELECT week FROM dst_week_stats WHERE season = 2026", conn)
        assert set(dst.week) == {1}                          # nothing scored for week 2

    def test_snaps_are_joined_by_name_and_team(self, conn, sources):
        nfl_stats.ingest(conn, [SEASON])
        frame = stats(conn).set_index("player_id")
        assert frame.loc["00-wr", "snap_pct"] == pytest.approx(0.92)

    def test_one_seasons_missing_snaps_leave_only_that_season_null(self, conn, sources, monkeypatch):
        def flaky(season, refresh=False, cfg=None, today=None):
            if season == SEASON:
                raise nflverse.NflverseUnavailable("not published")
            return raw_snaps(season)

        monkeypatch.setattr(nflverse, "snap_counts", flaky)
        nfl_stats.ingest(conn, [2025, SEASON])
        frame = stats(conn)
        offense = frame[frame.player_id.isin(["00-qb", "00-wr"])]
        assert offense[offense.season == 2025].snaps.notna().all()
        assert offense[offense.season == SEASON].snaps.isna().all()


class TestRosters:
    def rosters(self, conn) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM rosters ORDER BY season, week, player_id", conn)

    def test_skill_positions_regular_season_with_an_id_only(self, conn, sources):
        nfl_stats.ingest(conn, [SEASON])
        frame = self.rosters(conn)
        assert set(frame.player_id) == {"00-qb", "00-wr", "00-fb"}   # no OL, no id-less TE
        assert set(frame.week) == {1, 2}                             # no wild-card row
        assert set(frame.status) == {"ACT", "INA"}                   # every status kept

    def test_team_and_position_are_normalized(self, conn, sources):
        nfl_stats.ingest(conn, [SEASON])
        fb = self.rosters(conn).set_index("player_id").loc["00-fb"].iloc[0]
        assert fb["team"] == "LAR"
        assert fb["position"] == "RB"
        assert fb["depth_position"] == "RB"

    def test_rerun_is_idempotent(self, conn, sources):
        nfl_stats.ingest(conn, [SEASON])
        n = len(self.rosters(conn))
        nfl_stats.ingest(conn, [SEASON])
        assert len(self.rosters(conn)) == n == 6

    def test_a_missing_roster_file_warns_and_everything_else_still_loads(self, conn, sources,
                                                                          monkeypatch):
        def missing(season, refresh=False, cfg=None, today=None):
            raise nflverse.NflverseUnavailable("not published")

        monkeypatch.setattr(nflverse, "weekly_rosters", missing)
        counts = nfl_stats.ingest(conn, [SEASON])
        assert counts["rosters"] == 0
        assert counts["player_week_stats"] > 0
        assert self.rosters(conn).empty


class TestIngestStatus:
    def at(self, iso: str) -> datetime:
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)

    def test_current_when_every_completed_week_is_ingested(self, conn, sources):
        nfl_stats.ingest(conn, [SEASON])
        status = nfl_stats.ingest_status(conn, SEASON, now=self.at("2026-09-17T12:00:00"))
        assert status["last_ingested_week"] == 1
        assert status["last_completed_week"] == 1
        assert status["missing_weeks"] == []
        assert "!!" not in nfl_stats.format_status(status)

    def test_reports_how_far_ahead_the_rosters_run(self, conn, sources):
        nfl_stats.ingest(conn, [SEASON])
        status = nfl_stats.ingest_status(conn, SEASON, now=self.at("2026-09-17T12:00:00"))
        assert status["last_roster_week"] == 2
        assert status["last_role_week"] == 2
        assert "rosters through week 2" in nfl_stats.format_status(status)
        assert "depth charts/injuries through week 2" in nfl_stats.format_status(status)

    def test_a_completed_but_unpublished_week_is_flagged(self, conn, sources):
        nfl_stats.ingest(conn, [SEASON])
        # Monday after the week-2 game: complete, but the fake source only has week 1.
        status = nfl_stats.ingest_status(conn, SEASON, now=self.at("2026-09-21T12:00:00"))
        assert status["last_completed_week"] == 2
        assert status["missing_weeks"] == [2]
        assert "!!" in nfl_stats.format_status(status)

    def test_a_week_still_being_played_is_not_complete(self, conn, sources):
        nfl_stats.ingest(conn, [SEASON])
        # One hour into the week-2 kickoff.
        status = nfl_stats.ingest_status(conn, SEASON, now=self.at("2026-09-20T21:25:00"))
        assert status["last_completed_week"] == 1
        assert status["missing_weeks"] == []

    def test_empty_season_reports_nothing_rather_than_crashing(self, conn):
        status = nfl_stats.ingest_status(conn, SEASON, now=self.at("2026-09-17T12:00:00"))
        assert status["last_ingested_week"] is None
        assert status["last_completed_week"] is None
        assert "-" in nfl_stats.format_status(status)
