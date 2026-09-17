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

T24: the lognormal's floor was measured to be optimistic on the honest pool
(a fifth of top-12 actuals below its "10th percentile", a third below its
25th): it has no mass at zero and a thin left tail. ``EmpiricalMarginals``
replaces the *shape* with the fitted distribution of actual / projection per
position and projection level -- zeros included, mean-normalised so the
projection stays the mean -- behind the same Gaussian copula. Pass one to
``CorrelatedScores(marginals=...)``; without it the lognormal is used, so
every earlier number is reproducible. ``data/score_marginals.json`` is the
shipped fit (``scripts/fit_score_marginals.py`` refits it and prints the
held-out calibration).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import REPO_ROOT
from src.contest import ContestError

DEFAULT_MARGINALS_PATH = REPO_ROOT / "data" / "score_marginals.json"

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


# Standard-normal quantiles used by the calibration checks (no scipy).
Z = {0.05: -1.6449, 0.10: -1.2816, 0.25: -0.6745, 0.50: 0.0,
     0.75: 0.6745, 0.90: 1.2816, 0.95: 1.6449}


def normal_cdf(z):
    """Phi(z), vectorised. Zelen & Severo 26.2.17, |error| < 7.5e-8."""
    z = np.asarray(z, dtype=float)
    a = np.abs(z)
    t = 1.0 / (1.0 + 0.2316419 * a)
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
                + t * (-1.821255978 + t * 1.330274429))))
    tail = np.exp(-0.5 * a * a) / np.sqrt(2 * np.pi) * poly
    return np.where(z >= 0, 1.0 - tail, tail)


def lognormal_quantile(position: str, projection, q: float) -> np.ndarray:
    """The lognormal marginal's q-quantile for each projection (closed form)."""
    if q not in Z:
        raise ContestError(f"no z-score for quantile {q}; choose from {sorted(Z)}")
    mean = np.asarray(projection, dtype=float).clip(0.05, None)
    out = np.empty_like(mean)
    for i, m in enumerate(mean):
        mu, sigma = lognormal_params(m, sd_for(m, position))
        out[i] = np.exp(mu + sigma * Z[q])
    return out


