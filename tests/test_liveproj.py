"""Overlaying model projections onto a live pool: v4 (RoleAware) by default,
the v3 path kept for comparison."""

from __future__ import annotations

import pandas as pd
import pytest

from src import db
from src.liveproj import project_live_pool
from src.projection import CalibratedAverage, RoleAware


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    db.create_schema(conn)
    db.upsert(conn, "players", [
        {"player_id": "00-001", "name": "Vet QB", "position": "QB", "first_season": 2020},
        {"player_id": "00-002", "name": "Backup QB", "position": "QB", "first_season": 2020},
    ])
    db.upsert(conn, "games", [
        {"game_id": f"g{w}", "season": 2025, "week": w, "home_team": "AAA",
         "away_team": "BBB", "spread_line": 0.0, "total_line": 44.0}
        for w in range(1, 6)
    ] + [{"game_id": "g2026", "season": 2026, "week": 1, "home_team": "AAA",
          "away_team": "BBB", "spread_line": 0.0, "total_line": 44.0}])
    db.upsert(conn, "player_week_stats", [
        {"player_id": "00-001", "season": 2025, "week": w, "team": "AAA",
         "opponent": "BBB", "game_id": f"g{w}", "dk_points": 20.0 + w, "snap_pct": 1.0}
        for w in range(1, 6)
    ] + [
        {"player_id": "00-002", "season": 2025, "week": w, "team": "AAA",
         "opponent": "BBB", "game_id": f"g{w}", "dk_points": 2.0, "snap_pct": 0.05}
        for w in range(1, 6)
    ])
    db.upsert(conn, "rosters", [
        {"player_id": p, "season": 2025, "week": w, "team": "AAA", "position": "QB",
         "status": "ACT", "name": p} for p in ("00-001", "00-002") for w in range(1, 6)
    ])
    db.upsert(conn, "roles", [
        {"player_id": "00-001", "season": 2025, "week": w, "team": "AAA", "position": "QB",
         "depth_rank": 1} for w in range(1, 6)
    ] + [
        {"player_id": "00-002", "season": 2025, "week": w, "team": "AAA", "position": "QB",
         "depth_rank": 2} for w in range(1, 6)
    ] + [
        {"player_id": "00-001", "season": 2026, "week": 1, "team": "AAA", "position": "QB",
         "depth_rank": 1},
        {"player_id": "00-002", "season": 2026, "week": 1, "team": "AAA", "position": "QB",
         "depth_rank": 2},
    ])
    yield conn
    conn.close()


def pool_rows(*rows):
    return pd.DataFrame([{
        "player_id": f"dk{i}", "name": name, "team": "AAA", "position": pos,
        "salary": 10000.0, "projection": avg, "gsis_id": gsis,
    } for i, (name, gsis, pos, avg) in enumerate(rows)])


QB1_OWN = sum(20.0 + w for w in range(1, 6)) / 5      # 23.0
QB2_OWN = 2.0


