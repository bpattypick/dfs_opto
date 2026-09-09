"""Duplication: how many field entries are identical to yours, and what it costs.

Roadmap Step 3c, task T7. In a small Showdown pool the "optimal" build gets
entered by many people, and DK splits a prize among everyone tied on score.
Identical lineups tie in every outcome, so a duplicated first place is a divided
first place. The roadmap's claim is that this is the most concrete Showdown edge
available and almost nobody casual models it.

Two things live here:

``dup_estimate`` — the measurement. Exact-match frequency of a candidate in a
generated field. Cheap, and the number recorded on every ledger entry so the
estimate becomes checkable against real contest standings later.

``compare_duplication`` — the price. Simulates each candidate twice, once
against the field as generated and once against a counterfactual field holding
no copies of that candidate, and reports both ROIs. The gap is what duplication
costs, in the only unit that matters.

The counterfactual keeps the contest size fixed by substituting other field
lineups for the copies. Simply deleting them would shrink the field, which
changes ranks and payout mapping and would contaminate the comparison with an
effect that has nothing to do with duplication.
"""

from __future__ import annotations

from collections import Counter
from typing import Sequence

import numpy as np
import pandas as pd

from src.contest import ContestError, PayoutTable, ScoreModel, simulate_contest
from src.showdown import Lineup


def dup_count(candidate: Lineup, field: Sequence[Lineup]) -> int:
    """Field entries identical to ``candidate``. FLEX order does not matter."""
    key = candidate.key()
    return sum(1 for lineup in field if lineup.key() == key)


def dup_rate(candidate: Lineup, field: Sequence[Lineup]) -> float:
    """Share of the field holding this exact lineup, in [0, 1]."""
    if not field:
        raise ContestError("field is empty")
    return dup_count(candidate, field) / len(field)


def dup_estimate(
    candidate: Lineup, field: Sequence[Lineup], contest_size: int | None = None
) -> float:
    """Expected number of identical entries in a contest of ``contest_size``.

    Defaults to the generated field's own size. This is the value recorded as
    ``dup_estimate`` on a ledger entry: a rate scales to whatever contest you
    actually entered, which is rarely the size you simulated.
    """
    rate = dup_rate(candidate, field)
    return rate * (len(field) if contest_size is None else contest_size)


def field_without(candidate: Lineup, field: Sequence[Lineup], rng) -> list[Lineup]:
    """The field with copies of ``candidate`` swapped for other entries.

    Substituting rather than deleting keeps the contest size fixed, so the
    comparison isolates duplication instead of also measuring a smaller field.
    Replacements are drawn from the field's own non-matching entries, which
    preserves its ownership character.
    """
    key = candidate.key()
    others = [lineup for lineup in field if lineup.key() != key]
    if not others:
        raise ContestError(
            "every entry in the field is identical to the candidate — there is "
            "no counterfactual to compare against"
        )
    copies = len(field) - len(others)
    if not copies:
        return list(field)
    picks = rng.integers(0, len(others), size=copies)
    return others + [others[i] for i in picks]


def compare_duplication(
    candidates: Sequence[Lineup],
    field: Sequence[Lineup],
    players: Sequence[str],
    draw_scores: ScoreModel,
    payouts: PayoutTable,
    entry_fee: float,
    trials: int = 500,
    seed: int | None = None,
    labels: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Raw vs dup-adjusted ROI for a slate's candidate pool.

    ``raw_roi`` is what the lineup would return if nobody else had it;
    ``dup_roi`` is what it returns against the field as generated. ``roi_cost``
    is the difference — the price of being on the same build as everyone else.

    Sorted by ``dup_roi``, which is the number to select on. Ranking by raw ROI
    or by projected points is what puts you on the chalk.
    """
    if not candidates:
        raise ContestError("no candidates to compare")
    if entry_fee <= 0:
        # Both ROIs would be None and every column below meaningless.
        raise ContestError("entry_fee must be positive to compare ROI")
    if labels is not None and len(labels) != len(candidates):
        raise ContestError("labels must match candidates")

    rng = np.random.default_rng(seed)
    rows = []
    for i, candidate in enumerate(candidates):
        alone = simulate_contest(
            candidate, field_without(candidate, field, rng), players, draw_scores,
            payouts, entry_fee=entry_fee, trials=trials, seed=seed,
        )
        actual = simulate_contest(
            candidate, field, players, draw_scores, payouts,
            entry_fee=entry_fee, trials=trials, seed=seed,
        )
        rows.append({
            "label": labels[i] if labels is not None else f"candidate_{i}",
            "lineup": candidate,
            "dup_count": dup_count(candidate, field),
            "dup_rate": dup_rate(candidate, field),
            "raw_roi": alone.expected_roi,
            "dup_roi": actual.expected_roi,
            "roi_cost": alone.expected_roi - actual.expected_roi,
            "cash_rate": actual.cash_rate,
            "top1_rate": actual.top1_rate,
        })
    return pd.DataFrame(rows).sort_values("dup_roi", ascending=False).reset_index(drop=True)


def most_duplicated(field: Sequence[Lineup], top: int = 5) -> pd.DataFrame:
    """The builds the field converges on — what to differentiate from."""
    if not field:
        raise ContestError("field is empty")
    counts = Counter(lineup.key() for lineup in field)
    by_key = {lineup.key(): lineup for lineup in field}
    rows = [
        {"lineup": by_key[key], "dup_count": n, "dup_rate": n / len(field)}
        for key, n in counts.most_common(top)
    ]
    return pd.DataFrame(rows)
