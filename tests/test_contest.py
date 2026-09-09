"""Placement and ROI tests (roadmap Step 3b, task T6).

Every simulation test uses a deterministic score model, so the correct answer is
hand-computable rather than a tolerance around a random draw.
"""

from __future__ import annotations

import numpy as np
import pytest

from src import contest
from src.contest import ContestError, PayoutTable, simulate_contest
from src.showdown import Lineup

PLAYERS = [f"p{i}" for i in range(8)]
CANDIDATE = Lineup("p0", ("p1", "p2", "p3", "p4", "p5"))
WORSE = Lineup("p6", ("p1", "p2", "p3", "p4", "p5"))
WORST = Lineup("p7", ("p1", "p2", "p3", "p4", "p5"))


def fixed_scores(**overrides):
    """A score model that returns the same scores every trial."""
    base = np.zeros(len(PLAYERS))
    base[0] = 30.0   # p0 -> candidate's captain, the best score
    base[6] = 20.0   # p6 -> WORSE
    base[7] = 10.0   # p7 -> WORST
    for name, value in overrides.items():
        base[PLAYERS.index(name)] = value

    def draw(rng, trials):
        return np.tile(base, (trials, 1))
    return draw


# 1st $100, 2nd $60, 3rd $20. Nothing below.
PAYOUTS = PayoutTable.from_tiers([(1, 1, 100.0), (2, 2, 60.0), (3, 3, 20.0)])


class TestPayoutTable:
    def test_prize_by_rank(self):
        table = PayoutTable.from_tiers([(1, 1, 500.0), (2, 3, 200.0), (4, 10, 50.0)])
        assert [table.prize(r) for r in (1, 2, 3, 4, 10, 11)] == [
            500.0, 200.0, 200.0, 50.0, 50.0, 0.0
        ]

    def test_paid_places_and_total(self):
        table = PayoutTable.from_tiers([(1, 1, 500.0), (2, 3, 200.0), (4, 10, 50.0)])
        assert table.paid_places == 10
        assert table.total_prizes == 500 + 2 * 200 + 7 * 50

    def test_overlapping_tiers_are_refused(self):
        with pytest.raises(ContestError, match="overlaps"):
            PayoutTable.from_tiers([(1, 5, 10.0), (3, 8, 5.0)])

    @pytest.mark.parametrize("bad", [[(0, 1, 10.0)], [(5, 2, 10.0)], [(1, 1, -5.0)]])
    def test_malformed_tiers_are_refused(self, bad):
        with pytest.raises(ContestError):
            PayoutTable.from_tiers(bad)

    def test_empty_table_is_refused(self):
        with pytest.raises(ContestError, match="no tiers"):
            PayoutTable.from_tiers([])

    def test_effective_rake(self):
        # $1000 collected, $850 paid out -> 15% rake (CONTEST_RULES R4).
        table = PayoutTable.from_tiers([(1, 1, 850.0)])
        assert table.effective_rake(entry_fee=10.0, entries=100) == pytest.approx(0.15)

    def test_overlay_reads_as_negative_rake(self):
        # Guaranteed pool that didn't fill: the house adds money.
        table = PayoutTable.from_tiers([(1, 1, 1200.0)])
        assert table.effective_rake(entry_fee=10.0, entries=100) == pytest.approx(-0.2)


class TestPlacement:
    def test_outright_win_pays_first_prize(self):
        result = simulate_contest(
            CANDIDATE, [WORSE, WORST], PLAYERS, fixed_scores(), PAYOUTS,
            entry_fee=10.0, trials=5, seed=0,
        )
        assert result.entries == 3
        assert result.win_rate == 1.0
        assert result.mean_payout == pytest.approx(100.0)
        assert result.expected_roi == pytest.approx((100 - 10) / 10)
        assert result.median_rank == 1

    def test_finishing_out_of_the_money_pays_nothing(self):
        # Candidate's captain is now the worst on the slate, and this table pays
        # only the top two of three entries.
        top_two = PayoutTable.from_tiers([(1, 1, 100.0), (2, 2, 60.0)])
        result = simulate_contest(
            CANDIDATE, [WORSE, WORST], PLAYERS, fixed_scores(p0=1.0), top_two,
            entry_fee=10.0, trials=5, seed=0,
        )
        assert result.median_rank == 3
        assert result.mean_payout == 0.0
        assert result.cash_rate == 0.0
        assert result.expected_roi == pytest.approx(-1.0)

    def test_free_contest_has_no_roi_but_still_places(self):
        result = simulate_contest(
            CANDIDATE, [WORSE], PLAYERS, fixed_scores(), PAYOUTS, trials=3, seed=0,
        )
        assert result.expected_roi is None
        assert result.win_rate == 1.0

    def test_cash_rate_counts_any_prize(self):
        # Second of three: pays $60, so it cashes without winning.
        result = simulate_contest(
            CANDIDATE, [WORSE, WORST], PLAYERS, fixed_scores(p0=15.0), PAYOUTS,
            entry_fee=10.0, trials=4, seed=0,
        )
        assert result.median_rank == 2
        assert result.cash_rate == 1.0
        assert result.win_rate == 0.0
        assert result.mean_payout == pytest.approx(60.0)


