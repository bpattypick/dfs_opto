"""The real score simulator (T13): fitted variance, skewed marginals, block correlation."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import scoremodel
from src.contest import ContestError, PayoutTable, simulate_contest
from src.showdown import Lineup


def slate():
    return pd.DataFrame([
        ("qb_a", "QB", "AAA", 20.0), ("wr1_a", "WR", "AAA", 16.0),
        ("wr2_a", "WR", "AAA", 9.0),  ("te_a", "TE", "AAA", 7.0),
        ("rb_a", "RB", "AAA", 12.0),  ("dst_a", "DST", "AAA", 6.0),
        ("qb_b", "QB", "BBB", 18.0),  ("wr1_b", "WR", "BBB", 14.0),
        ("rb_b", "RB", "BBB", 10.0),  ("dst_b", "DST", "BBB", 5.0),
    ], columns=["player_id", "position", "team", "projection"])


class TestVariance:
    def test_sd_grows_with_projection_at_the_fitted_slope(self):
        assert scoremodel.sd_for(10.0, "WR") == pytest.approx(3.57 + 3.64)
        assert scoremodel.sd_for(20.0, "WR") > scoremodel.sd_for(5.0, "WR")

    def test_qb_spread_is_nearly_flat(self):
        # Measured: a QB's spread barely depends on his level.
        assert scoremodel.sd_for(25.0, "QB") - scoremodel.sd_for(10.0, "QB") < 1.0

    def test_unknown_position_gets_the_overall_fit(self):
        assert scoremodel.sd_for(10.0, "??") == pytest.approx(3.81 + 3.10)


class TestLognormal:
    def test_parameters_reproduce_mean_and_sd(self):
        mu, sigma = scoremodel.lognormal_params(12.0, 7.0)
        x = np.exp(mu + sigma * np.random.default_rng(0).standard_normal(400_000))
        assert x.mean() == pytest.approx(12.0, rel=0.02)
        assert x.std() == pytest.approx(7.0, rel=0.03)

    def test_is_right_skewed_and_non_negative(self):
        mu, sigma = scoremodel.lognormal_params(8.0, 6.0)
        x = np.exp(mu + sigma * np.random.default_rng(1).standard_normal(200_000))
        assert (x >= 0).all()
        assert np.median(x) < x.mean()         # right skew

    @pytest.mark.parametrize("mean,sd", [(0.0, 1.0), (5.0, 0.0), (-1.0, 2.0)])
    def test_degenerate_inputs_are_refused(self, mean, sd):
        with pytest.raises(ContestError):
            scoremodel.lognormal_params(mean, sd)


class TestCorrelationMatrix:
    def test_qb_and_his_top_target_get_the_stack_correlation(self):
        R = scoremodel.build_correlation(slate())
        ids = slate()["player_id"].tolist()
        i, j = ids.index("qb_a"), ids.index("wr1_a")
        assert R[i, j] == pytest.approx(0.327)

    def test_qb_and_a_secondary_receiver_get_the_plain_wr_value(self):
        R = scoremodel.build_correlation(slate())
        ids = slate()["player_id"].tolist()
        assert R[ids.index("qb_a"), ids.index("wr2_a")] == pytest.approx(0.213)
        assert R[ids.index("qb_a"), ids.index("te_a")] == pytest.approx(0.170)

    def test_opposing_quarterbacks_are_positively_correlated(self):
        R = scoremodel.build_correlation(slate())
        ids = slate()["player_id"].tolist()
        assert R[ids.index("qb_a"), ids.index("qb_b")] == pytest.approx(0.185)

    def test_a_dst_is_anti_correlated_with_the_qb_it_faces(self):
        R = scoremodel.build_correlation(slate())
        ids = slate()["player_id"].tolist()
        assert R[ids.index("dst_a"), ids.index("qb_b")] == pytest.approx(-0.251)
        assert R[ids.index("dst_b"), ids.index("qb_a")] == pytest.approx(-0.251)

    def test_unmeasured_pairs_are_independent(self):
        R = scoremodel.build_correlation(slate())
        ids = slate()["player_id"].tolist()
        assert R[ids.index("wr1_a"), ids.index("wr2_a")] == 0.0     # WR-WR teammates
        assert R[ids.index("rb_a"), ids.index("qb_a")] == 0.0       # QB-RB below threshold

    def test_matrix_is_symmetric_unit_diagonal_and_psd(self):
        R = scoremodel.build_correlation(slate())
        assert np.allclose(R, R.T)
        assert np.allclose(np.diag(R), 1.0)
        assert np.linalg.eigvalsh(R).min() > -1e-9

    def test_duplicate_ids_are_refused(self):
        s = pd.concat([slate(), slate().head(1)], ignore_index=True)
        with pytest.raises(ContestError, match="duplicate"):
            scoremodel.build_correlation(s)


class TestCorrelatedScores:
    def test_shape_and_player_order(self):
        m = scoremodel.CorrelatedScores(slate())
        x = m(np.random.default_rng(0), 500)
        assert x.shape == (500, 10)
        assert m.players == slate()["player_id"].tolist()

    def test_means_match_projections_and_sds_match_the_fit(self):
        m = scoremodel.CorrelatedScores(slate())
        x = m(np.random.default_rng(0), 300_000)
        s = slate()
        for k, (pid, pos, proj) in enumerate(zip(s.player_id, s.position, s.projection)):
            assert x[:, k].mean() == pytest.approx(proj, rel=0.03), pid
            assert x[:, k].std() == pytest.approx(scoremodel.sd_for(proj, pos), rel=0.05), pid

    def test_the_stack_is_actually_correlated_in_the_draws(self):
        # The thing the placeholder could not do and the SF@LAR lineup bet on.
        m = scoremodel.CorrelatedScores(slate())
        x = m(np.random.default_rng(2), 200_000)
        ids = m.players
        r = np.corrcoef(x[:, ids.index("qb_a")], x[:, ids.index("wr1_a")])[0, 1]
        # Pearson on the lognormals sits a little under the copula's 0.327.
        assert 0.25 < r < 0.36
        r_ind = np.corrcoef(x[:, ids.index("wr1_a")], x[:, ids.index("wr2_a")])[0, 1]
        assert abs(r_ind) < 0.02

    def test_scores_are_never_negative(self):
        x = scoremodel.CorrelatedScores(slate())(np.random.default_rng(3), 50_000)
        assert (x >= 0).all()

    def test_a_zero_projection_lands_near_zero_rather_than_failing(self):
        s = slate(); s.loc[0, "projection"] = 0.0
        x = scoremodel.CorrelatedScores(s)(np.random.default_rng(0), 20_000)
        assert x[:, 0].mean() < 0.5

    def test_negative_projection_is_refused(self):
        s = slate(); s.loc[0, "projection"] = -1.0
        with pytest.raises(ContestError, match="non-negative"):
            scoremodel.CorrelatedScores(s)

    def test_plugs_into_the_contest_simulator(self):
        m = scoremodel.CorrelatedScores(slate())
        mine = Lineup("qb_a", ("wr1_a", "rb_a", "te_a", "qb_b", "dst_b"))
        field = [Lineup("qb_b", ("wr1_b", "rb_b", "wr1_a", "rb_a", "dst_a"))] * 20
        res = simulate_contest(mine, field, m.players, m,
                               PayoutTable.from_tiers([(1, 1, 50.0), (2, 5, 10.0)]),
                               entry_fee=5.0, trials=400, seed=0)
        assert 0.0 <= res.cash_rate <= 1.0 and res.entries == 21
