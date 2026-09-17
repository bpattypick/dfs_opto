"""T22 ingest: depth charts (two feed formats) and injury reports into `roles`.

Every value here is a pre-lock fact. The one place look-ahead could creep in
is the daily-snapshot format, where the snapshot used for a week must be the
last one *before* that team's kickoff -- pinned below.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src import db, nflverse
from src.ingest import nfl_stats

GAMES = pd.DataFrame([
    {"season": 2025, "week": 1, "home_team": "KC", "away_team": "DEN",
     "kickoff_utc": "2025-09-07T20:25:00+00:00"},
    {"season": 2025, "week": 2, "home_team": "DEN", "away_team": "KC",
     "kickoff_utc": "2025-09-14T20:25:00+00:00"},
    # SEA on bye in week 2: only a week-1 game
    {"season": 2025, "week": 1, "home_team": "SEA", "away_team": "NE",
     "kickoff_utc": "2025-09-08T00:20:00+00:00"},
    {"season": 2025, "week": 1, "home_team": "LAR", "away_team": "SF",
     "kickoff_utc": "2025-09-07T20:25:00+00:00"},
])


def weekly_format() -> pd.DataFrame:
    """The 2020-2024 shape: depth_team is the rank, starters tie."""
    rows = []
    for w in (1, 2):
        rows += [
            dict(season=2024, club_code="KC", week=float(w), game_type="REG", depth_team="1",
                 formation="Offense", gsis_id="00-mahomes", position="QB", depth_position="QB",
                 full_name="Patrick Mahomes"),
            dict(season=2024, club_code="KC", week=float(w), game_type="REG", depth_team="2",
                 formation="Offense", gsis_id="00-wentz", position="QB", depth_position="QB",
                 full_name="Carson Wentz"),
            dict(season=2024, club_code="KC", week=float(w), game_type="REG", depth_team="1",
                 formation="Offense", gsis_id="00-rice", position="WR", depth_position="WR",
                 full_name="Rashee Rice"),
            dict(season=2024, club_code="KC", week=float(w), game_type="REG", depth_team="1",
                 formation="Offense", gsis_id="00-worthy", position="WR", depth_position="WR",
                 full_name="Xavier Worthy"),
            # listed twice: as FB depth 2 and RB depth 3 -> keeps the better rank, as RB
            dict(season=2024, club_code="KC", week=float(w), game_type="REG", depth_team="2",
                 formation="Offense", gsis_id="00-fb", position="FB", depth_position="FB",
                 full_name="A Fullback"),
            dict(season=2024, club_code="KC", week=float(w), game_type="REG", depth_team="3",
                 formation="Offense", gsis_id="00-fb", position="RB", depth_position="RB",
                 full_name="A Fullback"),
            dict(season=2024, club_code="LA", week=float(w), game_type="REG", depth_team="1",
                 formation="Offense", gsis_id="00-nacua", position="WR", depth_position="WR",
                 full_name="Puka Nacua"),
            # not offense / not a skill position / playoffs: all dropped
            dict(season=2024, club_code="KC", week=float(w), game_type="REG", depth_team="1",
                 formation="Defense", gsis_id="00-jones", position="DT", depth_position="DT",
                 full_name="Chris Jones"),
            dict(season=2024, club_code="KC", week=float(w), game_type="REG", depth_team="1",
                 formation="Offense", gsis_id="00-lt", position="T", depth_position="LT",
                 full_name="A Tackle"),
        ]
    rows.append(dict(season=2024, club_code="KC", week=19.0, game_type="WC", depth_team="1",
                     formation="Offense", gsis_id="00-mahomes", position="QB",
                     depth_position="QB", full_name="Patrick Mahomes"))
    return pd.DataFrame(rows)


def daily_format() -> pd.DataFrame:
    """The 2025+ shape: dated snapshots, pos_rank ordinal within the position group."""
    def snap(dt, team, players):
        return [dict(dt=dt, team=team, player_name=n, gsis_id=g, pos_grp="3WR 1TE",
                     pos_abb=pos, pos_slot=slot, pos_rank=rank)
                for n, g, pos, slot, rank in players]

    rows = []
    # Week 1 (KC kicks off 2025-09-07 20:25Z): snapshots on the 5th, 7th morning, and
    # one AFTER kickoff the same day that must not be used.
    rows += snap("2025-09-05T07:15:00Z", "KC", [("Patrick Mahomes", "00-mahomes", "QB", 9, 1),
                                                 ("Justin Fields", "00-fields", "QB", 9, 2),
                                                 ("Rashee Rice", "00-rice", "WR", 1, 1),
                                                 ("Xavier Worthy", "00-worthy", "WR", 2, 2)])
    rows += snap("2025-09-07T07:15:00Z", "KC", [("Patrick Mahomes", "00-mahomes", "QB", 9, 1),
                                                 ("Justin Fields", "00-fields", "QB", 9, 3),
                                                 ("Rashee Rice", "00-rice", "WR", 1, 1),
                                                 ("Rashee Rice", "00-rice", "WR", 8, 4),
                                                 ("Xavier Worthy", "00-worthy", "WR", 2, 2)])
    rows += snap("2025-09-07T23:00:00Z", "KC", [("Patrick Mahomes", "00-mahomes", "QB", 9, 1),
                                                 ("Justin Fields", "00-fields", "QB", 9, 9)])
    # Week 2 (kickoff 2025-09-14): one snapshot the morning of.
    rows += snap("2025-09-14T07:15:00Z", "KC", [("Patrick Mahomes", "00-mahomes", "QB", 9, 1),
                                                 ("Justin Fields", "00-fields", "QB", 9, 2)])
    # DEN: only a snapshot from three weeks before its week-2 game -> too stale to use
    rows += snap("2025-08-24T07:15:00Z", "DEN", [("Bo Nix", "00-nix", "QB", 9, 1)])
    # SEA: week-1 snapshot, and a later one that has no game to attach to (bye)
    rows += snap("2025-09-07T07:15:00Z", "SEA", [("Sam Darnold", "00-darnold", "QB", 9, 1)])
    rows += snap("2025-09-13T07:15:00Z", "SEA", [("Sam Darnold", "00-darnold", "QB", 9, 1)])
    # LA team code, a defender, and a row with no gsis_id
    rows += snap("2025-09-07T07:15:00Z", "LA", [("Puka Nacua", "00-nacua", "WR", 1, 1),
                                                 ("A Corner", "00-cb", "LCB", 5, 1),
                                                 ("No Id", None, "TE", 3, 2)])
    return pd.DataFrame(rows)


def injuries() -> pd.DataFrame:
    return pd.DataFrame([
        dict(season=2025, game_type="REG", team="KC", week=1, gsis_id="00-rice", position="WR",
             full_name="Rashee Rice", report_status="Questionable",
             practice_status="Limited Participation in Practice"),
        dict(season=2025, game_type="REG", team="KC", week=1, gsis_id="00-rice", position="WR",
             full_name="Rashee Rice", report_status="Out",
             practice_status="Did Not Participate In Practice"),   # later listing: most severe wins
        dict(season=2025, game_type="REG", team="LA", week=1, gsis_id="00-kupp", position="WR",
             full_name="Cooper Kupp", report_status="Doubtful", practice_status=None),
        dict(season=2025, game_type="REG", team="KC", week=1, gsis_id="00-jones", position="DT",
             full_name="Chris Jones", report_status="Out", practice_status=None),
        dict(season=2025, game_type="WC", team="KC", week=19, gsis_id="00-rice", position="WR",
             full_name="Rashee Rice", report_status="Out", practice_status=None),
        dict(season=2025, game_type="REG", team="KC", week=1, gsis_id="00-mahomes", position="QB",
             full_name="Patrick Mahomes", report_status=None,
             practice_status="Full Participation in Practice"),
    ])


class TestWeeklyFormat:
    def test_rank_position_and_team_are_normalized(self):
        out = nfl_stats._transform_depth_charts(weekly_format(), GAMES, ["REG"]).set_index(
            ["player_id", "week"])
        assert out.loc[("00-mahomes", 1), "depth_rank"] == 1
        assert out.loc[("00-wentz", 1), "depth_rank"] == 2
        assert out.loc[("00-nacua", 1), "team"] == "LAR"
        assert out.loc[("00-fb", 1), "position"] == "RB"
        assert out.loc[("00-fb", 1), "depth_rank"] == 2          # best rank across listings
        assert (out["depth_as_of"] == "week").all()

    def test_starters_tie_in_this_format(self):
        out = nfl_stats._transform_depth_charts(weekly_format(), GAMES, ["REG"])
        wr = out[(out.position == "WR") & (out.week == 1) & (out.team == "KC")]
        assert list(wr.depth_rank) == [1, 1]

    def test_defense_linemen_and_playoffs_are_dropped(self):
        out = nfl_stats._transform_depth_charts(weekly_format(), GAMES, ["REG"])
        assert "00-jones" not in set(out.player_id)
        assert "00-lt" not in set(out.player_id)
        assert set(out.week) == {1, 2}


class TestDailyFormat:
    def out(self):
        return nfl_stats._transform_depth_charts(daily_format(), GAMES, ["REG"])

    def test_uses_the_last_snapshot_before_kickoff_never_after(self):
        out = self.out().set_index(["player_id", "week"])
        # the 07:15 snapshot on game day, not the 5th and not the 23:00 post-game one
        assert out.loc[("00-fields", 1), "depth_rank"] == 3
        assert out.loc[("00-fields", 1), "depth_as_of"] == "2025-09-07T07:15:00Z"
        assert out.loc[("00-fields", 2), "depth_rank"] == 2

    def test_a_player_in_several_slots_keeps_his_best_rank(self):
        out = self.out().set_index(["player_id", "week"])
        assert out.loc[("00-rice", 1), "depth_rank"] == 1

    def test_a_bye_week_produces_no_row(self):
        out = self.out()
        assert set(out[out.player_id == "00-darnold"].week) == {1}

    def test_a_snapshot_older_than_a_week_is_not_attached_to_a_game(self):
        # The live season's newest snapshot must not be pinned to every future week.
        out = self.out()
        assert "00-nix" not in set(out.player_id)

    def test_team_codes_defenders_and_idless_rows(self):
        out = self.out()
        assert set(out[out.player_id == "00-nacua"].team) == {"LAR"}
        assert "00-cb" not in set(out.player_id)
        assert out.player_id.notna().all()


class TestInjuries:
    def test_most_severe_listing_wins_and_only_known_statuses_are_kept(self):
        out = nfl_stats._transform_injuries(injuries(), ["REG"]).set_index(["player_id", "week"])
        assert out.loc[("00-rice", 1), "injury_status"] == "Out"
        assert out.loc[("00-kupp", 1), "injury_status"] == "Doubtful"
        assert pd.isna(out.loc[("00-mahomes", 1), "injury_status"])
        assert out.loc[("00-kupp", 1), "team"] == "LAR"

    def test_playoff_rows_are_dropped(self):
        out = nfl_stats._transform_injuries(injuries(), ["REG"])
        assert set(out.week) == {1}


class TestMergedRoles:
    def test_depth_and_injury_rows_union_on_player_week(self):
        depth = nfl_stats._transform_depth_charts(daily_format(), GAMES, ["REG"])
        inj = nfl_stats._transform_injuries(injuries(), ["REG"])
        roles = nfl_stats._merge_roles(depth, inj).set_index(["player_id", "week"])
        assert roles.loc[("00-rice", 1), "depth_rank"] == 1
        assert roles.loc[("00-rice", 1), "injury_status"] == "Out"
        assert pd.isna(roles.loc[("00-kupp", 1), "depth_rank"])   # injury report only
        assert roles.loc[("00-kupp", 1), "position"] == "WR"
        assert "00-jones" not in roles.index.get_level_values(0)   # DT never enters
        assert pd.isna(roles.loc[("00-mahomes", 2), "injury_status"])


class TestLoadRoles:
    @pytest.fixture
    def conn(self):
        conn = db.connect(":memory:")
        db.create_schema(conn)
        db.upsert(conn, "games", [
            {"game_id": f"2025_{int(r.week):02d}_{r.away_team}_{r.home_team}", "season": r.season,
             "week": r.week, "home_team": r.home_team, "away_team": r.away_team,
             "kickoff_utc": r.kickoff_utc} for r in GAMES.itertuples()
        ])
        yield conn
        conn.close()

    def test_loads_and_is_idempotent(self, conn, monkeypatch):
        monkeypatch.setattr(nflverse, "depth_charts",
                            lambda season, refresh=False, cfg=None, today=None: daily_format())
        monkeypatch.setattr(nflverse, "injuries",
                            lambda season, refresh=False, cfg=None, today=None: injuries())
        n1 = nfl_stats.load_roles(conn, [2025])
        n2 = nfl_stats.load_roles(conn, [2025])
        assert n1 == n2 > 0
        rows = pd.read_sql_query("SELECT * FROM roles", conn)
        assert len(rows) == n1
        assert set(rows.columns) >= {"depth_rank", "injury_status", "depth_as_of"}

    def test_a_missing_feed_warns_and_the_other_still_loads(self, conn, monkeypatch):
        def missing(season, refresh=False, cfg=None, today=None):
            raise nflverse.NflverseUnavailable("not published")

        monkeypatch.setattr(nflverse, "depth_charts", missing)
        monkeypatch.setattr(nflverse, "injuries",
                            lambda season, refresh=False, cfg=None, today=None: injuries())
        assert nfl_stats.load_roles(conn, [2025]) > 0
        rows = pd.read_sql_query("SELECT * FROM roles", conn)
        assert rows.depth_rank.isna().all()
        assert set(rows.injury_status.dropna()) == {"Out", "Doubtful"}
