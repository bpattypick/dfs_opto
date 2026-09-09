"""Duplication model tests (roadmap Step 3c, task T7)."""

from __future__ import annotations

import numpy as np
import pytest

from src import duplication
from src.contest import ContestError, PayoutTable
from src.showdown import Lineup

A = Lineup("p0", ("p1", "p2", "p3", "p4", "p5"))
A_REORDERED = Lineup("p0", ("p5", "p4", "p3", "p2", "p1"))
B = Lineup("p6", ("p1", "p2", "p3", "p4", "p5"))
C = Lineup("p7", ("p1", "p2", "p3", "p4", "p5"))
PLAYERS = [f"p{i}" for i in range(8)]

PAYOUTS = PayoutTable.from_tiers([(1, 1, 100.0), (2, 2, 40.0), (3, 5, 10.0)])


def fixed_scores():
    """p0 best, then p6, then p7 — so A wins, B second, C third."""
    base = np.zeros(len(PLAYERS))
    base[0], base[6], base[7] = 30.0, 20.0, 10.0

    def draw(rng, trials):
        return np.tile(base, (trials, 1))
    return draw


class TestDupCounting:
    def test_counts_exact_matches(self):
        assert duplication.dup_count(A, [A, A, B, C]) == 2

    def test_flex_order_does_not_matter(self):
        # The same six players is the same entry, however it was pasted in.
        assert duplication.dup_count(A, [A_REORDERED, B]) == 1

    def test_a_different_captain_is_a_different_lineup(self):
        # Same five FLEX, different CPT — not a duplicate, and scores differently.
        assert duplication.dup_count(A, [B, C]) == 0

    def test_rate_is_a_share_of_the_field(self):
        assert duplication.dup_rate(A, [A, A, B, C]) == 0.5

    def test_rate_needs_a_field(self):
        with pytest.raises(ContestError, match="field is empty"):
            duplication.dup_rate(A, [])


class TestDupEstimate:
    def test_defaults_to_the_generated_field_size(self):
        assert duplication.dup_estimate(A, [A, A, B, C]) == 2.0

    def test_scales_to_the_contest_actually_entered(self):
        # You rarely enter a contest the size of the field you simulated, so the
        # ledger wants the estimate scaled to the real one.
        assert duplication.dup_estimate(A, [A, B, C, C], contest_size=1000) == 250.0

    def test_zero_when_the_lineup_is_unique(self):
        assert duplication.dup_estimate(A, [B, C]) == 0.0


class TestFieldWithout:
    def test_keeps_the_field_size_fixed(self):
        # Deleting copies would shrink the contest, changing ranks and payout
        # mapping, and contaminate the comparison.
        field = [A, A, A, B, C]
        swapped = duplication.field_without(A, field, np.random.default_rng(0))
        assert len(swapped) == len(field)

    def test_removes_every_copy(self):
        swapped = duplication.field_without(A, [A, A, B, C], np.random.default_rng(0))
        assert duplication.dup_count(A, swapped) == 0

    def test_is_a_no_op_when_there_are_no_copies(self):
        field = [B, C]
        assert duplication.field_without(A, field, np.random.default_rng(0)) == field

    def test_an_all_identical_field_has_no_counterfactual(self):
        with pytest.raises(ContestError, match="no counterfactual"):
            duplication.field_without(A, [A, A], np.random.default_rng(0))


