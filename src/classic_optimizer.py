"""DK Classic optimizer wrapper — solves one legal Classic lineup for a pool.

Mirrors ``src.ownership.optimal_lineup``'s role for Showdown, but for the
9-slot no-captain Classic roster (``src/classic.py``). Classic pools are much
larger than a single-game Showdown pool (every team with a game in the
window), so this delegates to the same ``pydfs_lineup_optimizer`` ILP solver
rather than an exhaustive search — confirmed against
``get_optimizer(Site.DRAFTKINGS, Sport.FOOTBALL)``: budget 50000, positions
QB/RB/RB/WR/WR/WR/TE/FLEX(RB,WR,TE)/DST, no team-count restriction.

Leakage (CLAUDE.md principle 1): this module fetches nothing and consumes
only the projections it is handed; passing pre-lock projections is the
caller's responsibility.
"""

from __future__ import annotations

from typing import Sequence

import pandas as pd
from pydfs_lineup_optimizer import Player, Site, Sport, get_optimizer
from pydfs_lineup_optimizer.exceptions import GenerateLineupException

from src import classic

REQUIRED_COLUMNS = ("player_id", "name", "team", "position", "salary", "projection")
_SLOT_COUNTS = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "DST": 1}


class OptimizerError(RuntimeError):
    """A pool that cannot produce a legal Classic lineup."""


def _build_players(pool: pd.DataFrame, projections: Sequence[float]) -> list[Player]:
    players = []
    for (_, row), projection in zip(pool.iterrows(), projections):
        key = str(row["player_id"])
        players.append(
            Player(key, key, "", [str(row["position"])], str(row["team"]),
                   float(row["salary"]), float(projection))
        )
    return players


def optimal_lineup(pool: pd.DataFrame, projections: Sequence[float]) -> classic.Lineup:
    """Solve one Classic lineup: QB/RB/RB/WR/WR/WR/TE/FLEX/DST under the cap.

    ``pool`` needs ``player_id``, ``name``, ``team``, ``position``, ``salary``
    columns (``src.pool.POOL_COLUMNS`` plus ``position`` already included
    there); ``projections`` is a same-length sequence of per-player points,
    aligned by position in the frame.
    """
    missing = set(REQUIRED_COLUMNS) - set(pool.columns)
    if missing:
        raise OptimizerError(f"pool is missing columns: {sorted(missing)}")
    if len(pool) != len(projections):
        raise OptimizerError("pool and projections must be the same length")

    optimizer = get_optimizer(Site.DRAFTKINGS, Sport.FOOTBALL)
    optimizer.player_pool.load_players(_build_players(pool, projections))
    try:
        lineup = next(optimizer.optimize(1))
    except (GenerateLineupException, StopIteration) as exc:
        raise OptimizerError(
            "optimizer could not build a legal lineup from this pool — check "
            "the salary cap is reachable and every slot (QB/RB/WR/TE/DST) has "
            "enough eligible players"
        ) from exc

    slots: dict[str, list[str]] = {k: [] for k in _SLOT_COUNTS}
    for player in lineup.players:
        slots.setdefault(player.lineup_position, []).append(player.id)

    if any(len(slots.get(k, [])) != n for k, n in _SLOT_COUNTS.items()):
        raise OptimizerError(f"optimizer returned a malformed lineup: {slots}")

    result = classic.Lineup(
        qb=slots["QB"][0],
        rb=tuple(sorted(slots["RB"])),
        wr=tuple(sorted(slots["WR"])),
        te=slots["TE"][0],
        flex=slots["FLEX"][0],
        dst=slots["DST"][0],
    )
    salary = dict(zip(pool["player_id"].astype(str), pool["salary"].astype(float)))
    position = dict(zip(pool["player_id"].astype(str), pool["position"].astype(str)))
    classic.check_lineup(result, salary, position)  # the optimizer's output is DK-legal
    return result
