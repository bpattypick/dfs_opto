"""Backtest harness, time-boxed history, and the leakage test (spec §7).

The leakage test is the one CLAUDE.md's definition of done has required since
the first commit and which did not exist until now. It implements the spec's
spot-check literally: delete week W and everything after, confirm week W's
projections are unchanged. A model that can only see what the harness hands it
passes by construction; the test proves the harness hands it nothing else.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import db
from src.backtest import harness, history
from src.projection import (CalibratedAverage, PriorAverage, ProjectionError,
                            ShrunkVegas)

# Two seasons, three weeks each, four players. Points chosen so trailing means
# are hand-computable. Player p_new has history only from 2024 week 2.
PLAYERS = [
    ("p_qb", "Ace QB", "QB"), ("p_wr", "Bo WR", "WR"),
    ("p_rb", "Cy RB", "RB"), ("p_new", "Dee WR", "WR"),
]
# (player, season, week, team, dk_points, snaps)
STATS = [
    ("p_qb", 2023, 1, "AAA", 20.0, 60), ("p_qb", 2023, 2, "AAA", 30.0, 60),
    ("p_qb", 2023, 3, "AAA", 10.0, 60), ("p_qb", 2024, 1, "AAA", 40.0, 60),
    ("p_qb", 2024, 2, "AAA", 25.0, 60), ("p_qb", 2024, 3, "AAA", 99.0, 60),
    ("p_wr", 2023, 1, "AAA", 10.0, 50), ("p_wr", 2023, 2, "AAA", 14.0, 50),
    ("p_wr", 2023, 3, "AAA", 12.0, 50), ("p_wr", 2024, 1, "AAA", 16.0, 50),
    ("p_wr", 2024, 2, "AAA", 18.0, 50), ("p_wr", 2024, 3, "AAA", 50.0, 50),
    ("p_rb", 2023, 1, "BBB", 8.0, 40),  ("p_rb", 2023, 2, "BBB", 12.0, 40),
    ("p_rb", 2023, 3, "BBB", 10.0, 40), ("p_rb", 2024, 1, "BBB", 6.0, 40),
    ("p_rb", 2024, 2, "BBB", 9.0, 0),   ("p_rb", 2024, 3, "BBB", 7.0, 40),
    ("p_new", 2024, 2, "BBB", 5.0, 30), ("p_new", 2024, 3, "BBB", 6.0, 30),
]
# One game per week; AAA at home, favoured by 3, total 46 -> AAA 24.5, BBB 21.5
GAMES = [(f"{s}_{w:02d}_BBB_AAA", s, w, "AAA", "BBB", 3.0, 46.0)
         for s in (2023, 2024) for w in (1, 2, 3)]


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
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
    yield conn
    conn.close()


class TestTimeBox:
    def test_as_of_is_strictly_before_the_target_week(self, conn):
        past = history.as_of(conn, 2024, 2)
        assert ((past.season < 2024) | ((past.season == 2024) & (past.week < 2))).all()
        assert not ((past.season == 2024) & (past.week >= 2)).any()

    def test_week_one_sees_all_of_the_prior_season_and_none_of_this_one(self, conn):
        # Tuple ordering: (2023, 3) < (2024, 1).
        past = history.as_of(conn, 2024, 1)
        assert set(past.season) == {2023}
        assert len(past) == 9

    def test_slate_never_carries_the_answer(self, conn):
        s = history.slate(conn, 2024, 2)
        assert "dk_points" not in s.columns
        assert set(s.columns) >= {"player_id", "position", "team", "implied_total"}

    def test_implied_totals_use_the_verified_sign_convention(self, conn):
        t = history.implied_totals(conn, 2024, 1).set_index("team")["implied_total"]
        assert t["AAA"] == pytest.approx(24.5)   # home, favoured by 3
        assert t["BBB"] == pytest.approx(21.5)

    def test_actuals_are_a_separate_call(self, conn):
        a = history.actuals(conn, 2024, 3).set_index("player_id")
        assert a.loc["p_qb", "dk_points"] == 99.0


class TestLeakage:
    """Spec §7: 'delete week W data, confirm week W lineup output unchanged'."""

    @pytest.mark.parametrize("model", [PriorAverage(window=17), ShrunkVegas(k=2.0),
                                       CalibratedAverage(min_fit_rows=5)])
    def test_deleting_the_future_does_not_change_the_projection(self, conn, model):
        W = (2024, 2)
        before = model.project(history.as_of(conn, *W), history.slate(conn, *W))

        # Week W's own scores, and everything after, are wildly different now.
        conn.execute("UPDATE player_week_stats SET dk_points = dk_points * 100 "
                     "WHERE season > ? OR (season = ? AND week >= ?)", (W[0], W[0], W[1]))
        conn.execute("DELETE FROM player_week_stats WHERE season = ? AND week > ?", W)
        after = model.project(history.as_of(conn, *W), history.slate(conn, *W))

        pd.testing.assert_frame_equal(
            before.sort_values("player_id").reset_index(drop=True),
            after.sort_values("player_id").reset_index(drop=True),
        )

    def test_the_harness_never_hands_a_model_the_target_scores(self, conn):
        seen = {}

        class Spy:
            name = "spy"
            def project(self, hist, slate):
                # max (season, week) as a tuple — not max season and max week
                # independently, which would report 2023's week 3.
                seen["hist_max"] = max(zip(hist.season, hist.week))
                seen["slate_cols"] = set(slate.columns)
                return pd.DataFrame({"player_id": slate.player_id, "projection": 1.0})

        harness.evaluate_week(conn, Spy(), 2024, 2)
        assert seen["hist_max"] == (2024, 1)
        assert "dk_points" not in seen["slate_cols"]

    def test_a_model_is_refused_the_answer_key_even_if_handed_it(self, conn):
        past = history.as_of(conn, 2024, 2)
        leaky = history.slate(conn, 2024, 2).assign(dk_points=1.0)
        with pytest.raises(ProjectionError, match="answer key"):
            ShrunkVegas().project(past, leaky)


class TestPriorAverage:
    def test_trailing_mean_over_the_window(self, conn):
        # p_qb before 2024 w2: 20, 30, 10, 40 -> window 2 = mean(10, 40) = 25
        out = PriorAverage(window=2).project(
            history.as_of(conn, 2024, 2), history.slate(conn, 2024, 2)
        ).set_index("player_id")
        assert out.loc["p_qb", "projection"] == pytest.approx(25.0)

    def test_no_history_means_no_projection(self, conn):
        # p_new first appears in 2024 w2; projecting w2 it has nothing.
        out = PriorAverage().project(
            history.as_of(conn, 2024, 2), history.slate(conn, 2024, 2)
        ).set_index("player_id")
        assert pd.isna(out.loc["p_new", "projection"])


class TestShrunkVegas:
    def hist_slate(self, conn, W=(2024, 2)):
        return history.as_of(conn, *W), history.slate(conn, *W)

    def test_k_zero_and_no_vegas_reduces_to_the_prior_average(self, conn):
        h, s = self.hist_slate(conn)
        a = PriorAverage(window=17).project(h, s).set_index("player_id")
        b = ShrunkVegas(window=17, k=0.0, vegas=False).project(h, s).set_index("player_id")
        for p in ("p_qb", "p_wr", "p_rb"):
            assert b.loc[p, "projection"] == pytest.approx(a.loc[p, "projection"])

    def test_shrinkage_pulls_toward_the_positional_mean(self, conn):
        # Week 3, so the WR pool holds p_wr (5 games) and p_new (1 game) and the
        # positional mean differs from p_wr's own average.
        h, s = self.hist_slate(conn, W=(2024, 3))
        strong = ShrunkVegas(k=0.0, vegas=False).project(h, s).set_index("player_id")
        shrunk = ShrunkVegas(k=100.0, vegas=False).project(h, s).set_index("player_id")
        wr_mean = h[h.position == "WR"]["dk_points"].mean()
        assert strong.loc["p_wr", "projection"] != pytest.approx(wr_mean)
        assert abs(shrunk.loc["p_wr", "projection"] - wr_mean) < \
               abs(strong.loc["p_wr", "projection"] - wr_mean)

    def test_a_rookie_gets_the_positional_prior(self, conn):
        h, s = self.hist_slate(conn)
        out = ShrunkVegas(vegas=False).project(h, s).set_index("player_id")
        wr_mean = h[h.position == "WR"]["dk_points"].mean()
        assert out.loc["p_new", "projection"] == pytest.approx(wr_mean)

    def test_vegas_scales_by_implied_total(self, conn):
        h, s = self.hist_slate(conn)
        flat = ShrunkVegas(vegas=False).project(h, s).set_index("player_id")
        scaled = ShrunkVegas(vegas=True).project(h, s).set_index("player_id")
        # AAA implied 24.5 vs mean 23.0 -> scaled up; BBB 21.5 -> scaled down.
        assert scaled.loc["p_qb", "projection"] > flat.loc["p_qb", "projection"]
        assert scaled.loc["p_rb", "projection"] < flat.loc["p_rb", "projection"]

    def test_negative_k_is_refused(self, conn):
        h, s = self.hist_slate(conn)
        with pytest.raises(ProjectionError, match="non-negative"):
            ShrunkVegas(k=-1).project(h, s)


class TestHarness:
    def test_evaluates_only_players_with_enough_history(self, conn):
        rows = harness.evaluate_week(conn, PriorAverage(), 2024, 2, min_games=3)
        assert set(rows.player_id) == {"p_qb", "p_wr"}     # p_rb has 0 snaps, p_new is new

    def test_zero_snaps_is_treated_as_inactive(self, conn):
        # Spec §7's stated approximation for pre-lock inactives.
        rows = harness.evaluate_week(conn, PriorAverage(), 2024, 2, min_games=1)
        assert "p_rb" not in set(rows.player_id)

    def test_a_perfect_model_scores_perfectly(self, conn):
        class Oracle:
            name = "oracle"
            def project(self, hist, slate):
                truth = history.actuals(conn, 2024, 3).set_index("player_id")["dk_points"]
                return pd.DataFrame({"player_id": slate.player_id,
                                     "projection": slate.player_id.map(truth)})
        res = harness.run(conn, Oracle(), seasons=[2024], weeks=[3], persist=False)
        assert res.metrics["mae"] == pytest.approx(0.0)
        assert res.metrics["spearman"] == pytest.approx(1.0)
        assert res.metrics["bias"] == pytest.approx(0.0)

    def test_bias_sign_means_projections_run_high(self, conn):
        class High:
            name = "high"
            def project(self, hist, slate):
                return pd.DataFrame({"player_id": slate.player_id, "projection": 1000.0})
        res = harness.run(conn, High(), seasons=[2024], weeks=[3], persist=False)
        assert res.metrics["bias"] > 0

    def test_run_persists_to_backtest_runs(self, conn):
        harness.run(conn, PriorAverage(), seasons=[2024], persist=True)
        assert conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 1

    def test_skips_the_first_week_with_no_history(self, conn):
        # 2023 w1 has nothing before it; the run should not fail, just skip it.
        res = harness.run(conn, PriorAverage(), seasons=[2023, 2024], persist=False)
        assert (res.rows.season.min(), res.rows.week.min()) > (2023, 1)

    def test_a_model_returning_wrong_columns_is_refused(self, conn):
        class Bad:
            name = "bad"
            def project(self, hist, slate):
                return pd.DataFrame({"player_id": slate.player_id, "pts": 1.0})
        with pytest.raises(harness.BacktestError, match="returned columns"):
            harness.evaluate_week(conn, Bad(), 2024, 2)

    def test_empty_database_fails_loudly(self):
        empty = db.connect(":memory:"); db.create_schema(empty)
        with pytest.raises(harness.BacktestError, match="no weeks"):
            harness.run(empty, PriorAverage(), seasons=[2024], persist=False)


class TestCalibratedAverage:
    """v2: level-dependent calibration, fit on history only."""

    def regressing_history(self, beta=0.8, alpha=2.0, players=40, weeks=12, seed=0):
        """Scores where actual = alpha + beta*prior + noise, so beta is recoverable."""
        rng = np.random.default_rng(seed)
        rows, positions = [], ["QB", "RB", "WR", "TE"]
        for i in range(players):
            level = rng.uniform(2, 25)
            hist = []
            for w in range(1, weeks + 1):
                prior = np.mean(hist[-17:]) if hist else level
                pts = alpha + beta * prior + rng.normal(0, 1.5)
                hist.append(pts)
                rows.append({"player_id": f"p{i}", "position": positions[i % 4],
                             "season": 2024, "week": w, "team": "T", "opponent": "U",
                             "game_id": f"g{w}", "snaps": 50, "snap_pct": 0.8,
                             "targets": 5, "carries": 5, "receptions": 3,
                             "pass_attempts": 0, "dk_points": pts})
        hist = pd.DataFrame(rows)
        slate = hist[hist.week == weeks][["player_id", "position", "team"]].copy()
        return hist, slate

    def test_recovers_the_regression_slope_from_history(self):
        hist, slate = self.regressing_history(beta=0.8, alpha=2.0)
        m = CalibratedAverage(min_fit_rows=50)
        m.project(hist, slate)
        alpha, beta = m.last_fit["ALL"]
        assert beta == pytest.approx(0.8, abs=0.08)
        assert alpha == pytest.approx(2.0, abs=1.0)

    def test_preserves_rank_order_exactly(self, conn):
        # A linear map cannot reorder, so Spearman vs the baseline is 1.
        h, s = history.as_of(conn, 2024, 3), history.slate(conn, 2024, 3)
        base = PriorAverage().project(h, s).set_index("player_id")["projection"]
        cal = CalibratedAverage(min_fit_rows=5).project(h, s).set_index("player_id")["projection"]
        both = pd.concat([base, cal], axis=1, keys=["b", "c"]).dropna()
        assert both["b"].rank().corr(both["c"].rank()) == pytest.approx(1.0)

    def test_pulls_the_tails_toward_the_middle(self):
        hist, slate = self.regressing_history(beta=0.8)
        base = PriorAverage().project(hist, slate).set_index("player_id")["projection"]
        cal = CalibratedAverage(min_fit_rows=50).project(hist, slate).set_index("player_id")["projection"]
        top, bottom = base.idxmax(), base.idxmin()
        assert cal[top] < base[top]
        assert cal[bottom] > base[bottom]

    def test_a_players_own_score_never_enters_its_own_prior(self):
        # With shift(1), the first game of every player has no prior and is
        # excluded from the fit; the fit therefore sees weeks-1 rows fewer than
        # the history holds.
        hist, slate = self.regressing_history(players=10, weeks=6)
        m = CalibratedAverage(min_fit_rows=5)
        m.project(hist, slate)
        assert m.last_fit is not None

    def test_refuses_to_calibrate_on_too_little_history(self, conn):
        h, s = history.as_of(conn, 2024, 2), history.slate(conn, 2024, 2)
        with pytest.raises(ProjectionError, match="calibrate on"):
            CalibratedAverage(min_fit_rows=1000).project(h, s)

    def test_per_position_fits_separate_slopes(self):
        # QBs regress at 0.6, everyone else at 0.9; the pooled slope is neither.
        rng = np.random.default_rng(5)
        rows = []
        for i in range(60):
            pos = "QB" if i % 2 == 0 else "WR"
            beta = 0.6 if pos == "QB" else 0.9
            level, hist = rng.uniform(5, 25), []
            for w in range(1, 15):
                prior = np.mean(hist[-17:]) if hist else level
                pts = 2.0 + beta * prior + rng.normal(0, 1.0)
                hist.append(pts)
                rows.append({"player_id": f"p{i}", "position": pos, "season": 2024,
                             "week": w, "team": "T", "opponent": "U", "game_id": f"g{w}",
                             "snaps": 50, "snap_pct": 0.8, "targets": 5, "carries": 5,
                             "receptions": 3, "pass_attempts": 0, "dk_points": pts})
        hist = pd.DataFrame(rows)
        slate = hist[hist.week == 14][["player_id", "position", "team"]]
        m = CalibratedAverage(per_position=True, min_fit_rows=50)
        m.project(hist, slate)
        assert m.last_fit["QB"][1] == pytest.approx(0.6, abs=0.08)
        assert m.last_fit["WR"][1] == pytest.approx(0.9, abs=0.08)
        # The pooled line is not between them: the groups settle at different
        # levels and a line through two clusters runs steeper than either slope.
        assert m.last_fit["ALL"] != m.last_fit["QB"]
