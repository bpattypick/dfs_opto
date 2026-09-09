"""Placement and ROI for a Showdown contest entry.

Roadmap Step 3b, task T6. The field generator (T5) says who you are up against;
this says where you finish and what it pays. That turns a lineup's point
distribution into the only number that matters, expected ROI.

Per Monte Carlo trial: draw player scores, score the candidate and every field
lineup, rank, map the rank to the contest's payout table, accumulate.

Ties are split, which is not a detail. Two identical lineups score identically
in every trial, so a duplicated entry ties with its copies and splits their
combined prize. That is precisely how duplication destroys value in a Showdown,
and handling it here means duplication is priced without a separate model — the
dup-adjusted comparison T7 wants falls out of running this on candidates whose
duplicate counts differ.

SCORE MODEL: this module does not own one. ``draw_scores`` is injected, because
the roadmap's "existing sim" does not exist and a real one is a modelling
decision, not plumbing. ``independent_normal_scores`` is a deliberately simple
placeholder — see its docstring for why its independence assumption flatters
some lineups and penalises others.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np

from src import showdown
from src.showdown import Lineup

# Draws a (trials, n_players) matrix of fantasy scores.
ScoreModel = Callable[[np.random.Generator, int], np.ndarray]


class ContestError(RuntimeError):
    """A contest that cannot be simulated as specified."""


@dataclass(frozen=True)
class PayoutTable:
    """Prizes by finishing rank, as ``(first_rank, last_rank, prize_each)``.

    Mirrors how DK publishes a structure ("1st $500, 2nd-3rd $200, 4th-10th
    $50"). Save a copy per contest: the ledger's ``payout_structure_id`` points
    here, and the real table disappears from the site after the contest.
    """

    tiers: tuple[tuple[int, int, float], ...]

    def __post_init__(self):
        if not self.tiers:
            raise ContestError("payout table has no tiers")
        last_end = 0
        for first, last, prize in sorted(self.tiers):
            if first < 1 or last < first:
                raise ContestError(f"invalid rank range {first}-{last}")
            if first <= last_end:
                raise ContestError(
                    f"tier {first}-{last} overlaps a lower tier ending at {last_end}"
                )
            if prize < 0:
                raise ContestError(f"negative prize in tier {first}-{last}")
            last_end = last

    @classmethod
    def from_tiers(cls, tiers) -> "PayoutTable":
        return cls(tuple((int(a), int(b), float(p)) for a, b, p in tiers))

    @property
    def paid_places(self) -> int:
        return max(last for _, last, _ in self.tiers)

    @property
    def total_prizes(self) -> float:
        return sum((last - first + 1) * prize for first, last, prize in self.tiers)

    def prize(self, rank: int) -> float:
        for first, last, prize in self.tiers:
            if first <= rank <= last:
                return prize
        return 0.0

    def cumulative(self, max_rank: int) -> np.ndarray:
        """``out[r]`` = total prize money for ranks 1..r, for tie splitting."""
        per_rank = np.zeros(max_rank + 1)
        for rank in range(1, max_rank + 1):
            per_rank[rank] = self.prize(rank)
        return np.cumsum(per_rank)

    def effective_rake(self, entry_fee: float, entries: int) -> float:
        """CONTEST_RULES R4. Negative means overlay — the house is adding money."""
        collected = entry_fee * entries
        if collected <= 0:
            raise ContestError("cannot compute rake without fees")
        return 1.0 - (self.total_prizes / collected)


@dataclass(frozen=True)
class ContestResult:
    trials: int
    entries: int
    duplicates: int          # field entries identical to the candidate
    expected_roi: float | None
    mean_payout: float
    cash_rate: float
    top1_rate: float
    win_rate: float
    mean_rank: float
    median_rank: float

    def summary(self) -> str:
        roi = "n/a" if self.expected_roi is None else f"{self.expected_roi:+.1%}"
        return (
            f"ROI {roi} | cash {self.cash_rate:.1%} | top-1% {self.top1_rate:.1%} "
            f"| win {self.win_rate:.2%} | median rank {self.median_rank:,.0f} "
            f"of {self.entries:,} | {self.duplicates} duplicate(s)"
        )


def independent_normal_scores(
    projection: Sequence[float], cv: float = 0.5, floor: float = 0.0
) -> ScoreModel:
    """Placeholder score model: each player normal about their projection.

    PLACEHOLDER, and the assumption to replace first. Scores are drawn
    independently, so a QB and their WR1 are uncorrelated here. Real football
    does not work that way — a quarterback's ceiling game *is* his receivers'
    ceiling game. Independence understates the variance of a stacked lineup and
    overstates the variance of a spread-out one, which biases placement toward
    lineups that spread exposure. Any conclusion about stacking drawn from this
    model is an artefact of the model.

    ``cv`` is the coefficient of variation (sd as a fraction of projection), so
    spread scales with the projection. Scores are floored, since DK scoring
    rarely goes far below zero.
    """
    mean = np.asarray(projection, dtype=float)
    if (mean < 0).any():
        raise ContestError("projections must be non-negative")
    if cv < 0:
        raise ContestError("cv must be non-negative")

    def draw(rng: np.random.Generator, trials: int) -> np.ndarray:
        noise = rng.normal(0.0, 1.0, size=(trials, mean.size))
        return np.clip(mean + noise * (mean * cv), floor, None)

    return draw


def _index_lineups(lineups: Sequence[Lineup], players: Sequence[str]):
    position = {pid: i for i, pid in enumerate(players)}
    try:
        captain = np.array([position[l.captain] for l in lineups], dtype=int)
        flex = np.array([[position[p] for p in l.flex] for l in lineups], dtype=int)
    except KeyError as exc:
        raise ContestError(f"lineup references player {exc} not in the pool") from exc
    return captain, flex


def _score(scores: np.ndarray, captain: np.ndarray, flex: np.ndarray) -> np.ndarray:
    """(trials, n_lineups) totals, captain at 1.5x.

    Accumulated slot by slot rather than fancy-indexing the whole (trials,
    lineups, 5) block, which would be hundreds of MB for a real field.
    """
    total = scores[:, captain] * showdown.CAPTAIN_MULTIPLIER
    for slot in range(flex.shape[1]):
        total += scores[:, flex[:, slot]]
    return total


def simulate_contest(
    candidate: Lineup,
    field: Sequence[Lineup],
    players: Sequence[str],
    draw_scores: ScoreModel,
    payouts: PayoutTable,
    entry_fee: float = 0.0,
    trials: int = 1000,
    seed: int | None = None,
    batch: int = 200,
) -> ContestResult:
    """Expected ROI, cash rate and top-1% rate for one candidate lineup.

    The candidate is an additional entry, so the contest holds
    ``len(field) + 1``. Ties split the combined prize for the tied ranks, the
    way DK settles them.
    """
    if not field:
        raise ContestError("field is empty")
    if trials < 1:
        raise ContestError(f"trials must be at least 1, got {trials}")

    field_cpt, field_flex = _index_lineups(field, players)
    cand_cpt, cand_flex = _index_lineups([candidate], players)
    entries = len(field) + 1
    cumulative = payouts.cumulative(entries + 1)
    top1_cutoff = max(1, int(np.ceil(entries * 0.01)))

    rng = np.random.default_rng(seed)
    payout_sum = 0.0
    cashes = tops = wins = 0
    ranks = np.empty(trials)

    done = 0
    while done < trials:
        n = min(batch, trials - done)
        scores = draw_scores(rng, n)
        if scores.shape != (n, len(players)):
            raise ContestError(
                f"score model returned {scores.shape}, expected {(n, len(players))}"
            )
        field_totals = _score(scores, field_cpt, field_flex)
        mine = _score(scores, cand_cpt, cand_flex)[:, 0]

        better = (field_totals > mine[:, None]).sum(axis=1)
        tied = (field_totals == mine[:, None]).sum(axis=1)
        rank = better + 1
        # Tied entries occupy ranks rank..rank+tied and share the pooled prize.
        shared = cumulative[np.minimum(rank + tied, entries + 1)] - cumulative[rank - 1]
        payout = shared / (tied + 1)

        payout_sum += payout.sum()
        cashes += int((payout > 0).sum())
        tops += int((rank <= top1_cutoff).sum())
        wins += int((rank == 1).sum())
        ranks[done:done + n] = rank
        done += n

    mean_payout = payout_sum / trials
    return ContestResult(
        trials=trials,
        entries=entries,
        duplicates=sum(1 for l in field if l.key() == candidate.key()),
        expected_roi=((mean_payout - entry_fee) / entry_fee) if entry_fee else None,
        mean_payout=mean_payout,
        cash_rate=cashes / trials,
        top1_rate=tops / trials,
        win_rate=wins / trials,
        mean_rank=float(ranks.mean()),
        median_rank=float(np.median(ranks)),
    )
