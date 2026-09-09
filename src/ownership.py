"""Optimal-rate ownership baseline — the zero-cost ownership estimate.

Roadmap Step 3a, task T4. The idea: ownership is mostly "who is obviously good
value". Run the optimizer many times over lightly randomized projections and
count how often each player turns up in the optimal lineup. No training data and
no vendor feed, which is exactly why it is the baseline every later ownership
model has to beat (roadmap Step 5) — a LightGBM model that can't beat this isn't
earning its complexity.

Leakage (CLAUDE.md principle 1): this module fetches nothing and consumes only
the projections it is handed, so it cannot look ahead on its own. Passing
pre-lock projections is the caller's responsibility.

Usage:
    from src.ownership import estimate_ownership
    rates = estimate_ownership(pool, n=1000, seed=0)
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd
from pydfs_lineup_optimizer import Player, Site, Sport, get_optimizer
from pydfs_lineup_optimizer.exceptions import GenerateLineupException

# DK Showdown: the captain slot costs 1.5x salary and scores 1.5x points.
# Stored salaries are the FLEX/UTIL base (CLAUDE.md conventions), so the
# multiplier is applied here rather than read off the CPT row.
CAPTAIN_MULTIPLIER = 1.5

ROSTER_SIZE = 6  # 1 CPT + 5 FLEX
REQUIRED_COLUMNS = ("player_id", "name", "team", "salary", "projection")


class OwnershipError(RuntimeError):
    """A pool that cannot produce a meaningful ownership estimate."""


def _validate_pool(pool: pd.DataFrame) -> pd.DataFrame:
    """Reject a pool that would silently produce a wrong estimate.

    Every check here is one that would otherwise surface as a plausible-looking
    ownership table rather than an error (CLAUDE.md principle 3).
    """
    if pool is None or len(pool) == 0:
        raise OwnershipError("player pool is empty")

    missing = [c for c in REQUIRED_COLUMNS if c not in pool.columns]
    if missing:
        raise OwnershipError(f"player pool is missing columns {missing}")

    pool = pool.loc[:, list(REQUIRED_COLUMNS)].reset_index(drop=True)

    dupes = pool["player_id"][pool["player_id"].duplicated()].tolist()
    if dupes:
        # A Showdown pool holds one row per player; the CPT/FLEX split is
        # applied below. Duplicate ids here usually mean the DK export's two
        # rows per player were loaded verbatim.
        raise OwnershipError(f"duplicate player_id in pool: {sorted(set(dupes))}")

    for column in ("salary", "projection"):
        if pool[column].isna().any():
            bad = pool.loc[pool[column].isna(), "player_id"].tolist()
            raise OwnershipError(f"null {column} for {bad}")
    if (pool["salary"] <= 0).any():
        bad = pool.loc[pool["salary"] <= 0, "player_id"].tolist()
        raise OwnershipError(f"non-positive salary for {bad}")

    teams = sorted(pool["team"].dropna().unique())
    if len(teams) != 2:
        # Showdown is a single game. One team means a bad filter; three means
        # two slates got mixed, and the resulting rates would be meaningless.
        raise OwnershipError(
            f"a Showdown pool must contain exactly 2 teams, got {len(teams)}: {teams}"
        )
    if len(pool) < ROSTER_SIZE:
        raise OwnershipError(
            f"pool has {len(pool)} players, need at least {ROSTER_SIZE}"
        )

    return pool


def _build_players(pool: pd.DataFrame, projections: Sequence[float]) -> list[Player]:
    """Two optimizer entries per player: one CPT, one FLEX.

    The optimizer identifies a person by name, not by the id passed to it, and
    uses that to keep one player out of both slots. Our canonical key is the
    GSIS id, so the id is passed as the name — two genuinely different players
    who share a name would otherwise be silently collapsed into one.
    """
    players: list[Player] = []
    for (_, row), projection in zip(pool.iterrows(), projections):
        key = str(row["player_id"])
        players.append(
            Player(f"{key}_FLEX", key, "", ["FLEX"], row["team"],
                   float(row["salary"]), float(projection))
        )
        players.append(
            Player(f"{key}_CPT", key, "", ["CPT"], row["team"],
                   float(row["salary"]) * CAPTAIN_MULTIPLIER,
                   float(projection) * CAPTAIN_MULTIPLIER)
        )
    return players


def optimal_lineup(pool: pd.DataFrame, projections: Sequence[float]) -> tuple[str, list[str]]:
    """Solve one Showdown lineup. Returns (captain_id, flex_ids).

    Salary cap, the 1 CPT + 5 FLEX shape, and DK's both-teams-represented rule
    are enforced by the optimizer's DRAFTKINGS_CAPTAIN_MODE settings.
    """
    optimizer = get_optimizer(Site.DRAFTKINGS_CAPTAIN_MODE, Sport.FOOTBALL)
    optimizer.player_pool.load_players(_build_players(pool, projections))
    try:
        lineup = next(optimizer.optimize(1))
    except (GenerateLineupException, StopIteration) as exc:
        raise OwnershipError(
            "optimizer could not build a legal lineup from this pool — check the "
            "salary cap is reachable and both teams have enough players"
        ) from exc

    captain, flex = None, []
    for player in lineup.players:
        key = player.full_name.strip()
        if player.lineup_position == "CPT":
            captain = key
        else:
            flex.append(key)
    if captain is None or len(flex) != ROSTER_SIZE - 1:
        raise OwnershipError(
            f"optimizer returned a malformed lineup: cpt={captain}, flex={flex}"
        )
    return captain, flex


def estimate_ownership(
    pool: pd.DataFrame,
    n: int = 1000,
    jitter: float = 0.15,
    seed: int | None = None,
) -> pd.DataFrame:
    """Per-player CPT and FLEX rates over ``n`` randomized optimizer runs.

    ``jitter`` is the relative standard deviation applied to each projection —
    the "lightly randomized" of roadmap Step 3a. Too little and every trial
    returns the same lineup, giving rates of 0 or 1; too much and the estimate
    converges on salary-weighted noise.

    Returns one row per pool player, including those never selected, sorted by
    total_pct descending. Rates are fractions in [0, 1].
    """
    pool = _validate_pool(pool)
    if n < 1:
        raise OwnershipError(f"n must be at least 1, got {n}")
    if jitter < 0:
        raise OwnershipError(f"jitter must be non-negative, got {jitter}")

    rng = np.random.default_rng(seed)
    base = pool["projection"].to_numpy(dtype=float)

    cpt_counts = {pid: 0 for pid in pool["player_id"].astype(str)}
    flex_counts = dict.fromkeys(cpt_counts, 0)

    for _ in range(n):
        # Multiplicative noise keeps the spread proportional to the projection:
        # a 3-point kicker shouldn't swing as far as a 20-point quarterback.
        noise = rng.normal(1.0, jitter, size=len(base)) if jitter else np.ones(len(base))
        captain, flex = optimal_lineup(pool, np.clip(base * noise, 0.0, None))
        cpt_counts[captain] += 1
        for pid in flex:
            flex_counts[pid] += 1

    rates = pool.loc[:, ["player_id", "name", "team"]].copy()
    ids = rates["player_id"].astype(str)
    rates["cpt_pct"] = [cpt_counts[i] / n for i in ids]
    rates["flex_pct"] = [flex_counts[i] / n for i in ids]
    rates["total_pct"] = rates["cpt_pct"] + rates["flex_pct"]
    return rates.sort_values("total_pct", ascending=False).reset_index(drop=True)
