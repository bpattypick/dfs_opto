"""Live-slate name resolution (T14's minimal wedge, task-adjacent to T14)."""

from __future__ import annotations

import pandas as pd
import pytest

from src import db
from src.resolve import resolve_pool


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    db.create_schema(conn)
    db.upsert(conn, "players", [
        {"player_id": "00-001", "name": "Bo Nix", "position": "QB", "first_season": 2024},
        {"player_id": "00-002", "name": "Trey McBride", "position": "TE", "first_season": 2022},
        {"player_id": "DST_DEN", "name": "Denver Broncos", "position": "DST", "first_season": None},
        {"player_id": "DST_KC", "name": "Kansas City Chiefs", "position": "DST", "first_season": None},
    ])
    db.upsert(conn, "games", [{"game_id": "g1", "season": 2025, "week": 1,
                               "home_team": "DEN", "away_team": "KC",
                               "spread_line": 0.0, "total_line": 44.0}])
    db.upsert(conn, "player_week_stats", [
        {"player_id": "00-001", "season": 2025, "week": 1, "team": "DEN",
         "opponent": "KC", "game_id": "g1", "dk_points": 20.0},
        {"player_id": "00-002", "season": 2025, "week": 1, "team": "ARI",
         "opponent": "KC", "game_id": "g1", "dk_points": 12.0},
    ])
    yield conn
    conn.close()


def pool(rows):
    return pd.DataFrame(rows, columns=["player_id", "name", "team", "position", "salary", "projection"])


class TestOffenseResolution:
    def test_matches_by_name_and_team(self, conn):
        out = resolve_pool(conn, pool([("dk1", "Bo Nix", "DEN", "QB", 10000, 19.0)]))
        assert out.iloc[0]["gsis_id"] == "00-001"

    def test_falls_back_to_name_only_across_a_team_change(self, conn):
        # Trey McBride's history has him at ARI; the export already reflects a
        # trade to DEN, so team+name would miss and name-only must still hit.
        out = resolve_pool(conn, pool([("dk2", "Trey McBride", "DEN", "TE", 6000, 10.0)]))
        assert out.iloc[0]["gsis_id"] == "00-002"

    def test_a_kicker_never_resolves(self, conn):
        # Kickers are not in the stats feed at all (FANTASY_POSITIONS excludes K).
        out = resolve_pool(conn, pool([("dk3", "Wil Lutz", "DEN", "K", 5200, 8.0)]))
        assert pd.isna(out.iloc[0]["gsis_id"])

    def test_an_unknown_rookie_is_unresolved_not_an_error(self, conn):
        out = resolve_pool(conn, pool([("dk4", "Nobody Yet", "KC", "WR", 200, 0.0)]))
        assert pd.isna(out.iloc[0]["gsis_id"])

    def test_empty_history_does_not_crash(self):
        empty = db.connect(":memory:"); db.create_schema(empty)
        out = resolve_pool(empty, pool([("dk1", "Bo Nix", "DEN", "QB", 10000, 19.0)]))
        assert pd.isna(out.iloc[0]["gsis_id"])


class TestDstResolution:
    def test_dst_resolves_exactly_via_team_not_name_matching(self, conn):
        # No fuzzy match involved — team -> canonical abbrev -> DST_<TEAM>.
        out = resolve_pool(conn, pool([("dk5", "Broncos", "DEN", "DST", 4800, 8.0)]))
        assert out.iloc[0]["gsis_id"] == "DST_DEN"

    def test_dst_resolves_even_with_no_offensive_history_loaded(self):
        empty = db.connect(":memory:"); db.create_schema(empty)
        out = resolve_pool(empty, pool([("dk5", "Chiefs", "KC", "DST", 6600, 5.0)]))
        assert out.iloc[0]["gsis_id"] == "DST_KC"


class TestTeamChangeFlag:
    def test_flags_a_player_whose_team_no_longer_matches_their_history(self, conn):
        # Justin Fields, 2025: full-time starter at NYJ. Exported here on KC.
        out = resolve_pool(conn, pool([("dk1", "Bo Nix", "KC", "QB", 9000, 10.0)]))
        assert bool(out.iloc[0]["team_changed"]) is True

    def test_does_not_flag_a_player_on_the_same_team(self, conn):
        out = resolve_pool(conn, pool([("dk1", "Bo Nix", "DEN", "QB", 10000, 19.0)]))
        assert bool(out.iloc[0]["team_changed"]) is False

    def test_an_unresolved_player_is_not_flagged(self, conn):
        # Nothing to compare against, so no false claim either way.
        out = resolve_pool(conn, pool([("dk4", "Nobody Yet", "KC", "WR", 200, 0.0)]))
        assert bool(out.iloc[0]["team_changed"]) is False

    def test_dst_is_never_flagged(self, conn):
        out = resolve_pool(conn, pool([("dk5", "Broncos", "DEN", "DST", 4800, 8.0)]))
        assert bool(out.iloc[0]["team_changed"]) is False