class TestTieSplitting:
    def test_a_duplicate_splits_the_pooled_prize(self):
        # Candidate ties one identical entry for the win. They occupy ranks 1-2,
        # so each takes (100 + 60) / 2 = 80.
        result = simulate_contest(
            CANDIDATE, [CANDIDATE, WORSE], PLAYERS, fixed_scores(), PAYOUTS,
            entry_fee=10.0, trials=5, seed=0,
        )
        assert result.duplicates == 1
        assert result.mean_payout == pytest.approx(80.0)
        assert result.expected_roi == pytest.approx(7.0)

    def test_two_duplicates_split_three_ways(self):
        # Ranks 1-3 pooled: (100 + 60 + 20) / 3 = 60.
        result = simulate_contest(
            CANDIDATE, [CANDIDATE, CANDIDATE, WORSE], PLAYERS, fixed_scores(),
            PAYOUTS, entry_fee=10.0, trials=5, seed=0,
        )
        assert result.duplicates == 2
        assert result.mean_payout == pytest.approx(60.0)

    def test_duplication_strictly_reduces_payout(self):
        # The Showdown thesis, priced: same lineup, same scores, more copies.
        payouts = []
        for copies in range(4):
            result = simulate_contest(
                CANDIDATE, [CANDIDATE] * copies + [WORSE], PLAYERS,
                fixed_scores(), PAYOUTS, entry_fee=10.0, trials=3, seed=0,
            )
            payouts.append(result.mean_payout)
        assert payouts == sorted(payouts, reverse=True)
        assert payouts[0] > payouts[-1]

    def test_ties_beyond_the_paid_places_pay_nothing(self):
        flat = PayoutTable.from_tiers([(1, 1, 100.0)])
        result = simulate_contest(
            CANDIDATE, [CANDIDATE, WORSE], PLAYERS, fixed_scores(p0=1.0), flat,
            entry_fee=10.0, trials=3, seed=0,
        )
        # Tied for last: ranks 2-3, neither paid.
        assert result.mean_payout == 0.0


class TestTopPercentile:
    def test_top1_rate_on_a_large_field(self):
        field = [WORSE] * 99
        result = simulate_contest(
            CANDIDATE, field, PLAYERS, fixed_scores(), PAYOUTS,
            entry_fee=1.0, trials=3, seed=0,
        )
        assert result.entries == 100
        assert result.top1_rate == 1.0   # always first
        assert result.win_rate == 1.0

    def test_missing_the_top_percentile(self):
        field = [WORSE] * 99
        result = simulate_contest(
            CANDIDATE, field, PLAYERS, fixed_scores(p0=1.0), PAYOUTS,
            entry_fee=1.0, trials=3, seed=0,
        )
        assert result.top1_rate == 0.0


class TestValidation:
    def test_empty_field_is_refused(self):
        with pytest.raises(ContestError, match="field is empty"):
            simulate_contest(CANDIDATE, [], PLAYERS, fixed_scores(), PAYOUTS)

    def test_zero_trials_is_refused(self):
        with pytest.raises(ContestError, match="at least 1"):
            simulate_contest(CANDIDATE, [WORSE], PLAYERS, fixed_scores(), PAYOUTS,
                             trials=0)

    def test_lineup_outside_the_pool_is_refused(self):
        stranger = Lineup("ghost", ("p1", "p2", "p3", "p4", "p5"))
        with pytest.raises(ContestError, match="not in the pool"):
            simulate_contest(stranger, [WORSE], PLAYERS, fixed_scores(), PAYOUTS)

    def test_a_misshaped_score_model_is_caught(self):
        def wrong(rng, trials):
            return np.zeros((trials, 3))
        with pytest.raises(ContestError, match="score model returned"):
            simulate_contest(CANDIDATE, [WORSE], PLAYERS, wrong, PAYOUTS, trials=2)

    def test_batching_does_not_change_the_answer(self):
        kwargs = dict(players=PLAYERS, draw_scores=fixed_scores(), payouts=PAYOUTS,
                      entry_fee=10.0, trials=10, seed=0)
        a = simulate_contest(CANDIDATE, [WORSE, WORST], batch=1, **kwargs)
        b = simulate_contest(CANDIDATE, [WORSE, WORST], batch=100, **kwargs)
        assert a.mean_payout == b.mean_payout


class TestScoreModel:
    def test_shape_and_floor(self):
        model = contest.independent_normal_scores([10.0, 5.0, 0.0], cv=2.0)
        drawn = model(np.random.default_rng(0), 50)
        assert drawn.shape == (50, 3)
        assert (drawn >= 0).all()      # floored

    def test_zero_variance_returns_the_projection(self):
        model = contest.independent_normal_scores([10.0, 5.0], cv=0.0)
        drawn = model(np.random.default_rng(0), 4)
        assert (drawn == np.array([10.0, 5.0])).all()

    def test_spread_scales_with_projection(self):
        # A 20-point player should swing further than a 2-point one.
        model = contest.independent_normal_scores([20.0, 2.0], cv=0.5)
        drawn = model(np.random.default_rng(0), 2000)
        assert drawn[:, 0].std() > drawn[:, 1].std() * 3

    def test_negative_projection_is_refused(self):
        with pytest.raises(ContestError, match="non-negative"):
            contest.independent_normal_scores([-1.0])