class TestRoleAwareLive:
    def test_a_resolved_veteran_gets_v4_from_his_current_role(self, conn):
        out = project_live_pool(conn, pool_rows(("Vet QB", "00-001", "QB", 15.0)),
                                season=2026, week=1)
        row = out.iloc[0]
        assert row["proj_source"] == "v4"
        # only he holds QB1 in history, so the prior is his own mean: shrinkage is a no-op
        assert row["projection"] == pytest.approx(QB1_OWN)
        assert row["eff_rank"] == 1 and row["depth_rank"] == 1

    def test_effective_rank_is_among_the_pool_handed_in(self, conn):
        out = project_live_pool(conn, pool_rows(("Vet QB", "00-001", "QB", 15.0),
                                                ("Backup QB", "00-002", "QB", 4.0)),
                                season=2026, week=1).set_index("name")
        assert out.loc["Vet QB", "eff_rank"] == 1
        assert out.loc["Backup QB", "eff_rank"] == 2
        assert out.loc["Backup QB", "projection"] == pytest.approx(QB2_OWN)

    def test_a_demoted_veteran_projects_as_a_backup(self, conn):
        # The Fields case: a starter's history, a backup's depth chart this week.
        conn.execute("UPDATE roles SET depth_rank = 2 WHERE player_id = '00-001' AND season = 2026")
        conn.execute("UPDATE roles SET depth_rank = 1 WHERE player_id = '00-002' AND season = 2026")
        out = project_live_pool(conn, pool_rows(("Vet QB", "00-001", "QB", 15.0),
                                                ("Backup QB", "00-002", "QB", 4.0)),
                                season=2026, week=1).set_index("name")
        assert out.loc["Vet QB", "eff_rank"] == 2
        assert out.loc["Vet QB", "projection"] == pytest.approx(QB2_OWN)     # not 23, not 15
        assert out.loc["Backup QB", "projection"] == pytest.approx(QB1_OWN)  # not 2

    def test_a_team_changer_is_projected_from_his_new_role_not_refused(self, conn):
        row = pool_rows(("Vet QB", "00-001", "QB", 15.0))
        row["team_changed"] = True
        out = project_live_pool(conn, row, season=2026, week=1)
        assert out.iloc[0]["proj_source"] == "v4"
        assert bool(out.iloc[0]["team_changed"]) is True      # still reported

    def test_a_resolved_player_with_no_history_still_gets_v4(self, conn):
        db.upsert(conn, "players", [{"player_id": "00-003", "name": "Rook", "position": "QB",
                                     "first_season": 2026}])
        out = project_live_pool(conn, pool_rows(("Rook", "00-003", "QB", 0.0)),
                                season=2026, week=1)
        assert out.iloc[0]["proj_source"] == "v4"
        assert out.iloc[0]["projection"] > 0.0                  # a role prior, not DK's 0.0

    def test_an_injury_listing_this_week_is_applied(self, conn):
        conn.execute("UPDATE roles SET injury_status = 'Out' WHERE player_id = '00-001' AND season = 2026")
        out = project_live_pool(conn, pool_rows(("Vet QB", "00-001", "QB", 15.0)),
                                season=2026, week=1)
        assert out.iloc[0]["projection"] == 0.0
        assert out.iloc[0]["injury_status"] == "Out"

    def test_an_unresolved_player_and_a_kicker_keep_avg_points(self, conn):
        out = project_live_pool(conn, pool_rows(("Ghost", None, "WR", 15.0),
                                                ("Kicker", "00-009", "K", 8.0)),
                                season=2026, week=1)
        assert list(out["proj_source"]) == ["avg_points", "avg_points"]
        assert list(out["projection"]) == [15.0, 8.0]

    def test_empty_pool_does_not_crash(self, conn):
        empty = pd.DataFrame(columns=["player_id", "name", "team", "position",
                                      "salary", "projection", "gsis_id"])
        out = project_live_pool(conn, empty, season=2026, week=1)
        assert out.empty

    def test_no_history_at_all_leaves_the_pool_untouched(self):
        bare = db.connect(":memory:"); db.create_schema(bare)
        out = project_live_pool(bare, pool_rows(("Vet QB", "00-001", "QB", 15.0)),
                                season=2026, week=1)
        assert out.iloc[0]["proj_source"] == "avg_points"


class TestLegacyV3Path:
    def v3(self, **kw):
        return CalibratedAverage(per_position=True, min_fit_rows=3, **kw)

    def test_a_resolved_veteran_gets_v3(self, conn):
        out = project_live_pool(conn, pool_rows(("Vet QB", "00-001", "QB", 15.0)),
                                season=2026, week=1, model=self.v3())
        assert out.iloc[0]["proj_source"] == "v3"
        assert out.iloc[0]["projection"] != 15.0

    def test_too_little_history_keeps_avg_points(self, conn):
        db.upsert(conn, "players", [{"player_id": "00-003", "name": "Rook", "position": "QB",
                                     "first_season": 2025}])
        db.upsert(conn, "player_week_stats", [
            {"player_id": "00-003", "season": 2025, "week": 1, "team": "AAA",
             "opponent": "BBB", "game_id": "g1", "dk_points": 5.0}])
        out = project_live_pool(conn, pool_rows(("Rook", "00-003", "QB", 3.0)),
                                season=2026, week=1, min_games=3, model=self.v3())
        assert out.iloc[0]["proj_source"] == "avg_points"
        assert out.iloc[0]["projection"] == 3.0

    def test_a_team_changer_is_still_refused_by_v3(self, conn):
        row = pool_rows(("Vet QB", "00-001", "QB", 15.0))
        row["team_changed"] = True
        out = project_live_pool(conn, row, season=2026, week=1, model=self.v3())
        assert out.iloc[0]["proj_source"] == "team_changed"
        assert out.iloc[0]["projection"] == 15.0

    def test_a_custom_role_aware_instance_is_honoured(self, conn):
        out = project_live_pool(conn, pool_rows(("Vet QB", "00-001", "QB", 15.0)),
                                season=2026, week=1, model=RoleAware(k=0.0))
        assert out.iloc[0]["proj_source"] == "v4"
        assert out.iloc[0]["projection"] == pytest.approx(QB1_OWN)
