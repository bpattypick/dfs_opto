"""Field generator — synthetic opponent lineups for a Showdown contest.

Roadmap Step 3a, task T5. Given per-player ownership estimates, sample a field
of plausible opponent lineups. This is what turns a lineup's *point
distribution* into an *expected finish*: without opponents there is nothing to
place against.

Sampling method: each lineup is drawn slot by slot, weighted by how far each
player still is from their target selection count for the field. Naive weighted
sampling drifts — expensive players get rejected by the cap more often than
their ownership implies, and the realized field quietly stops matching the
input. Tracking the running deficit self-corrects: a player who is behind gets
likelier, one at target drops out.

The generated field is only as good as the ownership estimates fed to it. Those
come from `src/ownership.py` today (the optimal-rate baseline) and from the
December LightGBM model later; calibration against real contest standings is
what tells you which is closer to a real field.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src import showdown
from src.showdown import Lineup

# Enough retries to clear an unlucky draw, few enough to fail fast on a pool
# that genuinely cannot make a legal lineup.
MAX_DRAWS_PER_LINEUP = 200

# Fraction of a player's target kept as baseline weight even once their deficit
# is spent, so a player at target never drops out of the draw entirely.
DEFICIT_FLOOR = 0.05

# Cap rejections bias the deficit: lineups containing expensive players get
# rejected more often, so those players fall behind while cheap ones fill up.
# Chased strictly, the tail of the field ends up owing its remaining selections
# entirely to players who cannot fit under the cap together, and no legal lineup
# exists. So deficit-chasing is best-effort: after this share of the retries,
# fall back to plain ownership-proportional weighting, which is known feasible
# because the estimates came from lineups that respected the cap.
CHASE_SHARE = 0.5

POOL_COLUMNS = ("player_id", "team", "salary")
OWNERSHIP_COLUMNS = ("player_id", "cpt_pct", "flex_pct")


class FieldError(RuntimeError):
    """A field that cannot be generated as specified."""


def _validate(pool: pd.DataFrame, ownership: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    for frame, columns, label in (
        (pool, POOL_COLUMNS, "pool"),
        (ownership, OWNERSHIP_COLUMNS, "ownership"),
    ):
        if frame is None or len(frame) == 0:
            raise FieldError(f"{label} is empty")
        missing = [c for c in columns if c not in frame.columns]
        if missing:
            raise FieldError(f"{label} is missing columns {missing}")

    pool = pool.loc[:, list(POOL_COLUMNS)].reset_index(drop=True)
    ownership = ownership.loc[:, list(OWNERSHIP_COLUMNS)].reset_index(drop=True)

    if pool["player_id"].duplicated().any():
        raise FieldError("duplicate player_id in pool")
    if len(pool) < showdown.ROSTER_SIZE:
        raise FieldError(
            f"pool has {len(pool)} players, need at least {showdown.ROSTER_SIZE}"
        )
    if len(pool["team"].dropna().unique()) != 2:
        raise FieldError("a Showdown pool must contain exactly 2 teams")

    missing_rates = set(pool["player_id"]) - set(ownership["player_id"])
    if missing_rates:
        # Silently treating these as 0% would quietly shrink the player pool.
        raise FieldError(f"no ownership estimate for {sorted(missing_rates)}")

    ownership = (
        ownership.set_index("player_id").loc[pool["player_id"]].reset_index()
    )
    for column in ("cpt_pct", "flex_pct"):
        if ownership[column].isna().any():
            raise FieldError(f"null {column} in ownership estimates")
        if (ownership[column] < 0).any():
            raise FieldError(f"negative {column} in ownership estimates")
    return pool, ownership


def _sample(rng, candidates: np.ndarray, weights: np.ndarray, size: int) -> np.ndarray:
    """Weighted draw without replacement, tolerant of all-zero weights."""
    weights = np.clip(weights, 0.0, None) + 1e-9  # keep enough non-zero mass
    return rng.choice(candidates, size=size, replace=False, p=weights / weights.sum())


def generate_field(
    pool: pd.DataFrame,
    ownership: pd.DataFrame,
    size: int,
    seed: int | None = None,
    cap: int = showdown.SALARY_CAP,
    tolerance: float = 0.02,
) -> list[Lineup]:
    """Sample ``size`` DK-legal opponent lineups matching the ownership input.

    Every returned lineup satisfies the cap, the 1 CPT + 5 FLEX shape and the
    both-teams rule.

    ``tolerance`` is the repair pass's stopping target, not a guarantee: the
    pass swaps players until realized ownership is within it *or* no legal swap
    improves matters. Exact marginals are not always simultaneously reachable
    under the cap, so a small residual is expected — measured at ~0.03 per slot
    for fields of 100-5,000. Check the result with ``realized_ownership`` rather
    than assuming the target was met.
    """
    pool, ownership = _validate(pool, ownership)
    if size < 1:
        raise FieldError(f"field size must be at least 1, got {size}")

    rng = np.random.default_rng(seed)
    ids = pool["player_id"].to_numpy()
    salary = dict(zip(pool["player_id"], pool["salary"].astype(float)))
    team = dict(zip(pool["player_id"], pool["team"]))
    index = {pid: i for i, pid in enumerate(ids)}

    # Targets in absolute selections, so the deficit is a count we can chase.
    cpt_target = ownership["cpt_pct"].to_numpy(dtype=float) * size
    flex_target = ownership["flex_pct"].to_numpy(dtype=float) * size
    cpt_taken = np.zeros(len(ids))
    flex_taken = np.zeros(len(ids))

    field: list[Lineup] = []
    for _ in range(size):
        lineup = None
        chase_until = int(MAX_DRAWS_PER_LINEUP * CHASE_SHARE)
        for attempt in range(MAX_DRAWS_PER_LINEUP):
            chasing = attempt < chase_until
            cpt_weights = (
                (cpt_target - cpt_taken) + DEFICIT_FLOOR * cpt_target
                if chasing else cpt_target
            )
            captain = _sample(rng, ids, cpt_weights, 1)[0]
            others = np.array([p for p in ids if p != captain])
            weights = np.array([
                (flex_target[index[p]] - flex_taken[index[p]])
                + DEFICIT_FLOOR * flex_target[index[p]]
                if chasing else flex_target[index[p]]
                for p in others
            ])
            flex = _sample(rng, others, weights, showdown.FLEX_SLOTS)
            candidate = Lineup(captain=str(captain), flex=tuple(str(p) for p in flex))
            if showdown.is_legal(candidate, salary, team, cap):
                lineup = candidate
                break
        if lineup is None:
            raise FieldError(
                f"could not draw a legal lineup in {MAX_DRAWS_PER_LINEUP} attempts "
                f"after {len(field)} of {size} — check the salary cap is reachable "
                "and that ownership estimates aren't concentrated on players who "
                "cannot fit together"
            )
        cpt_taken[index[lineup.captain]] += 1
        for p in lineup.flex:
            flex_taken[index[p]] += 1
        field.append(lineup)

    return _repair_field(field, ids, cpt_target, flex_target, salary, team, cap,
                         rng, tolerance)


def _repair_field(
    field, ids, cpt_target, flex_target, salary, team, cap, rng, tolerance,
    max_swaps=None,
):
    """Swap players between lineups until realized ownership meets the target.

    Slot-wise sampling alone cannot hit the marginals. Ownership estimates are
    marginal rates, but the salary cap makes players compete for the same
    lineup, so rejection systematically suppresses expensive players — the
    residual error plateaus instead of shrinking as the field grows.

    This pass corrects it directly: move a player who is over their target out
    of a lineup and an under-target player in, keeping the swap only if the
    lineup is still DK-legal. Two moves are available — replacing a FLEX player,
    and changing a lineup's captain (including trading the captain with one of
    its own FLEX players, which leaves the roster identical and only shifts
    which player carries the 1.5x). Every accepted swap strictly reduces total
    absolute error, so progress is monotone and the loop stops when it can no
    longer improve.
    """
    size = len(field)
    if max_swaps is None:
        max_swaps = 100 * size
    index = {pid: i for i, pid in enumerate(ids)}

    cpt_count = np.zeros(len(ids))
    flex_count = np.zeros(len(ids))
    holds_flex: dict = {pid: set() for pid in ids}
    has_cpt: dict = {pid: set() for pid in ids}
    for i, lineup in enumerate(field):
        cpt_count[index[lineup.captain]] += 1
        has_cpt[lineup.captain].add(i)
        for pid in lineup.flex:
            flex_count[index[pid]] += 1
            holds_flex[pid].add(i)

    def place(i, lineup):
        old = field[i]
        cpt_count[index[old.captain]] -= 1
        has_cpt[old.captain].discard(i)
        for pid in old.flex:
            flex_count[index[pid]] -= 1
            holds_flex[pid].discard(i)
        cpt_count[index[lineup.captain]] += 1
        has_cpt[lineup.captain].add(i)
        for pid in lineup.flex:
            flex_count[index[pid]] += 1
            holds_flex[pid].add(i)
        field[i] = lineup

    def ranked(counts, targets, holders):
        """(over-target, under-target) players, worst first, that we can act on."""
        error = counts - targets
        over = [ids[i] for i in np.argsort(-error)
                if error[i] > 0 and holders[ids[i]]]
        under = [ids[i] for i in np.argsort(error) if error[i] < 0]
        return over, under

    def try_flex():
        over, under = ranked(flex_count, flex_target, holds_flex)
        for out_pid in over:
            for in_pid in under:
                options = [i for i in holds_flex[out_pid]
                           if in_pid not in field[i].players]
                rng.shuffle(options)
                for i in options:
                    lineup = field[i]
                    swapped = Lineup(
                        lineup.captain,
                        tuple(in_pid if p == out_pid else p for p in lineup.flex),
                    )
                    if showdown.is_legal(swapped, salary, team, cap):
                        place(i, swapped)
                        return True
        return False

    def try_captain():
        over, under = ranked(cpt_count, cpt_target, has_cpt)
        for out_pid in over:
            for in_pid in under:
                options = list(has_cpt[out_pid])
                rng.shuffle(options)
                for i in options:
                    lineup = field[i]
                    if in_pid in lineup.flex:
                        # Trade roles: same six players, the 1.5x moves over.
                        swapped = Lineup(
                            in_pid,
                            tuple(out_pid if p == in_pid else p for p in lineup.flex),
                        )
                    elif in_pid == lineup.captain:
                        continue
                    else:
                        swapped = Lineup(in_pid, lineup.flex)
                    if showdown.is_legal(swapped, salary, team, cap):
                        place(i, swapped)
                        return True
        return False

    for _ in range(max_swaps):
        cpt_error = np.abs(cpt_count - cpt_target).max() / size
        flex_error = np.abs(flex_count - flex_target).max() / size
        if max(cpt_error, flex_error) <= tolerance:
            break
        # Attack whichever slot is further out, but accept progress on either.
        first, second = ((try_flex, try_captain) if flex_error >= cpt_error
                         else (try_captain, try_flex))
        if not (first() or second()):
            break

    return field


def realized_ownership(field: list[Lineup], pool: pd.DataFrame) -> pd.DataFrame:
    """Per-player CPT/FLEX/total rates actually present in a generated field.

    Same shape as ``src.ownership.estimate_ownership`` output, so the two can be
    compared directly — that comparison is the generator's calibration check.
    """
    if not field:
        raise FieldError("field is empty")
    ids = pool["player_id"].tolist()
    cpt = dict.fromkeys(ids, 0)
    flex = dict.fromkeys(ids, 0)
    for lineup in field:
        if lineup.captain in cpt:
            cpt[lineup.captain] += 1
        for p in lineup.flex:
            if p in flex:
                flex[p] += 1

    n = len(field)
    out = pd.DataFrame({
        "player_id": ids,
        "cpt_pct": [cpt[i] / n for i in ids],
        "flex_pct": [flex[i] / n for i in ids],
    })
    out["total_pct"] = out["cpt_pct"] + out["flex_pct"]
    return out.sort_values("total_pct", ascending=False).reset_index(drop=True)
