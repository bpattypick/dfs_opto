"""T23: the backtest scored on the full dressed roster, not just stat-recorders.

The played pool conditions every number on having played — the one thing a
pre-lock projection cannot know, and the thing three live losses turned on.
These tests pin down the roster pool: who is in it, that a dressed player with
no stat line scores 0 against his projection, what the coverage numbers say,
and that the leakage guarantee holds in the new mode.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from src import db
from src.backtest import harness, history
from src.backtest.__main__ import main as backtest_main
from src.projection import PriorAverage

# Fixture world. AAA hosts BBB every week; CCC is on bye in 2024 w2.
#   p_qb, p_wr    regular starters with history and a stat line every week
#   p_bench       backup QB: 4 games of history, DRESSES in 2024 w2/w3, no stat
#                 line in w2 (0 offensive snaps) -> the class T23 exists for
#   p_new         rookie RB, first stat line in 2024 w2: in the pool, no history
#   p_ir          on reserve (RES) in 2024 w2 -> not in the pool
#   p_ps          practice squad (DEV), never played -> not in the pool
#   p_ina         inactive (INA) in 2024 w2 -> not in the pool
#   p_bye         ACT for CCC, no game -> not in the pool
PLAYERS = [
    ("p_qb", "Ace QB", "QB"), ("p_wr", "Bo WR", "WR"), ("p_bench", "Backup QB", "QB"),
    ("p_new", "Rookie RB", "RB"), ("p_ir", "Hurt WR", "WR"), ("p_ina", "Scratch RB", "RB"),
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
    ("p_ir", 2023, 1, "BBB", 9.0, 40), ("p_ir", 2023, 2, "BBB", 9.0, 40),
    ("p_ir", 2023, 3, "BBB", 9.0, 40),
    ("p_new", 2024, 2, "BBB", 5.0, 30), ("p_new", 2024, 3, "BBB", 6.0, 30),
] + [
    (f"DST_{t}", s, w, t, pts, None)
    for t, pts in (("AAA", 8.0), ("BBB", 4.0))
    for s, w in ((2023, 1), (2023, 2), (2023, 3), (2024, 1), (2024, 2), (2024, 3))
]
GAMES = [(f"{s}_{w:02d}_BBB_AAA", s, w, "AAA", "BBB", 3.0, 46.0)
         for s in (2023, 2024) for w in (1, 2, 3)]
# (player, season, week, team, position, status)
ROSTERS = [
    (p, 2024, w, "AAA", pos, "ACT")
    for p, pos in (("p_qb", "QB"), ("p_wr", "WR"), ("p_bench", "QB"))
    for w in (1, 2, 3)
] + [
    ("p_new", 2024, 2, "BBB", "RB", "ACT"), ("p_new", 2024, 3, "BBB", "RB", "ACT"),
    ("p_ir", 2024, 2, "BBB", "WR", "RES"),
    ("p_ps", 2024, 2, "BBB", "WR", "DEV"),
    ("p_ina", 2024, 2, "AAA", "RB", "INA"),
    ("p_bye", 2024, 2, "CCC", "TE", "ACT"),
]


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
         "snaps": sn, "game_id": f"{s}_{w:02d}_BBB_AAA",
         "opponent": "BBB" if t == "AAA" else "AAA"}
        for p, s, w, t, pts, sn in STATS
    ])
    db.upsert(conn, "rosters", [
        {"player_id": p, "season": s, "week": w, "team": t, "position": pos,
         "status": st, "name": p}
        for p, s, w, t, pos, st in ROSTERS
    ])


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    populate(conn)
    yield conn
    conn.close()


W = (2024, 2)


class TestRosterSlate:
    def test_pool_is_dressed_players_on_teams_with_a_game_plus_both_dsts(self, conn):
        s = history.slate(conn, *W, pool="roster")
        assert set(s.player_id) == {"p_qb", "p_wr", "p_bench", "p_new", "DST_AAA", "DST_BBB"}

    def test_reserve_practice_squad_inactive_and_bye_are_out(self, conn):
        ids = set(history.slate(conn, *W, pool="roster").player_id)
        assert not ids & {"p_ir", "p_ps", "p_ina", "p_bye"}

    def test_pre_lock_columns_only_and_no_scores(self, conn):
        s = history.slate(conn, *W, pool="roster")
        assert list(s.columns) == history.SLATE_COLUMNS
        assert "dk_points" not in s.columns
        assert s.set_index("team").loc["AAA", "implied_total"].iloc[0] == pytest.approx(24.5)

    def test_position_and_opponent_come_from_the_roster_and_schedule(self, conn):
        s = history.slate(conn, *W, pool="roster").set_index("player_id")
        assert s.loc["p_new", "position"] == "RB"          # no players row needed
        assert s.loc["p_new", "opponent"] == "AAA"
        assert s.loc["DST_BBB", "position"] == "DST"

    def test_the_played_pool_is_unchanged_by_default(self, conn):
        s = history.slate(conn, *W)
        assert set(s.player_id) == {"p_qb", "p_wr", "p_new", "DST_AAA", "DST_BBB"}
        assert "p_bench" not in set(s.player_id)

    def test_an_unknown_pool_is_refused(self, conn):
        with pytest.raises(ValueError, match="unknown pool"):
            history.slate(conn, *W, pool="everyone")

    def test_a_week_with_no_rosters_is_empty_and_the_harness_says_so(self, conn):
        assert history.slate(conn, 2023, 2, pool="roster").empty
        with pytest.raises(harness.BacktestError, match="no roster rows"):
            harness.evaluate_week(conn, PriorAverage(), 2023, 2, pool="roster")


class TestRosterHarness:
    def test_a_dressed_player_with_no_stat_line_scores_zero(self, conn):
        rows = harness.evaluate_week(conn, PriorAverage(), *W, pool="roster").set_index("player_id")
        assert rows.loc["p_bench", "actual"] == 0.0
        # trailing mean of 4, 6, 5, 3 — the model promised 4.5 and got 0
        assert rows.loc["p_bench", "projection"] == pytest.approx(4.5)

    def test_the_played_pool_cannot_see_him(self, conn):
        rows = harness.evaluate_week(conn, PriorAverage(), *W, pool="played")
        assert "p_bench" not in set(rows.player_id)

    def test_evaluated_rows_carry_game_id_for_the_showdown_slice(self, conn):
        rows = harness.evaluate_week(conn, PriorAverage(), *W, pool="roster")
        assert set(rows.game_id) == {"2024_02_BBB_AAA"}

    def test_min_games_still_gates_who_is_scored(self, conn):
        rows = harness.evaluate_week(conn, PriorAverage(), *W, min_games=3, pool="roster")
        assert set(rows.player_id) == {"p_qb", "p_wr", "p_bench", "DST_AAA", "DST_BBB"}

    def test_coverage_reports_who_could_not_be_scored_and_what_they_carried(self, conn):
        res = harness.run(conn, PriorAverage(), seasons=[2024], weeks=[2], pool="roster",
                          persist=False)
        cov = res.metrics["coverage"]
        assert cov["pool_n"] == 6
        assert cov["evaluated_n"] == 5
        assert cov["excluded_n"] == 1                      # p_new: no history
        # p_new scored 5 of the pool's 25 + 18 + 0 + 5 + 8 + 4 = 60
        assert cov["points_unseen_share"] == pytest.approx(5 / 60)

    def test_coverage_reports_the_zero_scorers_and_what_they_were_promised(self, conn):
        res = harness.run(conn, PriorAverage(), seasons=[2024], weeks=[2], pool="roster",
                          persist=False)
        cov = res.metrics["coverage"]
        assert cov["zero_rate"] == pytest.approx(1 / 5)
        assert cov["zero_mean_projection"] == pytest.approx(4.5)

    def test_showdown_slice_is_the_projected_top_per_game(self, conn):
        res = harness.run(conn, PriorAverage(), seasons=[2024], weeks=[2], pool="roster",
                          persist=False)
        top = res.metrics["showdown_top12"]
        assert top["n"] == 5                                # one game, fewer than 12 scored
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
        assert res.metrics["coverage"]["pool_n"] == 5      # the stat rows
        assert res.metrics["coverage"]["zero_rate"] == 0.0

    def test_an_unknown_pool_is_refused_by_run(self, conn):
        with pytest.raises(harness.BacktestError, match="unknown pool"):
            harness.run(conn, PriorAverage(), seasons=[2024], pool="everyone", persist=False)

    def test_the_cli_accepts_the_pool(self, tmp_path, capsys):
        path = tmp_path / "t.sqlite"
        c = db.connect(path); populate(c); c.commit(); c.close()
        rc = backtest_main(["--model", "prior_average", "--seasons", "2024", "2024",
                            "--pool", "roster", "--db", str(path), "--no-persist"])
        assert rc == 0
        assert "pool=roster" in capsys.readouterr().out


class TestRosterLeakage:
    """Spec §7's spot-check, in roster mode: delete the future, output unchanged."""

    def test_deleting_future_scores_and_rosters_does_not_change_the_projection(self, conn):
        before = harness.evaluate_week(conn, PriorAverage(), *W, pool="roster")

        conn.execute("UPDATE player_week_stats SET dk_points = dk_points * 100 "
                     "WHERE season > ? OR (season = ? AND week >= ?)", (W[0], W[0], W[1]))
        conn.execute("DELETE FROM player_week_stats WHERE season = ? AND week > ?", W)
        conn.execute("DELETE FROM rosters WHERE season = ? AND week > ?", W)
        after = harness.evaluate_week(conn, PriorAverage(), *W, pool="roster")

        pd.testing.assert_frame_equal(
            before[["player_id", "projection"]].sort_values("player_id").reset_index(drop=True),
            after[["player_id", "projection"]].sort_values("player_id").reset_index(drop=True),
        )

    def test_the_harness_never_hands_a_model_the_target_scores(self, conn):
        seen = {}

        class Spy:
            name = "spy"

            def project(self, hist, slate):
                seen["hist_max"] = max(zip(hist.season, hist.week))
                seen["slate_cols"] = set(slate.columns)
                seen["slate_ids"] = set(slate.player_id)
                return pd.DataFrame({"player_id": slate.player_id, "projection": 1.0})

        harness.evaluate_week(conn, Spy(), *W, pool="roster")
        assert seen["hist_max"] == (2024, 1)
        assert "dk_points" not in seen["slate_cols"]
        assert "p_bench" in seen["slate_ids"]                # the model is asked about him