class TestCompareDuplication:
    def test_duplication_costs_roi(self):
        # A wins outright, but 4 copies split first place 5 ways.
        field = [A] * 4 + [B, C] * 3
        table = duplication.compare_duplication(
            [A], field, PLAYERS, fixed_scores(), PAYOUTS,
            entry_fee=5.0, trials=5, seed=0, labels=["chalk"],
        )
        row = table.iloc[0]
        assert row["dup_count"] == 4
        assert row["raw_roi"] > row["dup_roi"]
        assert row["roi_cost"] == pytest.approx(row["raw_roi"] - row["dup_roi"])

    def test_a_unique_lineup_pays_no_duplication_cost(self):
        # B is not in this field, so its counterfactual is the field itself.
        table = duplication.compare_duplication(
            [B], [A, A, C, C], PLAYERS, fixed_scores(), PAYOUTS,
            entry_fee=5.0, trials=5, seed=0,
        )
        assert table.iloc[0]["dup_count"] == 0
        assert table.iloc[0]["roi_cost"] == pytest.approx(0.0)

    def test_a_candidate_present_in_the_field_counts_as_duplicated(self):
        # The field is the opponents. A field entry identical to yours means
        # somebody else built the same lineup — that is the duplicate.
        table = duplication.compare_duplication(
            [B], [A, B, C], PLAYERS, fixed_scores(), PAYOUTS,
            entry_fee=5.0, trials=5, seed=0,
        )
        assert table.iloc[0]["dup_count"] == 1

    def test_results_are_ranked_by_dup_adjusted_roi(self):
        table = duplication.compare_duplication(
            [A, B], [A] * 6 + [C] * 6, PLAYERS, fixed_scores(), PAYOUTS,
            entry_fee=5.0, trials=5, seed=0, labels=["A", "B"],
        )
        assert table["dup_roi"].is_monotonic_decreasing

    def test_duplication_alone_cannot_overtake_a_better_lineup(self):
        # Worth pinning down, because it bounds the whole strategy. With fixed
        # scores A always outscores B, so A's copies occupy the ranks above B.
        # A's split share is the *average* of the top k+1 prizes and B takes the
        # (k+1)th; in a decreasing payout table the average is never smaller.
        # Duplication does not make a worse lineup win — see the next test for
        # what actually drives the inversion.
        for copies in (2, 4, 8):
            table = duplication.compare_duplication(
                [A, B], [A] * copies + [C] * 4, PLAYERS, fixed_scores(), PAYOUTS,
                entry_fee=5.0, trials=3, seed=0, labels=["A", "B"],
            ).set_index("label")
            assert table.loc["A", "dup_roi"] >= table.loc["B", "dup_roi"]

    def test_with_score_variance_the_less_duplicated_lineup_wins(self):
        # The real mechanism. Once scores vary, B sometimes finishes above the
        # A block, and on those trials keeps a prize it would otherwise split.
        # That is what inverted the ranking on the 2026-w01 slate: chalk had a
        # better projection, better median rank and a better cash rate, and was
        # still much worse to enter.
        def noisy(rng, trials):
            mean = np.zeros(len(PLAYERS))
            mean[0], mean[6], mean[7] = 22.0, 20.0, 18.0   # A only just ahead
            return np.clip(mean + rng.normal(0, 1, (trials, len(PLAYERS))) * (mean * 0.4),
                           0, None)

        table = duplication.compare_duplication(
            [A, B], [A] * 8 + [C] * 4, PLAYERS, noisy, PAYOUTS,
            entry_fee=5.0, trials=3000, seed=1, labels=["A", "B"],
        ).set_index("label")
        assert table.loc["B", "dup_roi"] > table.loc["A", "dup_roi"]
        # A is still the better lineup in isolation — only duplication flips it.
        assert table.loc["A", "raw_roi"] > table.loc["B", "raw_roi"]

    def test_labels_must_match_candidates(self):
        with pytest.raises(ContestError, match="labels must match"):
            duplication.compare_duplication(
                [A, B], [A, B, C], PLAYERS, fixed_scores(), PAYOUTS,
                entry_fee=5.0, labels=["only-one"],
            )

    def test_a_free_contest_has_no_roi_to_compare(self):
        with pytest.raises(ContestError, match="entry_fee must be positive"):
            duplication.compare_duplication(
                [A], [A, B, C], PLAYERS, fixed_scores(), PAYOUTS, entry_fee=0.0,
            )

    def test_no_candidates_is_refused(self):
        with pytest.raises(ContestError, match="no candidates"):
            duplication.compare_duplication(
                [], [A, B], PLAYERS, fixed_scores(), PAYOUTS, entry_fee=5.0,
            )


class TestMostDuplicated:
    def test_ranks_the_builds_the_field_converges_on(self):
        table = duplication.most_duplicated([A, A, A, B, B, C], top=2)
        assert table["dup_count"].tolist() == [3, 2]
        assert table.iloc[0]["dup_rate"] == pytest.approx(0.5)

    def test_needs_a_field(self):
        with pytest.raises(ContestError, match="field is empty"):
            duplication.most_duplicated([])
