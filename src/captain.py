"""Captain selection by floor and ceiling, not by mean (T19).

Confirmed twice live: the optimizer captained the highest projected mean per
dollar and lost on it both times — SF@LAR on an over-projected Stafford,
DEN@KC on J.K. Dobbins, a non-receiving back who returned 3.6 on zero targets
in a 21-point blowout. The mean says nothing about that downside. The captain
slot multiplies a player's *variance* by 2.25, so the captain is where the
shape of the distribution matters most, and the pipeline had no way to see
it: candidates came from a mean-maximising optimizer, so every candidate
captained the same player and the contest sim never got to compare captains.

Three pieces, all from what already exists:

1. ``player_quantiles`` — each player's floor / median / ceiling, closed-form
   from the lognormal the score model already fits (T13: sd = a + b × proj,
   per position, right-skewed). No new parameters.
2. ``captain_candidates`` — one lineup per plausible captain, with the
   *exact* best five-FLEX complement (salary cap, both teams). Captains are
   the top by the objective's quantile, plus the top by mean so the old
   answer is always on the board to be beaten.
3. ``rank_lineups`` — every candidate's lineup-level floor / median /
   ceiling from the correlated score model (captain at 1.5×, stacks
   correlated), so a cash lineup is chosen on its floor and a GPP lineup on
   its ceiling. The contest sim still has the final say; this makes sure it
   is comparing captains.

Objectives (CONTEST_RULES R5: majority cash while the process is validated):
``cash`` ranks on the 25th percentile, ``gpp`` on the 90th.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src import contest
from src.scoremodel import lognormal_params, sd_for
from src.showdown import CAPTAIN_MULTIPLIER, FLEX_SLOTS, SALARY_CAP, Lineup

# Standard-normal quantiles; scipy is deliberately not a dependency.
Z = {0.05: -1.6449, 0.10: -1.2816, 0.25: -0.6745, 0.50: 0.0,
     0.75: 0.6745, 0.90: 1.2816, 0.95: 1.6449}
QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)

# objective -> (player/lineup quantile column, contest-sim rate to order by)
OBJECTIVES = {
    "cash": ("p25", "cash_rate"),
    "gpp": ("p90", "top1_rate"),
}
POOL_COLUMNS = ("player_id", "team", "position", "salary", "projection")

# A FLEX outside the top-N by projection is almost never in the best five;
# the cheapest few are kept too because the cap sometimes needs a filler.
COMPLEMENT_POOL = 30
COMPLEMENT_CHEAPEST = 5


class CaptainError(RuntimeError):
    """A captain decision that cannot be made as specified."""


def _column(q: float) -> str:
    return f"p{int(round(q * 100))}"


def _check_pool(pool: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in POOL_COLUMNS if c not in pool.columns]
    if missing:
        raise CaptainError(f"pool is missing {missing}")
    if pool["player_id"].duplicated().any():
        raise CaptainError("duplicate player_id in pool")
    if pool.empty:
        raise CaptainError("pool is empty")
    return pool.reset_index(drop=True)


def player_quantiles(pool: pd.DataFrame, quantiles=QUANTILES) -> pd.DataFrame:
    """Per player: projection, fitted sd, and the lognormal quantiles asked for.

    Same marginal the score simulator draws from, so a captain's "floor" here
    is exactly the tenth percentile the contest sim would produce for him.
    """
    p = _check_pool(pool)
    mean = p["projection"].to_numpy(dtype=float).clip(0.05, None)
    sd = np.array([sd_for(m, pos) for m, pos in zip(mean, p["position"])])
    params = [lognormal_params(m, s) for m, s in zip(mean, sd)]
    mu = np.array([a for a, _ in params]); sigma = np.array([b for _, b in params])
    out = p[["player_id", "team", "position", "salary", "projection"]].copy()
    out["sd"] = sd
    for q in quantiles:
        if q not in Z:
            raise CaptainError(f"no z-score for quantile {q}; choose from {sorted(Z)}")
        out[_column(q)] = np.exp(mu + sigma * Z[q])
    return out


def best_complement(
    captain: str,
    pool: pd.DataFrame,
    cap: int = SALARY_CAP,
    pool_size: int = COMPLEMENT_POOL,
) -> Lineup | None:
    """The five FLEX that maximise projected points around ``captain``.

    Exact over the candidate set (top ``pool_size`` by projection plus the
    cheapest few), under the cap with the captain at 1.5× salary and with
    both teams represented. ``None`` when no legal five exists — a captain
    priced out of a complement is not a candidate.
    """
    p = _check_pool(pool).set_index("player_id")
    if captain not in p.index:
        raise CaptainError(f"captain {captain!r} is not in the pool")
    budget = cap - CAPTAIN_MULTIPLIER * float(p.loc[captain, "salary"])
    cpt_team = p.loc[captain, "team"]

    others = p.drop(index=captain)
    others = others[others["salary"] <= budget]
    if len(others) < FLEX_SLOTS:
        return None
    by_proj = others.sort_values("projection", ascending=False)
    keep = set(by_proj.index[:pool_size]) | set(others.sort_values("salary").index[:COMPLEMENT_CHEAPEST])
    cand = others.loc[sorted(keep)]
    ids = cand.index.to_numpy()
    salary = cand["salary"].to_numpy(dtype=float)
    proj = cand["projection"].to_numpy(dtype=float)
    other_team = (cand["team"] != cpt_team).to_numpy()

    combos = np.array(list(itertools.combinations(range(len(cand)), FLEX_SLOTS)), dtype=int)
    if combos.size == 0:
        return None
    legal = (salary[combos].sum(axis=1) <= budget) & other_team[combos].any(axis=1)
    if not legal.any():
        return None
    totals = np.where(legal, proj[combos].sum(axis=1), -np.inf)
    best = combos[int(np.argmax(totals))]
    return Lineup(captain, tuple(sorted(ids[best].tolist())))


def captain_candidates(
    pool: pd.DataFrame,
    objective: str = "cash",
    n_captains: int = 10,
    cap: int = SALARY_CAP,
) -> list[Lineup]:
    """One lineup per plausible captain: top ``n_captains`` by the objective's
    quantile, union the top by mean projection. Captains with no legal
    complement are dropped."""
    if objective not in OBJECTIVES:
        raise CaptainError(f"unknown objective {objective!r}; choose from {sorted(OBJECTIVES)}")
    col = OBJECTIVES[objective][0]
    q = player_quantiles(pool)
    picks = list(q.sort_values(col, ascending=False)["player_id"].head(n_captains))
    for pid in q.sort_values("projection", ascending=False)["player_id"].head(n_captains):
        if pid not in picks:
            picks.append(pid)
    lineups = []
    for pid in picks:
        built = best_complement(pid, pool, cap=cap)
        if built is not None:
            lineups.append(built)
    return lineups


@dataclass
class LineupQuantiles:
    table: pd.DataFrame     # one row per lineup: mean and the quantiles

    def ordered(self, objective: str) -> pd.DataFrame:
        col = OBJECTIVES[objective][0]
        return self.table.sort_values([col, "mean"], ascending=False).reset_index(drop=True)


def rank_lineups(
    lineups: list[Lineup],
    players: list[str],
    draw_scores: contest.ScoreModel,
    trials: int = 4000,
    seed: int | None = None,
    quantiles=QUANTILES,
) -> LineupQuantiles:
    """Lineup-level mean and quantiles from the correlated score model.

    One draw of ``trials`` correlated score vectors, every lineup scored on
    the same draws (captain at 1.5×), quantiles per lineup. Same model the
    contest sim uses, so these agree with it by construction.
    """
    if not lineups:
        raise CaptainError("no lineups to rank")
    if trials < 100:
        raise CaptainError(f"trials must be at least 100, got {trials}")
    cpt, flex = contest._index_lineups(lineups, players)
    rng = np.random.default_rng(seed)
    scores = draw_scores(rng, trials)
    totals = contest._score(scores, cpt, flex)           # (trials, n_lineups)
    rows = {"lineup": lineups, "captain": [l.captain for l in lineups],
            "mean": totals.mean(axis=0)}
    for q in quantiles:
        rows[_column(q)] = np.quantile(totals, q, axis=0)
    return LineupQuantiles(pd.DataFrame(rows))


def captain_board(
    pool: pd.DataFrame,
    players: list[str],
    draw_scores: contest.ScoreModel,
    objective: str = "cash",
    n_captains: int = 10,
    trials: int = 4000,
    seed: int | None = None,
) -> pd.DataFrame:
    """The decision table: per candidate captain, his own floor/ceiling and
    the best lineup built around him with its floor/median/ceiling, ordered
    by the objective. Empty if no captain has a legal complement."""
    lineups = captain_candidates(pool, objective=objective, n_captains=n_captains)
    if not lineups:
        return pd.DataFrame()
    ranked = rank_lineups(lineups, players, draw_scores, trials=trials, seed=seed).ordered(objective)
    q = player_quantiles(pool).set_index("player_id")
    ranked["cpt_projection"] = ranked["captain"].map(q["projection"])
    ranked["cpt_sd"] = ranked["captain"].map(q["sd"])
    ranked["cpt_p10"] = ranked["captain"].map(q["p10"])
    ranked["cpt_p90"] = ranked["captain"].map(q["p90"])
    ranked["cpt_position"] = ranked["captain"].map(q["position"])
    return ranked
