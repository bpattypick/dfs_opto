"""Overlaying v3 projections onto a live pool (T16/T18/T14 assembled)."""

from __future__ import annotations

import pandas as pd
import pytest

from src import db
from src.liveproj import project_live_pool
from src.projection import CalibratedAverage


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    db.create_schema(conn)
    db.upsert(conn, "players", [
        {"player_id": "00-001", "name": "Vet QB", "position": "QB", "first_season": 2020},
    ])
    db.upsert(conn, "games", [
        {"game_id": f"g{w}", "season": 2025, "week": w, "home_team": "AAA",
         "away_team": "BBB", "spread_line": 0.0, "total_line": 44.0}
        for w in range(1, 6)
    ])
    db.upsert(conn, "player_week_stats", [
        {"player_id": "00-001", "season": 2025, "week": w, "team": "AAA",
         "opponent": "BBB", "game_id": f"g{w}", "dk_points": 20.0 + w}
        for w in range(1, 6)
    ])
    yield conn
    conn.close()


def pool_row(name, gsis_id, avg_points=15.0):
    return pd.DataFrame([{
        "player_id": "dk1", "name": name, "team": "AAA", "position": "QB",
        "salary": 10000.0, "projection": avg_points, "gsis_id": gsis_id,
    }])


class TestProjectLivePool:
    def test_a_resolved_veteran_gets_v3(self, conn):
        model = CalibratedAverage(per_position=True, min_fit_rows=3)
        out = project_live_pool(conn, pool_row("Vet QB", "00-001"), season=2026, week=1,
                                model=model)
        assert out.iloc[0]["proj_source"] == "v3"
        # v3's projection should differ from the stale AvgPointsPerGame stand-in.
        assert out.iloc[0]["projection"] != 15.0

    def test_an_unresolved_player_keeps_avg_points(self, conn):
        out = project_live_pool(conn, pool_row("Ghost", None), season=2026, week=1)
        assert out.iloc[0]["proj_source"] == "avg_points"
        assert out.iloc[0]["projection"] == 15.0

    def test_a_resolved_player_with_too_little_history_keeps_avg_points(self, conn):
        db.upsert(conn, "players", [
            {"player_id": "00-002", "name": "Rook", "position": "QB", "first_season": 2025},
        ])
        db.upsert(conn, "player_week_stats", [
            {"player_id": "00-002", "season": 2025, "week": 1, "team": "AAA",
             "opponent": "BBB", "game_id": "g1", "dk_points": 5.0},
        ])
        out = project_live_pool(conn, pool_row("Rook", "00-002", 3.0), season=2026, week=1,
                                min_games=3)
        assert out.iloc[0]["proj_source"] == "avg_points"
        assert out.iloc[0]["projection"] == 3.0

    def test_empty_pool_does_not_crash(self, conn):
        empty = pd.DataFrame(columns=["player_id", "name", "team", "position",
                                      "salary", "projection", "gsis_id"])
        out = project_live_pool(conn, empty, season=2026, week=1)
        assert out.empty

    def test_all_unresolved_returns_unchanged(self, conn):
        out = project_live_pool(conn, pool_row("Ghost", None), season=2026, week=1)
        assert (out["proj_source"] == "avg_points").all()

    def test_accepts_a_custom_model_instance(self, conn):
        model = CalibratedAverage(per_position=True, window=3, min_fit_rows=3)
        out = project_live_pool(conn, pool_row("Vet QB", "00-001"), season=2026, week=1,
                                model=model)
        assert out.iloc[0]["proj_source"] == "v3"
