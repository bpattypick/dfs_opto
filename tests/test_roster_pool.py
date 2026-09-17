"""T23 + T22: the backtest scored on the full dressed roster, with roles.

The played pool conditions every number on having played — the one thing a
pre-lock projection cannot know, and the thing three live losses turned on.
These tests pin down the roster pool (who is in it, that a dressed player with
no stat line scores 0 against his projection, the coverage numbers), the role
columns that ride along (depth rank, rank among dressed teammates, injury
listing, zero rows in history), the RoleAware model on the cases that lost
live, and that the leakage guarantee holds in the new mode.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from src import db
from src.backtest import harness, history
from src.backtest.__main__ import main as backtest_main
from src.projection import PriorAverage, ProjectionError, RoleAware, role_bucket

# Fixture world. AAA hosts BBB every week; CCC is on bye in 2024 w2.
#   p_qb, p_wr    AAA starters with history and a stat line every week
#   p_bench       AAA backup QB (depth 2): dresses every week, 0 snaps in 2024 w2
#   p_demoted     BBB's starting QB through 2024 w1, depth 2 from w2 on -> Fields
#   p_newqb       BBB's new starter from 2024 w2, no history at all
#   p_new         rookie RB, first stat line 2024 w2; depth 2 behind p_ina, who is
#                 INACTIVE that week -> rank 1 among the dressed (the Price case)
#   p_rb1         BBB RB1 through 2023, on reserve in 2024 -> supplies the RB1 prior
#   p_ir          on reserve (RES) in 2024 -> not in the pool
#   p_ps          practice squad (DEV), never played -> not in the pool
#   p_bye         ACT for CCC, no game -> not in the pool
PLAYERS = [
    ("p_qb", "Ace QB", "QB"), ("p_wr", "Bo WR", "WR"), ("p_bench", "Backup QB", "QB"),
    ("p_demoted", "Demoted QB", "QB"), ("p_newqb", "New QB", "QB"),
    ("p_new", "Rookie RB", "RB"), ("p_rb1", "Old RB1", "RB"), ("p_ir", "Hurt WR", "WR"),
    ("p_ina", "Scratch RB", "RB"),
    ("DST_AAA", "AAA D/ST", "DST"), ("DST_BBB", "BBB D/ST", "DST"),
]
# (player, season, week, team, dk_points, snaps)
STATS = [
    ("p_qb", 2023, 1, "AAA", 20.0, 60), ("p_qb", 2023, 2, "AAA", 30.0, 60),
    ("p_qb", 2023, 3, "AAA", 10.0, 60), ("p_qb", 2024, 1, "AAA", 40.0, 60),
    ("p_qb", 2024, 2, "AAA", 25.0, 60), ("p_qb", 2024, 3, "AAA", 99.0, 60),
    ("p_wr", 2023, 1, "AAA", 10.0, 50), ("p_wr", 2023, 2, "AAA", 14.0, 50),
    ("p_wr", 2023, 3, "AAA", 12.0, 50), ("p_wr", 2024, 1, "AAA", 16.0, 50),
    ("p_wr", 2024, 2, "AAA", 18.0, 50), ("p_wr", 2024, 3, "AAA", 50.0, 50),
    ("p_bench", 2023, 1, "AAA", 4.0, 10), ("p_bench", 2023, 2, "AAA", 6.0, 10),
    ("p_bench", 2023, 3, "AAA", 5.0, 10), ("p_bench", 2024, 1, "AAA", 3.0, 10),
    ("p_bench", 2024, 3, "AAA", 7.0, 10),           # no 2024 w2 row: dressed, did not play
    ("p_demoted", 2023, 1, "BBB", 20.0, 60), ("p_demoted", 2023, 2, "BBB", 22.0, 60),
    ("p_demoted", 2023, 3, "BBB", 18.0, 60), ("p_demoted", 2024, 1, "BBB", 21.0, 60),
    ("p_newqb", 2024, 2, "BBB", 19.0, 60), ("p_newqb", 2024, 3, "BBB", 17.0, 60),
    ("p_new", 2024, 2, "BBB", 5.0, 30), ("p_new", 2024, 3, "BBB", 6.0, 30),
    ("p_rb1", 2023, 1, "BBB", 12.0, 40), ("p_rb1", 2023, 2, "BBB", 10.0, 40),
    ("p_rb1", 2023, 3, "BBB", 14.0, 40),
    ("p_ir", 2023, 1, "BBB", 9.0, 40), ("p_ir", 2023, 2, "BBB", 9.0, 40),
    ("p_ir", 2023, 3, "BBB", 9.0, 40),
] + [
    (f"DST_{t}", s, w, t, pts, None)
    for t, pts in (("AAA", 8.0), ("BBB", 4.0))
    for s, w in ((2023, 1), (2023, 2), (2023, 3), (2024, 1), (2024, 2), (2024, 3))
]
GAMES = [(f"{s}_{w:02d}_BBB_AAA", s, w, "AAA", "BBB", 3.0, 46.0)
         for s in (2023, 2024) for w in (1, 2, 3)]
# (player, season, week, team, position, status)
ROSTERS = (
    [(p, 2023, w, "AAA", pos, "ACT") for p, pos in (("p_qb", "QB"), ("p_wr", "WR"), ("p_bench", "QB"))
     for w in (1, 2, 3)]
    + [(p, 2023, w, "BBB", pos, "ACT") for p, pos in (("p_demoted", "QB"), ("p_rb1", "RB"), ("p_ir", "WR"))
       for w in (1, 2, 3)]
    + [(p, 2024, w, "AAA", pos, "ACT") for p, pos in (("p_qb", "QB"), ("p_wr", "WR"), ("p_bench", "QB"))
       for w in (1, 2, 3)]
    + [("p_demoted", 2024, w, "BBB", "QB", "ACT") for w in (1, 2, 3)]
    + [(p, 2024, w, "BBB", pos, "ACT") for p, pos in (("p_newqb", "QB"), ("p_new", "RB")) for w in (2, 3)]
    + [(p, 2024, w, "BBB", pos, "RES") for p, pos in (("p_rb1", "RB"), ("p_ir", "WR")) for w in (1, 2, 3)]
    + [("p_ina", 2024, 2, "BBB", "RB", "INA"), ("p_ps", 2024, 2, "BBB", "WR", "DEV"),
       ("p_bye", 2024, 2, "CCC", "TE", "ACT")]
)
# (player, season, week, team, position, depth_rank, injury_status)
ROLES = (
    [("p_qb", s, w, "AAA", "QB", 1, None) for s in (2023, 2024) for w in (1, 2, 3)]
    + [("p_bench", s, w, "AAA", "QB", 2, None) for s in (2023, 2024) for w in (1, 2, 3)]
    + [("p_wr", s, w, "AAA", "WR", 1, "Questionable" if (s, w) == (2024, 2) else None)
       for s in (2023, 2024) for w in (1, 2, 3)]
    + [("p_demoted", 2023, w, "BBB", "QB", 1, None) for w in (1, 2, 3)]
    + [("p_demoted", 2024, 1, "BBB", "QB", 1, None), ("p_demoted", 2024, 2, "BBB", "QB", 2, None),
       ("p_demoted", 2024, 3, "BBB", "QB", 2, None)]
    + [("p_newqb", 2024, w, "BBB", "QB", 1, None) for w in (2, 3)]
    + [("p_rb1", 2023, w, "BBB", "RB", 1, None) for w in (1, 2, 3)]
    + [("p_ir", 2023, w, "BBB", "WR", 1, None) for w in (1, 2, 3)]
    + [("p_new", 2024, 2, "BBB", "RB", 2, None), ("p_new", 2024, 3, "BBB", "RB", 1, None),
       ("p_ina", 2024, 2, "BBB", "RB", 1, None)]
)


def populate(conn) -> None:
    db.create_schema(conn)
    db.upsert(conn, "players", [
        {"player_id": p, "name": n, "position": pos, "first_season": 2023}
        for p, n, pos in PLAYERS
    ])
    db.upsert(conn, "games", [
        {"game_id": g, "season": s, "week": w, "home_team": h, "away_team": a,
         "spread_line": sp, "total_line": t} for g, s, w, h, a, sp, t in GAMES
    ])
    db.upsert(conn, "player_week_stats", [
        {"player_id": p, "season": s, "week": w, "team": t, "dk_points": pts,
         "snaps": sn, "snap_pct": None if sn is None else sn / 60,
         "game_id": f"{s}_{w:02d}_BBB_AAA", "opponent": "BBB" if t == "AAA" else "AAA"}
        for p, s, w, t, pts, sn in STATS
    ])
    db.upsert(conn, "rosters", [
        {"player_id": p, "season": s, "week": w, "team": t, "position": pos,
         "status": st, "name": p}
        for p, s, w, t, pos, st in ROSTERS
    ])
    db.upsert(conn, "roles", [
        {"player_id": p, "season": s, "week": w, "team": t, "position": pos,
         "depth_rank": dr, "injury_status": inj}
        for p, s, w, t, pos, dr, inj in ROLES
    ])


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    populate(conn)
    yield conn
    conn.close()


W = (2024, 2)
POOL_W2 = {"p_qb", "p_wr", "p_bench", "p_demoted", "p_newqb", "p_new", "DST_AAA", "DST_BBB"}
# Bucket priors from history before 2024 w2 (hand-computed from STATS above).
QB1_PRIOR = (20 + 30 + 10 + 40 + 20 + 22 + 18 + 21) / 8      # 22.625
QB2_PRIOR = (4 + 6 + 5 + 3) / 4                                # 4.5
RB1_PRIOR = (12 + 10 + 14) / 3                                 # 12
WR1_PRIOR = (10 + 14 + 12 + 16 + 9 + 9 + 9) / 7                # 11.2857


class TestRosterSlate:
    def test_pool_is_dressed_players_on_teams_with_a_game_plus_both_dsts(self, conn):
        s = history.slate(conn, *W, pool="roster")
        assert set(s.player_id) == POOL_W2

    def test_reserve_practice_squad_inactive_and_bye_are_out(self, conn):
        ids = set(history.slate(conn, *W, pool="roster").player_id)
        assert not ids & {"p_ir", "p_ps", "p_ina", "p_bye", "p_rb1"}

    def test_pre_lock_columns_only_and_no_scores(self, conn):
        s = history.slate(conn, *W, pool="roster")
        assert list(s.columns) == history.ROSTER_SLATE_COLUMNS
        assert "dk_points" not in s.columns
        assert s.set_index("team").loc["AAA", "implied_total"].iloc[0] == pytest.approx(24.5)

    def test_position_and_opponent_come_from_the_roster_and_schedule(self, conn):
        s = history.slate(conn, *W, pool="roster").set_index("player_id")
        assert s.loc["p_new", "position"] == "RB"          # no players row needed
        assert s.loc["p_new", "opponent"] == "AAA"
        assert s.loc["DST_BBB", "position"] == "DST"

    def test_the_played_pool_is_unchanged_by_default(self, conn):
        s = history.slate(conn, *W)
        assert set(s.player_id) == {"p_qb", "p_wr", "p_newqb", "p_new", "DST_AAA", "DST_BBB"}
        assert list(s.columns) == history.SLATE_COLUMNS

    def test_an_unknown_pool_is_refused(self, conn):
        with pytest.raises(ValueError, match="unknown pool"):
            history.slate(conn, *W, pool="everyone")
        with pytest.raises(ValueError, match="unknown pool"):
            history.as_of(conn, *W, pool="everyone")

    def test_a_week_with_no_rosters_is_empty_and_the_harness_says_so(self, conn):
        assert history.slate(conn, 2022, 2, pool="roster").empty
        conn.execute("DELETE FROM rosters WHERE season = 2023")
        with pytest.raises(harness.BacktestError, match="no roster rows"):
            harness.evaluate_week(conn, PriorAverage(), 2023, 2, pool="roster")


class TestRoles:
    def test_slate_carries_the_weeks_depth_rank_and_injury_listing(self, conn):
        s = history.slate(conn, *W, pool="roster").set_index("player_id")
        assert s.loc["p_demoted", "depth_rank"] == 2
        assert s.loc["p_wr", "injury_status"] == "Questionable"
        assert pd.isna(s.loc["p_qb", "injury_status"])
        assert pd.isna(s.loc["DST_AAA", "depth_rank"])

    def test_effective_rank_is_among_the_dressed_only(self, conn):
        # p_ina is depth 1 at RB but inactive; p_new (depth 2) is the RB1 who dressed.
        s = history.slate(conn, *W, pool="roster").set_index("player_id")
        assert s.loc["p_new", "eff_rank"] == 1
        assert s.loc["p_demoted", "eff_rank"] == 2
        assert s.loc["p_newqb", "eff_rank"] == 1
        assert s.loc["p_bench", "eff_rank"] == 2
        assert s.loc["DST_AAA", "eff_rank"] == 1

    def test_effective_ranks_break_ties_by_prior_snap_share_then_unknown_last(self):
        frame = pd.DataFrame({
            "season": 2024, "week": 1, "team": "T", "position": "WR",
            "player_id": ["a", "b", "c", "d"],
            "depth_rank": [1, 1, 2, None],
            "prior_snap": [0.5, 0.9, None, 0.99],
        })
        assert list(history.effective_ranks(frame)) == [2, 1, 3, 4]

    def test_roster_history_has_a_zero_row_for_a_dressed_silent_week(self, conn):
        h = history.as_of(conn, 2024, 3, pool="roster")
        row = h[(h.player_id == "p_demoted") & (h.season == 2024) & (h.week == 2)].iloc[0]
        assert row.dk_points == 0.0 and row.snaps == 0
        assert row.eff_rank == 2 and row.depth_rank == 2
        assert row.team == "BBB" and row.opponent == "AAA"
        assert list(h.columns) == list(history.HISTORY_COLUMNS) + list(history.ROLE_COLUMNS)

    def test_roster_history_is_still_strictly_before_the_week(self, conn):
        h = history.as_of(conn, *W, pool="roster")
        assert not ((h.season == 2024) & (h.week >= 2)).any()
        assert len(h) == 30                                  # no silent weeks before 2024 w2

    def test_played_history_is_byte_for_byte_the_original(self, conn):
        h = history.as_of(conn, 2024, 3)
        assert list(h.columns) == list(history.HISTORY_COLUMNS)
        assert not ((h.player_id == "p_demoted") & (h.week == 2) & (h.season == 2024)).any()

    def test_role_bucket_caps_depth_and_marks_unknown(self):
        assert role_bucket("QB", 1) == "QB1" and role_bucket("QB", 3) == "QB2"
        assert role_bucket("WR", 9) == "WR4" and role_bucket("RB", 2) == "RB2"
        assert role_bucket("TE", float("nan")) == "TE?" and role_bucket("WR", None) == "WR?"


class TestRoleAware:
    def project(self, conn, W=W, **kw):
        m = RoleAware(**kw)
        return m, m.project(history.as_of(conn, *W, pool="roster"),
                            history.slate(conn, *W, pool="roster")).set_index("player_id")["projection"]

    def test_a_demoted_starter_projects_as_a_backup_not_his_history(self, conn):
        # p_demoted averaged 20.25 as a starter; this week he is QB2.
        _, p = self.project(conn)
        assert p["p_demoted"] == pytest.approx(QB2_PRIOR)
        base = PriorAverage().project(history.as_of(conn, *W), history.slate(conn, *W, pool="roster"))
        assert base.set_index("player_id").loc["p_demoted", "projection"] == pytest.approx(20.25)

    def test_a_rookie_thrust_into_the_starting_role_gets_that_roles_prior(self, conn):
        _, p = self.project(conn)
        assert p["p_new"] == pytest.approx(RB1_PRIOR)          # Price, not 0.0
        assert p["p_newqb"] == pytest.approx(QB1_PRIOR)

    def test_a_starter_is_mostly_himself_shrunk_toward_the_role_prior(self, conn):
        _, p = self.project(conn, k=3.0)
        w = 4 / (4 + 3)
        assert p["p_qb"] == pytest.approx(w * 25.0 + (1 - w) * QB1_PRIOR)

    def test_k_zero_is_the_same_role_trailing_mean(self, conn):
        _, p = self.project(conn, k=0.0)
        assert p["p_qb"] == pytest.approx(25.0)
        assert p["p_demoted"] == pytest.approx(QB2_PRIOR)       # still no same-role games

    def test_a_backup_with_backup_history_stays_a_backup(self, conn):
        _, p = self.project(conn)
        assert p["p_bench"] == pytest.approx(4.5)

    def test_injury_listing_scales_the_projection(self, conn):
        _, full = self.project(conn, questionable=1.0)
        _, cut = self.project(conn, questionable=0.8)
        assert cut["p_wr"] == pytest.approx(0.8 * full["p_wr"])
        conn.execute("UPDATE roles SET injury_status = 'Out' WHERE player_id = 'p_wr' AND season = 2024 AND week = 2")
        _, out = self.project(conn)
        assert out["p_wr"] == 0.0

    def test_zero_rows_lower_the_role_prior_the_following_week(self, conn):
        m, _ = self.project(conn, W=(2024, 3))
        # QB2 prior now includes the two dressed-silent zeros from 2024 w2.
        assert m.last_priors["QB2"] == pytest.approx((4 + 6 + 5 + 3 + 0 + 0) / 6)

    def test_refuses_history_without_role_columns(self, conn):
        with pytest.raises(ProjectionError, match="role columns"):
            RoleAware().project(history.as_of(conn, *W), history.slate(conn, *W, pool="roster"))
        with pytest.raises(ProjectionError, match="role columns"):
            RoleAware().project(history.as_of(conn, *W, pool="roster"), history.slate(conn, *W))

    def test_negative_k_is_refused(self, conn):
        with pytest.raises(ProjectionError, match="non-negative"):
            self.project(conn, k=-1)


class TestRosterHarness:
    def test_a_dressed_player_with_no_stat_line_scores_zero(self, conn):
        rows = harness.evaluate_week(conn, PriorAverage(), *W, pool="roster").set_index("player_id")
        assert rows.loc["p_bench", "actual"] == 0.0
        # trailing mean of 4, 6, 5, 3 — the model promised 4.5 and got 0
        assert rows.loc["p_bench", "projection"] == pytest.approx(4.5)
        assert rows.loc["p_demoted", "actual"] == 0.0

    def test_the_played_pool_cannot_see_him(self, conn):
        rows = harness.evaluate_week(conn, PriorAverage(), *W, pool="played")
        assert not {"p_bench", "p_demoted"} & set(rows.player_id)

    def test_evaluated_rows_carry_game_id_for_the_showdown_slice(self, conn):
        rows = harness.evaluate_week(conn, PriorAverage(), *W, pool="roster")
        assert set(rows.game_id) == {"2024_02_BBB_AAA"}

    def test_min_games_still_gates_who_is_scored(self, conn):
        rows = harness.evaluate_week(conn, PriorAverage(), *W, min_games=3, pool="roster")
        assert set(rows.player_id) == POOL_W2 - {"p_newqb", "p_new"}

    def test_the_harness_honours_a_models_history_pool(self, conn):
        seen = {}

        class Spy:
            name = "spy"
            history_pool = "roster"

            def project(self, hist, slate):
                seen["zero"] = hist[(hist.player_id == "p_demoted") & (hist.season == 2024) & (hist.week == 2)]
                seen["cols"] = set(hist.columns)
                return pd.DataFrame({"player_id": slate.player_id, "projection": 1.0})

        harness.evaluate_week(conn, Spy(), 2024, 3, pool="roster")
        assert len(seen["zero"]) == 1 and seen["zero"].dk_points.iloc[0] == 0.0
        assert "eff_rank" in seen["cols"]

    def test_role_aware_scores_everyone_with_min_games_zero(self, conn):
        res = harness.run(conn, RoleAware(), seasons=[2024], weeks=[2], pool="roster",
                          min_games=0, persist=False)
        cov = res.metrics["coverage"]
        assert cov["pool_n"] == 8 and cov["evaluated_n"] == 8 and cov["excluded_n"] == 0
        rows = res.rows.set_index("player_id")
        assert rows.loc["p_new", "projection"] == pytest.approx(RB1_PRIOR)

    def test_coverage_reports_who_could_not_be_scored_and_what_they_carried(self, conn):
        res = harness.run(conn, PriorAverage(), seasons=[2024], weeks=[2], pool="roster",
                          persist=False)
        cov = res.metrics["coverage"]
        assert cov["pool_n"] == 8
        assert cov["evaluated_n"] == 6
        assert cov["excluded_n"] == 2                       # p_newqb, p_new: no history
        # they scored 19 + 5 of the pool's 25 + 18 + 0 + 0 + 19 + 5 + 8 + 4 = 79
        assert cov["points_unseen_share"] == pytest.approx(24 / 79)

    def test_coverage_reports_the_zero_scorers_and_what_they_were_promised(self, conn):
        res = harness.run(conn, PriorAverage(), seasons=[2024], weeks=[2], pool="roster",
                          persist=False)
        cov = res.metrics["coverage"]
        assert cov["zero_rate"] == pytest.approx(2 / 6)
        assert cov["zero_mean_projection"] == pytest.approx((4.5 + 20.25) / 2)

    def test_showdown_slice_is_the_projected_top_per_game(self, conn):
        res = harness.run(conn, PriorAverage(), seasons=[2024], weeks=[2], pool="roster",
                          persist=False)
        top = res.metrics["showdown_top12"]
        assert top["n"] == 6                                # one game, fewer than 12 scored
        assert top["mae"] == pytest.approx(res.metrics["mae"])

    def test_the_pool_is_recorded_in_metrics_config_and_the_persisted_run(self, conn):
        res = harness.run(conn, PriorAverage(), seasons=[2024], weeks=[2], pool="roster")
        assert res.metrics["pool"] == "roster"
        assert res.config["pool"] == "roster"
        stored = conn.execute("SELECT config_json FROM backtest_runs").fetchone()[0]
        assert json.loads(stored)["pool"] == "roster"
        assert "pool=roster" in res.summary()
        assert "roster" in harness.compare([res])

    def test_a_played_run_is_labelled_and_still_reports_coverage(self, conn):
        res = harness.run(conn, PriorAverage(), seasons=[2024], weeks=[2], persist=False)
        assert res.metrics["pool"] == "played"
        assert res.metrics["coverage"]["pool_n"] == 6      # the stat rows
        assert res.metrics["coverage"]["zero_rate"] == 0.0

    def test_an_unknown_pool_is_refused_by_run(self, conn):
        with pytest.raises(harness.BacktestError, match="unknown pool"):
            harness.run(conn, PriorAverage(), seasons=[2024], pool="everyone", persist=False)

    def test_the_cli_accepts_the_pool_and_the_role_model(self, tmp_path, capsys):
        path = tmp_path / "t.sqlite"
        c = db.connect(path); populate(c); c.commit(); c.close()
        rc = backtest_main(["--model", "prior_average", "--model", "role_aware",
                            "--seasons", "2024", "2024", "--pool", "roster",
                            "--db", str(path), "--no-persist"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "pool=roster" in out and "role_aware" in out


class TestRosterLeakage:
    """Spec §7's spot-check, in roster mode: delete the future, output unchanged."""

    @pytest.mark.parametrize("model", [PriorAverage(), RoleAware()])
    def test_deleting_future_scores_rosters_and_roles_does_not_change_the_projection(self, conn, model):
        before = harness.evaluate_week(conn, model, *W, pool="roster", min_games=0)

        conn.execute("UPDATE player_week_stats SET dk_points = dk_points * 100 "
                     "WHERE season > ? OR (season = ? AND week >= ?)", (W[0], W[0], W[1]))
        conn.execute("DELETE FROM player_week_stats WHERE season = ? AND week > ?", W)
        conn.execute("DELETE FROM rosters WHERE season = ? AND week > ?", W)
        conn.execute("DELETE FROM roles WHERE season = ? AND week > ?", W)
        conn.execute("UPDATE roles SET depth_rank = 1, injury_status = 'Out' "
                     "WHERE season = ? AND week > ?", W)
        after = harness.evaluate_week(conn, model, *W, pool="roster", min_games=0)

        pd.testing.assert_frame_equal(
            before[["player_id", "projection"]].sort_values("player_id").reset_index(drop=True),
            after[["player_id", "projection"]].sort_values("player_id").reset_index(drop=True),
        )

    def test_the_harness_never_hands_a_model_the_target_scores(self, conn):
        seen = {}

        class Spy:
            name = "spy"
            history_pool = "roster"

            def project(self, hist, slate):
                seen["hist_max"] = max(zip(hist.season, hist.week))
                seen["slate_cols"] = set(slate.columns)
                seen["slate_ids"] = set(slate.player_id)
                return pd.DataFrame({"player_id": slate.player_id, "projection": 1.0})

        harness.evaluate_week(conn, Spy(), *W, pool="roster")
        assert seen["hist_max"] == (2024, 1)
        assert "dk_points" not in seen["slate_cols"]
        assert "p_bench" in seen["slate_ids"]                # the model is asked about him
