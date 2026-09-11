"""Correlated, right-skewed player scores — the real score simulator (T13).

Replaces ``contest.independent_normal_scores``, which drew every player from
an independent normal with one flat coefficient of variation. Both of those
assumptions were measured against 34,936 player-weeks and both were wrong
(docs/data-sources.md, "What a Showdown score model has to reproduce"):

- spread grows with projection sub-linearly, sd = a + b * projection, and the
  slope is position-specific — a QB's spread barely depends on his level;
- the residual is right-skewed with fat tails (skew +0.93): a player scores
  under half his projection a third of the time and over double it 13%;
- teammates and opponents are correlated in a small block structure — a QB and
  his top pass-catcher at +0.33, opposing QBs at +0.19, a DST against the QB it
  faces at -0.25 — and almost every other pair is independent.

The model is a Gaussian copula over lognormal marginals. Draw a correlated
standard-normal vector per trial from the block-structure matrix, then map each
coordinate through a lognormal with that player's projected mean and fitted sd.
Lognormal is right-skewed, non-negative, and has closed-form parameters from
(mean, sd), so nothing here needs scipy. Every number is measured; nothing is
a placeholder.

Known limits, stated: DK scores can go slightly negative (a QB with picks and
no touchdowns); lognormal cannot, and that tail is ignored. The correlation
table is pooled over 2020-2025 and is not yet conditioned on game total or
spread.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.contest import ContestError

# sd = a + b * projection, fitted per position on 2020-2025 residuals.
VARIANCE = {
    "QB":  (7.90, 0.037),
    "RB":  (4.31, 0.283),
    "WR":  (3.57, 0.364),
    "TE":  (2.53, 0.426),
    "DST": (5.10, 0.057),
    "K":   (3.81, 0.310),    # not in the stats feed; overall fit as the prior
}
VARIANCE_DEFAULT = (3.81, 0.310)

# Residual correlation by (relationship, unordered position pair). Pairs not
# listed are treated as independent, which the measurement supports: the
# largest omitted teammate pair (QB-RB) was +0.03 and WR-WR +0.01.
CORRELATION = {
    ("teammate", "QB", "TOP"): 0.327,    # QB and his highest-projected pass-catcher
    ("teammate", "QB", "WR"):  0.213,
    ("teammate", "QB", "TE"):  0.170,
    ("teammate", "DST", "QB"): -0.106,
    ("teammate", "DST", "TE"): -0.063,
    ("teammate", "DST", "WR"): -0.050,
    ("opponent", "QB", "QB"):  0.185,
    ("opponent", "DST", "QB"): -0.251,
    ("opponent", "DST", "RB"): -0.155,
    ("opponent", "DST", "WR"): -0.103,
    ("opponent", "DST", "TE"): -0.065,
    ("opponent", "QB", "WR"):  0.071,
    ("opponent", "QB", "TE"):  0.063,
}
PASS_CATCHERS = ("WR", "TE")
REQUIRED = ("player_id", "position", "team", "projection")


def sd_for(projection, position: str) -> float:
    a, b = VARIANCE.get(position, VARIANCE_DEFAULT)
    return a + b * float(projection)


def lognormal_params(mean: float, sd: float) -> tuple[float, float]:
    """(mu, sigma) of the underlying normal for a lognormal with this mean/sd."""
    if mean <= 0:
        raise ContestError(f"lognormal mean must be positive, got {mean}")
    if sd <= 0:
        raise ContestError(f"lognormal sd must be positive, got {sd}")
    sigma2 = np.log1p((sd / mean) ** 2)
    return float(np.log(mean) - sigma2 / 2), float(np.sqrt(sigma2))


def _pair_key(rel: str, pos_a: str, pos_b: str, a_is_top: bool, b_is_top: bool):
    """Look up the correlation for one pair, trying the TOP form first."""
    if rel == "teammate" and {pos_a, pos_b} & {"QB"}:
        qb_side_top = (pos_a == "QB" and b_is_top) or (pos_b == "QB" and a_is_top)
        if qb_side_top and (pos_a in PASS_CATCHERS or pos_b in PASS_CATCHERS):
            return CORRELATION[("teammate", "QB", "TOP")]
    # Unordered lookup.
    for x, y in ((pos_a, pos_b), (pos_b, pos_a)):
        if (rel, x, y) in CORRELATION:
            return CORRELATION[(rel, x, y)]
    return 0.0


def build_correlation(slate: pd.DataFrame) -> np.ndarray:
    """Correlation matrix over the slate's players, in slate row order.

    Each team's highest-projected WR/TE is its QB's "top" target. Pairs on
    different teams are opponents (a Showdown slate is one game); pairs on the
    same team are teammates. Repaired to positive semi-definite if the block
    values, which were each measured separately, do not quite compose.
    """
    missing = [c for c in REQUIRED if c not in slate.columns]
    if missing:
        raise ContestError(f"slate is missing {missing}")
    if slate["player_id"].duplicated().any():
        raise ContestError("duplicate player_id in slate")

    s = slate.reset_index(drop=True)
    top = set()
    for team, g in s[s["position"].isin(PASS_CATCHERS)].groupby("team"):
        top.add(g["projection"].idxmax())

    n = len(s)
    R = np.eye(n)
    pos, team = s["position"].tolist(), s["team"].tolist()
    for i in range(n):
        for j in range(i + 1, n):
            rel = "teammate" if team[i] == team[j] else "opponent"
            r = _pair_key(rel, pos[i], pos[j], i in top, j in top)
            R[i, j] = R[j, i] = r

    # PSD repair: clip negative eigenvalues, restore unit diagonal.
    w, v = np.linalg.eigh(R)
    if w.min() < 0:
        w = np.clip(w, 1e-6, None)
        R = v @ np.diag(w) @ v.T
        d = np.sqrt(np.diag(R))
        R = R / np.outer(d, d)
    return R


@dataclass
class CorrelatedScores:
    """A ``contest.ScoreModel`` built from a slate: correlated lognormal draws.

    ``players`` is the id order the (trials, n) matrix uses — pass it to
    ``simulate_contest`` so lineups index the right columns.
    """

    slate: pd.DataFrame

    def __post_init__(self):
        s = self.slate.reset_index(drop=True)
        missing = [c for c in REQUIRED if c not in s.columns]
        if missing:
            raise ContestError(f"slate is missing {missing}")
        if (s["projection"] < 0).any():
            raise ContestError("projections must be non-negative")
        self.players: list[str] = s["player_id"].astype(str).tolist()
        # A zero projection cannot be lognormal; give it a hair of mean so the
        # player exists in the draw and lands near zero.
        mean = s["projection"].to_numpy(dtype=float).clip(0.05, None)
        sd = np.array([sd_for(m, p) for m, p in zip(mean, s["position"])])
        params = [lognormal_params(m, v) for m, v in zip(mean, sd)]
        self.mu = np.array([p[0] for p in params])
        self.sigma = np.array([p[1] for p in params])
        self.R = build_correlation(s)
        self._chol = np.linalg.cholesky(self.R)

    def __call__(self, rng: np.random.Generator, trials: int) -> np.ndarray:
        z = rng.standard_normal((trials, len(self.players))) @ self._chol.T
        return np.exp(self.mu + self.sigma * z)