class TestMixedPool:
    def test_resolves_what_it_can_and_leaves_the_rest_null(self, conn):
        out = resolve_pool(conn, pool([
            ("dk1", "Bo Nix", "DEN", "QB", 10000, 19.0),
            ("dk3", "Wil Lutz", "DEN", "K", 5200, 8.0),
            ("dk5", "Broncos", "DEN", "DST", 4800, 8.0),
        ]))
        got = dict(zip(out["name"], out["gsis_id"]))
        assert got["Bo Nix"] == "00-001"
        assert got["Broncos"] == "DST_DEN"
        assert pd.isna(got["Wil Lutz"])

    def test_does_not_mutate_the_input_frame(self, conn):
        src = pool([("dk1", "Bo Nix", "DEN", "QB", 10000, 19.0)])
        resolve_pool(conn, src)
        assert "gsis_id" not in src.columns


class TestCrosswalkPath:
    """T14: with the slate's week, the real crosswalk runs against that week's rosters."""

    @pytest.fixture
    def rostered(self, conn):
        db.upsert(conn, "rosters", [
            {"player_id": "00-001", "season": 2026, "week": 2, "team": "KC", "position": "QB",
             "status": "ACT", "name": "Bo Nix"},                      # moved to KC this year
            {"player_id": "00-003", "season": 2026, "week": 2, "team": "KC", "position": "QB",
             "status": "INA", "name": "Garrett Nussmeier"},           # rookie, no stats anywhere
        ])
        db.upsert(conn, "games", [{"game_id": "2026_02_DEN_KC", "season": 2026, "week": 2,
                                   "home_team": "KC", "away_team": "DEN"}])
        return conn

    def test_a_rookie_on_the_roster_resolves_with_no_history(self, rostered):
        out = resolve_pool(rostered, pool([("dk9", "Garrett Nussmeier", "KC", "QB", 3000, 0.0)]),
                           season=2026, week=2)
        assert out.iloc[0]["gsis_id"] == "00-003"
        assert bool(out.iloc[0]["team_changed"]) is False

    def test_a_team_changer_resolves_and_is_flagged(self, rostered):
        out = resolve_pool(rostered, pool([("dk1", "Bo Nix", "KC", "QB", 10000, 19.0)]),
                           season=2026, week=2)
        assert out.iloc[0]["gsis_id"] == "00-001"
        assert bool(out.iloc[0]["team_changed"]) is True         # last stat row was DEN

    def test_the_dk_id_is_persisted_and_beats_a_respelled_name(self, rostered):
        resolve_pool(rostered, pool([("dk1", "Bo Nix", "KC", "QB", 10000, 19.0)]), season=2026, week=2)
        stored = pd.read_sql_query("SELECT source_id, player_id FROM id_crosswalk WHERE source='dk'", rostered)
        assert dict(zip(stored.source_id, stored.player_id)) == {"dk1": "00-001"}
        out = resolve_pool(rostered, pool([("dk1", "Bo Nix Jr.", "KC", "QB", 10000, 19.0)]),
                           season=2026, week=2)
        assert out.iloc[0]["gsis_id"] == "00-001"

    def test_dst_and_kicker_behave_as_before(self, rostered):
        out = resolve_pool(rostered, pool([("dk5", "Chiefs", "KC", "DST", 4800, 8.0),
                                           ("dk6", "Harrison Butker", "KC", "K", 4000, 8.0)]),
                           season=2026, week=2)
        assert list(out["gsis_id"]) == ["DST_KC", None]

    def test_a_week_with_no_reference_leaves_the_pool_unresolved(self, conn):
        out = resolve_pool(conn, pool([("dk1", "Bo Nix", "DEN", "QB", 10000, 19.0)]),
                           season=2030, week=1)
        # nothing rostered in 2030 or 2029, and no stats: falls through, no crash
        assert out.iloc[0]["gsis_id"] is None or out.iloc[0]["gsis_id"] == "00-001"

    def test_legacy_path_is_unchanged_without_a_week(self, rostered):
        out = resolve_pool(rostered, pool([("dk1", "Bo Nix", "DEN", "QB", 10000, 19.0)]))
        assert out.iloc[0]["gsis_id"] == "00-001"
        assert bool(out.iloc[0]["team_changed"]) is False
