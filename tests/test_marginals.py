"""T24: the empirical score marginal -- zeros, a real left tail, mean preserved."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src import captain
from src.contest import ContestError
from src.scoremodel import (CorrelatedScores, EmpiricalMarginals, coverage_table,
                            load_marginals, lognormal_quantile, normal_cdf)


def residual_rows(seed=0, n=6000):
    """Synthetic (position, projection, actual): 10% zeros, right-skewed otherwise."""
    rng = np.random.default_rng(seed)
    rows = []
    for pos, zero_share in (("QB", 0.05), ("WR", 0.12), ("RB", 0.10), ("TE", 0.15)):
        proj = rng.uniform(2, 25, n // 4)
        ratio = np.where(rng.uniform(size=proj.size) < zero_share, 0.0,
                         rng.lognormal(-0.15, 0.55, proj.size))
        rows.append(pd.DataFrame({"position": pos, "projection": proj, "actual": proj * ratio,
                                  "season": 2024, "week": 1, "game_id": "g"}))
    return pd.concat(rows, ignore_index=True)


@pytest.fixture
def marginals():
    return EmpiricalMarginals.fit(residual_rows(), bands=3, min_rows=300, fitted_on="synthetic")


class TestNormalCdf:
    def test_matches_known_values(self):
        assert normal_cdf(0.0) == pytest.approx(0.5)
        assert normal_cdf(1.2816) == pytest.approx(0.90, abs=1e-4)
        assert normal_cdf(-1.6449) == pytest.approx(0.05, abs=1e-4)
        assert list(normal_cdf(np.array([-3, 3]))) == pytest.approx([0.00135, 0.99865], abs=1e-4)


class TestFit:
    def test_curves_are_monotone_and_carry_the_empirical_mean(self, marginals):
        # Synthetic ratios have mean ~0.89 (10% zeros x lognormal(-0.15, 0.55)); the
        # curve keeps that -- calibration first -- rather than forcing mean 1.
        rows = residual_rows()
        raw_mean = (rows.actual / rows.projection).mean()          # ~0.90
        for pos in ["ALL"] + marginals.positions():
            for curve in marginals.curves[pos]:
                assert np.all(np.diff(curve) >= -1e-12)
        for c in marginals.centers["ALL"]:
            assert marginals.mean_ratio("ALL", c) == pytest.approx(raw_mean, abs=0.05)
            assert marginals.mean_ratio("ALL", c) < 0.97

    def test_normalise_mean_is_an_explicit_option(self):
        m = EmpiricalMarginals.fit(residual_rows(), bands=3, min_rows=300, normalise_mean=True)
        for curve in m.curves["ALL"]:
            assert curve.mean() == pytest.approx(1.0, abs=0.02)

    def test_zero_mass_is_kept(self, marginals):
        assert marginals.zero_share["TE"].mean() == pytest.approx(0.15, abs=0.03)
        assert marginals.quantile("TE", 10.0, 0.05) == 0.0                 # inside the zero mass
        assert marginals.quantile("TE", 10.0, 0.50) > 0.0

    def test_mean_is_the_projection_times_the_bands_mean_ratio(self, marginals):
        scores = marginals.quantile("WR", 12.0, marginals.grid)
        assert scores.mean() == pytest.approx(12.0 * marginals.mean_ratio("WR", 12.0), rel=0.02)
        assert 0.80 < marginals.mean_ratio("WR", 12.0) < 0.98

    def test_a_position_with_too_few_rows_uses_the_pooled_curve(self):
        rows = residual_rows()
        rows.loc[rows.position == "TE", "position"] = "K"     # K gets 1500 rows; set min_rows high
        m = EmpiricalMarginals.fit(rows, bands=2, min_rows=2000)
        assert "K" not in m.positions()
        assert np.allclose(m.curve("K", 8.0), m.curve("ALL", 8.0))
        assert np.allclose(m.curve("DST", 8.0), m.curve("ALL", 8.0))

    def test_curve_interpolates_between_band_centres(self, marginals):
        c = marginals.centers["WR"]
        mid = (c[0] + c[1]) / 2
        expected = 0.5 * (marginals.curves["WR"][0] + marginals.curves["WR"][1])
        assert np.allclose(marginals.curve("WR", mid), expected)
        assert np.allclose(marginals.curve("WR", c[0] - 5), marginals.curves["WR"][0])
        assert np.allclose(marginals.curve("WR", c[-1] + 5), marginals.curves["WR"][-1])

    def test_refuses_too_little_data_and_missing_columns(self):
        with pytest.raises(ContestError, match="need"):
            EmpiricalMarginals.fit(residual_rows().head(50))
        with pytest.raises(ContestError, match="missing"):
            EmpiricalMarginals.fit(residual_rows().drop(columns="actual"))

    def test_round_trips_through_json(self, marginals, tmp_path):
        path = marginals.save(tmp_path / "m.json")
        back = EmpiricalMarginals.load(path)
        assert back.fitted_on == "synthetic"
        assert np.allclose(back.curve("QB", 15.0), marginals.curve("QB", 15.0))
        assert load_marginals(tmp_path / "missing.json") is None
        assert load_marginals(path).positions() == marginals.positions()


class TestCoverage:
    def test_empirical_is_calibrated_in_sample_and_lognormal_is_not_on_zeros(self, marginals):
        rows = residual_rows()
        emp = coverage_table(rows, marginals)
        for q in (0.10, 0.25, 0.50, 0.90):
            assert emp.loc["ALL", f"<=p{int(q * 100)}"] == pytest.approx(q, abs=0.03)
        logn = coverage_table(rows, None)
        assert logn.loc["TE", "<=p10"] > 0.14         # the zeros alone break the lognormal floor

    def test_lognormal_quantile_is_the_closed_form(self):
        q = lognormal_quantile("QB", np.array([20.0]), 0.50)
        assert 0 < q[0] < 20.0                          # median below the mean: right-skewed


class TestCorrelatedDraws:
    def slate(self):
        return pd.DataFrame({
            "player_id": ["qb", "wr", "te", "oqb"], "position": ["QB", "WR", "TE", "QB"],
            "team": ["A", "A", "A", "B"], "projection": [20.0, 14.0, 8.0, 18.0],
        })

    def test_empirical_draws_keep_the_mean_zero_mass_and_correlation(self, marginals):
        model = CorrelatedScores(self.slate(), marginals=marginals)
        assert model.kind == "empirical"
        draws = model(np.random.default_rng(0), 20000)
        assert draws[:, 0].mean() == pytest.approx(20.0 * marginals.mean_ratio("QB", 20.0), rel=0.03)
        assert (draws[:, 2] == 0).mean() == pytest.approx(0.15, abs=0.03)     # TE zero share
        assert np.corrcoef(draws[:, 0], draws[:, 1])[0, 1] > 0.15             # QB-top target
        assert (draws >= 0).all()

    def test_lognormal_path_is_unchanged_without_marginals(self):
        model = CorrelatedScores(self.slate())
        assert model.kind == "lognormal"
        draws = model(np.random.default_rng(0), 5000)
        assert (draws > 0).all()

    def test_a_zero_projection_draws_zero(self, marginals):
        s = self.slate(); s.loc[2, "projection"] = 0.0
        draws = CorrelatedScores(s, marginals=marginals)(np.random.default_rng(1), 500)
        assert (draws[:, 2] == 0).all()


class TestCaptainUsesTheMarginal:
    def pool(self):
        return pd.DataFrame([
            ("a", "A", "KC", "TE", 6000, 10.0), ("b", "B", "KC", "QB", 9000, 20.0),
            ("c", "C", "DEN", "WR", 7000, 12.0), ("d", "D", "DEN", "RB", 5000, 9.0),
            ("e", "E", "DEN", "QB", 8000, 17.0), ("f", "F", "KC", "WR", 4000, 6.0),
            ("g", "G", "DEN", "TE", 3000, 4.0),
        ], columns=["player_id", "name", "team", "position", "salary", "projection"])

    def test_floor_is_lower_with_the_fitted_marginal(self, marginals):
        logn = captain.player_quantiles(self.pool()).set_index("player_id")
        emp = captain.player_quantiles(self.pool(), marginals=marginals).set_index("player_id")
        assert emp.loc["a", "p10"] == 0.0                     # TE zero share 15% > 10%
        assert emp.loc["a", "p10"] < logn.loc["a", "p10"]
        assert emp.loc["b", "p50"] > 0 and emp.loc["b", "sd"] > 0

    def test_board_accepts_the_marginal(self, marginals):
        pool = self.pool()
        scores = CorrelatedScores(pool[["player_id", "position", "team", "projection"]], marginals=marginals)
        board = captain.captain_board(pool, scores.players, scores, objective="cash", n_captains=3,
                                      trials=500, seed=0, marginals=marginals)
        assert len(board) >= 1 and (board["p10"] <= board["p90"]).all()