@dataclass
class EmpiricalMarginals:
    """The fitted distribution of actual / projection, per position and level.

    For each position, projections are cut into ``bands`` quantile bands; in
    each band the quantile curve of actual/projection (zeros included) is
    stored on a probability grid. A player's curve is interpolated between
    the two nearest band centres so two players a point apart do not get
    different shapes. Positions with too few rows, and positions never in
    the stats feed (kickers), use the pooled ``ALL`` curves.

    The curves are **not** mean-normalised by default: they are the
    calibrated distribution of what happened given the projection, so
    ``E[score] = projection x mean ratio`` in that band -- a little under
    the projection at the top of the board, where v4 still runs ~3-5% high,
    and a little over at the bottom (T18's regression to the mean, now in
    the simulator too). ``normalise_mean=True`` scales each band's ratios to
    mean 1 if a caller wants the projection kept as the mean at the cost of
    calibration. Refit when the projection model changes: the residual
    shape belongs to the model that made the projections.
    """

    grid: np.ndarray
    centers: dict[str, np.ndarray]
    curves: dict[str, np.ndarray]          # position -> (bands, grid) ratio quantiles
    zero_share: dict[str, np.ndarray]
    counts: dict[str, np.ndarray]
    fitted_on: str = ""
    kind: str = field(default="empirical", init=False)

    @classmethod
    def fit(cls, rows: pd.DataFrame, bands: int = 5, grid_n: int = 201,
            min_rows: int = 300, min_projection: float = 0.5,
            fitted_on: str = "", normalise_mean: bool = False) -> "EmpiricalMarginals":
        for c in ("position", "projection", "actual"):
            if c not in rows.columns:
                raise ContestError(f"rows are missing {c}")
        r = rows[rows["projection"] >= min_projection].copy()
        if len(r) < min_rows:
            raise ContestError(f"only {len(r)} rows with projection >= {min_projection}; need {min_rows}")
        r["ratio"] = r["actual"].astype(float) / r["projection"].astype(float)
        grid = np.linspace(0.0, 1.0, grid_n)

        def fit_group(g: pd.DataFrame):
            n_bands = max(1, min(bands, len(g) // min_rows))
            edges = np.quantile(g["projection"], np.linspace(0, 1, n_bands + 1))
            idx = np.clip(np.searchsorted(edges, g["projection"], side="right") - 1, 0, n_bands - 1)
            centers, curves, zeros, counts = [], [], [], []
            for b in range(n_bands):
                part = g[idx == b]
                ratio = part["ratio"].to_numpy(dtype=float)
                if normalise_mean and ratio.mean() > 0:
                    ratio = ratio / ratio.mean()
                centers.append(float(np.median(part["projection"])))
                curves.append(np.quantile(ratio, grid))
                zeros.append(float((part["actual"] == 0).mean()))
                counts.append(int(len(part)))
            return (np.array(centers), np.array(curves), np.array(zeros), np.array(counts))

        centers, curves, zeros, counts = {}, {}, {}, {}
        centers["ALL"], curves["ALL"], zeros["ALL"], counts["ALL"] = fit_group(r)
        for pos, g in r.groupby("position"):
            if len(g) >= min_rows:
                centers[pos], curves[pos], zeros[pos], counts[pos] = fit_group(g)
        return cls(grid=grid, centers=centers, curves=curves, zero_share=zeros,
                   counts=counts, fitted_on=fitted_on)

    def positions(self) -> list[str]:
        return sorted(k for k in self.curves if k != "ALL")

    def curve(self, position: str, projection: float) -> np.ndarray:
        """Ratio-quantile curve for one player, interpolated between band centres."""
        key = position if position in self.curves else "ALL"
        c, k = self.centers[key], self.curves[key]
        if len(c) == 1 or projection <= c[0]:
            return k[0]
        if projection >= c[-1]:
            return k[-1]
        j = int(np.searchsorted(c, projection, side="right"))
        w = (projection - c[j - 1]) / (c[j] - c[j - 1])
        return (1 - w) * k[j - 1] + w * k[j]

    def quantile(self, position: str, projection, q) -> np.ndarray:
        """Score quantile(s) for each projection: projection x ratio quantile."""
        proj = np.asarray(projection, dtype=float)
        qs = np.asarray(q, dtype=float)
        out = np.empty(proj.shape + qs.shape) if qs.ndim else np.empty(proj.shape)
        for i, p in enumerate(proj.ravel()):
            vals = p * np.interp(qs, self.grid, self.curve(position, p))
            if qs.ndim:
                out.reshape(-1, qs.size)[i] = vals
            else:
                out.reshape(-1)[i] = vals
        return out

    def mean_ratio(self, position: str, projection: float) -> float:
        """E[actual / projection] at this level: the marginal's own mean, over the projection.

        Trapezoid over the probability grid; a plain grid mean over-weights
        the two end points, and the top one is the sample maximum.
        """
        return float(np.trapz(self.curve(position, projection), self.grid))

    def sd(self, position: str, projection: float) -> float:
        """Spread implied by the curve (uniform grid approximates the distribution)."""
        c = self.curve(position, projection)
        m = np.trapz(c, self.grid)
        return float(projection * np.sqrt(max(np.trapz((c - m) ** 2, self.grid), 0.0)))

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "fitted_on": self.fitted_on, "grid": self.grid.tolist(),
            "positions": {
                pos: {"centers": self.centers[pos].tolist(),
                      "curves": self.curves[pos].tolist(),
                      "zero_share": self.zero_share[pos].tolist(),
                      "counts": self.counts[pos].tolist()}
                for pos in self.curves
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EmpiricalMarginals":
        return cls(
            grid=np.array(d["grid"]),
            centers={p: np.array(v["centers"]) for p, v in d["positions"].items()},
            curves={p: np.array(v["curves"]) for p, v in d["positions"].items()},
            zero_share={p: np.array(v["zero_share"]) for p, v in d["positions"].items()},
            counts={p: np.array(v["counts"]) for p, v in d["positions"].items()},
            fitted_on=d.get("fitted_on", ""),
        )

    def save(self, path: Path | str = DEFAULT_MARGINALS_PATH) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict()))
        return path

    @classmethod
    def load(cls, path: Path | str = DEFAULT_MARGINALS_PATH) -> "EmpiricalMarginals":
        return cls.from_dict(json.loads(Path(path).read_text()))


def load_marginals(path: Path | str = DEFAULT_MARGINALS_PATH) -> EmpiricalMarginals | None:
    """The shipped fit, or None (-> lognormal) if it has not been produced yet."""
    path = Path(path)
    return EmpiricalMarginals.load(path) if path.exists() else None


def coverage_table(rows: pd.DataFrame, marginals: EmpiricalMarginals | None,
                   quantiles=(0.10, 0.25, 0.50, 0.75, 0.90, 0.95)) -> pd.DataFrame:
    """Share of actuals at or below each model quantile, by position (+ ALL).

    A calibrated marginal puts the share at the quantile itself. ``None``
    evaluates the lognormal, so the two can be compared on the same rows.
    """
    r = rows[rows["projection"] > 0.5]
    out = {}
    for pos, g in list(r.groupby("position")) + [("ALL", r)]:
        row = {}
        for q in quantiles:
            if marginals is None:
                qv = np.concatenate([lognormal_quantile(p, gg["projection"], q)
                                     for p, gg in g.groupby("position")])
                actual = np.concatenate([gg["actual"].to_numpy() for _, gg in g.groupby("position")])
            else:
                qv = np.concatenate([marginals.quantile(p, gg["projection"].to_numpy(), q)
                                     for p, gg in g.groupby("position")])
                actual = np.concatenate([gg["actual"].to_numpy() for _, gg in g.groupby("position")])
            row[f"<=p{int(round(q * 100))}"] = float((actual <= qv).mean())
        row["n"] = int(len(g))
        out[pos] = row
    return pd.DataFrame(out).T


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
    marginals: EmpiricalMarginals | None = None

    def __post_init__(self):
        s = self.slate.reset_index(drop=True)
        missing = [c for c in REQUIRED if c not in s.columns]
        if missing:
            raise ContestError(f"slate is missing {missing}")
        if (s["projection"] < 0).any():
            raise ContestError("projections must be non-negative")
        self.players: list[str] = s["player_id"].astype(str).tolist()
        proj = s["projection"].to_numpy(dtype=float)
        if self.marginals is not None:
            # (n, grid): each player's score-quantile curve, projection applied.
            self._curves = np.array([p * self.marginals.curve(pos, p)
                                     for p, pos in zip(proj, s["position"])])
            self._grid = self.marginals.grid
        else:
            # A zero projection cannot be lognormal; give it a hair of mean so
            # the player exists in the draw and lands near zero.
            mean = proj.clip(0.05, None)
            sd = np.array([sd_for(m, p) for m, p in zip(mean, s["position"])])
            params = [lognormal_params(m, v) for m, v in zip(mean, sd)]
            self.mu = np.array([p[0] for p in params])
            self.sigma = np.array([p[1] for p in params])
        self.R = build_correlation(s)
        self._chol = np.linalg.cholesky(self.R)

    @property
    def kind(self) -> str:
        return "empirical" if self.marginals is not None else "lognormal"

    def __call__(self, rng: np.random.Generator, trials: int) -> np.ndarray:
        z = rng.standard_normal((trials, len(self.players))) @ self._chol.T
        if self.marginals is None:
            return np.exp(self.mu + self.sigma * z)
        u = normal_cdf(z)
        out = np.empty_like(u)
        for i in range(len(self.players)):
            out[:, i] = np.interp(u[:, i], self._grid, self._curves[i])
        return out
