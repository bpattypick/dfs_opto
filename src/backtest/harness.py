"""The replay loop (spec §7): project each historical week, score against truth.

For every (season, week) in range: hand the model a time-boxed history and a
scoreless slate, take its projections, join to the actual dk_points the model
never saw, accumulate. Metrics per spec §7 — MAE, RMSE, Spearman rank
correlation — by position, by season, overall. Results persist to
``backtest_runs`` keyed by run_id so a model version's numbers are reproducible.

Two pools (T23), chosen with ``pool=``:

``"played"`` — players with at least ``min_games`` of prior history who took a
snap in week W. This was the only pool until T23, and it has a blind spot the
live slates found the hard way: by construction it never asks "does this
player play at all?", because everyone in it did. Kept as the like-for-like
comparison with earlier runs.

``"roster"`` — every skill player who *dressed* for week W (roster status
ACT) plus both DSTs, the pool a Showdown entrant faces at lock. A dressed
player with no stat line scored 0, and the model that projected him his
trailing average is wrong by all of it. In 2024 that is 22% of the dressed
skill players, three quarters of whom had enough history to be projected.
Coverage numbers report who the model could not project at all and how much
scoring they carried, so a model that only handles the easy players does not
look better than it is.

Either way, ``min_games`` keeps every model on the same footing (a trailing
average is undefined below it); a limitation stated rather than hidden.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

import numpy as np
import pandas as pd

from src import db
from src.backtest import history
from src.projection import ProjectionModel, ProjectionError

SHOWDOWN_TOP_N = 12   # the projected top-12 per game is what a Showdown lineup is built from


class BacktestError(RuntimeError):
    """A backtest that cannot run as specified."""


@dataclass
class BacktestResult:
    run_id: str
    model: str
    config: dict
    rows: pd.DataFrame            # one row per (season, week, player) evaluated
    metrics: dict = field(default_factory=dict)

    def summary(self) -> str:
        m = self.metrics
        lines = [
            f"model {self.model}  run {self.run_id[:8]}  pool={m.get('pool', 'played')}  "
            f"{m['n']:,} player-weeks over {m['weeks']} weeks",
            f"  overall  MAE {m['mae']:.2f}  RMSE {m['rmse']:.2f}  "
            f"Spearman {m['spearman']:.3f}  bias {m['bias']:+.2f}",
        ]
        cov = m.get("coverage")
        if cov:
            lines.append(
                f"  pool     {cov['pool_n']:,} in pool, {cov['evaluated_n']:,} evaluated, "
                f"{cov['excluded_n']:,} excluded (no projection) carrying "
                f"{cov['points_unseen_share']:.1%} of actual points"
            )
            lines.append(
                f"  zeros    {cov['zero_rate']:.1%} of evaluated scored 0; the model gave them "
                f"{cov['zero_mean_projection']:.2f} on average"
            )
        top = m.get("showdown_top12")
        if top:
            lines.append(
                f"  top-12/game  n={top['n']:,}  MAE {top['mae']:.2f}  "
                f"Spearman {top['spearman']:.3f}  bias {top['bias']:+.2f}"
            )
        lines.append("  by position:")
        for pos, pm in sorted(m["by_position"].items()):
            lines.append(f"    {pos:<4} n={pm['n']:>6,}  MAE {pm['mae']:.2f}  "
                         f"Spearman {pm['spearman']:.3f}  bias {pm['bias']:+.2f}")
        lines.append("  by season:")
        for season, sm in sorted(m["by_season"].items()):
            lines.append(f"    {season}  n={sm['n']:>6,}  MAE {sm['mae']:.2f}  "
                         f"Spearman {sm['spearman']:.3f}  bias {sm['bias']:+.2f}")
        return "\n".join(lines)


def _metrics(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"n": 0, "mae": float("nan"), "rmse": float("nan"),
                "spearman": float("nan"), "bias": float("nan")}
    err = frame["projection"] - frame["actual"]
    # Spearman = Pearson on ranks. Computed directly so we do not pull in scipy
    # for one line.
    sp = (frame["projection"].rank().corr(frame["actual"].rank())
          if len(frame) > 2 else float("nan"))
    return {
        "n": int(len(frame)),
        "mae": float(err.abs().mean()),
        "rmse": float(np.sqrt((err ** 2).mean())),
        "spearman": float(sp) if pd.notna(sp) else float("nan"),
        # Positive = projections run high. The measured live-slate bias was -5.1
        # in (actual - projected) terms, i.e. +5.1 here.
        "bias": float(err.mean()),
    }


def _coverage(pool_rows: pd.DataFrame) -> dict:
    """Who the model could not score, and what the scored players did."""
    ev = pool_rows["evaluated"]
    scored = pool_rows[ev]
    total = pool_rows["actual"].sum()
    zeros = scored[scored["actual"] == 0]
    return {
        "pool_n": int(len(pool_rows)),
        "evaluated_n": int(ev.sum()),
        "excluded_n": int((~ev).sum()),
        "points_unseen_share": float(pool_rows.loc[~ev, "actual"].sum() / total) if total else 0.0,
        "zero_rate": float((scored["actual"] == 0).mean()) if len(scored) else float("nan"),
        "zero_mean_projection": float(zeros["projection"].mean()) if len(zeros) else float("nan"),
    }


def _showdown_metrics(rows: pd.DataFrame) -> dict:
    """The projected top-N per game — the slice a Showdown lineup is chosen from."""
    if rows.empty or rows["game_id"].isna().all():
        return {}
    rank = rows.groupby("game_id")["projection"].rank(ascending=False, method="first")
    return _metrics(rows[rank <= SHOWDOWN_TOP_N])


def _week_rows(
    conn: sqlite3.Connection,
    model: ProjectionModel,
    season: int,
    week: int,
    min_games: int,
    pool: str,
) -> pd.DataFrame:
    """Every pool row for the week, flagged ``evaluated`` where the model is scored."""
    past = history.as_of(conn, season, week)
    if past.empty:
        raise BacktestError(f"no history before {season} week {week}")
    slate = history.slate(conn, season, week, pool=pool)
    if slate.empty:
        what = "stat rows" if pool == "played" else "roster rows"
        raise BacktestError(f"no {what} for {season} week {week}")
    assert "dk_points" not in slate.columns  # the structural guarantee

    projected = model.project(past, slate)
    missing = set(projected.columns) ^ {"player_id", "projection"}
    if missing:
        raise BacktestError(f"{model.name} returned columns {list(projected.columns)}")

    games_played = past.groupby("player_id").size().rename("n_games")
    truth = history.actuals(conn, season, week)

    # played: the pool *is* the stat rows, so an inner join loses nothing.
    # roster: a dressed player with no stat line is kept — he scored 0.
    rows = (slate[["player_id", "position", "team", "game_id"]]
            .merge(projected, on="player_id", how="left")
            .merge(truth, on="player_id", how="inner" if pool == "played" else "left")
            .merge(games_played, on="player_id", how="left"))
    rows["n_games"] = rows["n_games"].fillna(0)
    if pool == "roster":
        rows["dk_points"] = rows["dk_points"].fillna(0.0)
    rows = rows.rename(columns={"dk_points": "actual"})

    eligible = (rows["n_games"] >= min_games) & rows["projection"].notna()
    if pool == "played":
        # Spec §7's inactive approximation: a recorded 0 snaps means they did
        # not play, which no pre-lock projection could know. Unknown snaps are
        # kept. The roster pool keeps them all — that is its point.
        eligible &= rows["snaps"].isna() | (rows["snaps"] > 0)
    rows["evaluated"] = eligible
    rows["season"] = season
    rows["week"] = week
    return rows[["season", "week", "player_id", "position", "team", "game_id",
                 "n_games", "projection", "actual", "evaluated"]]


def evaluate_week(
    conn: sqlite3.Connection,
    model: ProjectionModel,
    season: int,
    week: int,
    min_games: int = 3,
    pool: str = "played",
) -> pd.DataFrame:
    """One week of the replay. Returns the rows the model was evaluated on."""
    rows = _week_rows(conn, model, season, week, min_games, pool)
    return rows[rows["evaluated"]].drop(columns="evaluated").reset_index(drop=True)


def run(
    conn: sqlite3.Connection,
    model: ProjectionModel,
    seasons: Sequence[int],
    min_games: int = 3,
    weeks: Sequence[int] | None = None,
    persist: bool = True,
    pool: str = "played",
) -> BacktestResult:
    """Replay every week in ``seasons`` and score the model."""
    if pool not in history.POOLS:
        raise BacktestError(f"unknown pool {pool!r}; expected one of {history.POOLS}")
    pairs = history.weeks_available(conn, seasons)
    if weeks is not None:
        pairs = [(s, w) for s, w in pairs if w in set(weeks)]
    if not pairs:
        raise BacktestError(f"no weeks to evaluate in seasons {list(seasons)}")

    frames = []
    for season, week in pairs:
        try:
            frames.append(_week_rows(conn, model, season, week, min_games, pool))
        except BacktestError as exc:
            if "no history before" in str(exc):
                continue        # the very first week has nothing to learn from
            raise
    pool_rows = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    rows = pool_rows[pool_rows["evaluated"]].drop(columns="evaluated").reset_index(drop=True) \
        if not pool_rows.empty else pd.DataFrame()
    if rows.empty:
        raise BacktestError("no evaluable player-weeks — is the database loaded?")

    metrics = _metrics(rows)
    metrics["pool"] = pool
    metrics["weeks"] = int(rows[["season", "week"]].drop_duplicates().shape[0])
    metrics["coverage"] = _coverage(pool_rows)
    metrics["showdown_top12"] = _showdown_metrics(rows)
    metrics["by_position"] = {p: _metrics(g) for p, g in rows.groupby("position")}
    metrics["by_season"] = {int(s): _metrics(g) for s, g in rows.groupby("season")}

    config = {"model": model.name, "params": {k: v for k, v in vars(model).items()
                                                if k != "name"},
              "seasons": [int(s) for s in seasons], "min_games": min_games,
              "weeks": list(weeks) if weeks else None, "pool": pool}
    result = BacktestResult(run_id=uuid.uuid4().hex, model=model.name,
                            config=config, rows=rows, metrics=metrics)
    if persist:
        db.upsert(conn, "backtest_runs", [{
            "run_id": result.run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config_json": json.dumps(config),
            "metrics_json": json.dumps(metrics),
        }])
        conn.commit()
    return result


def compare(results: Sequence[BacktestResult]) -> str:
    """Side-by-side table for the ship/no-ship decision."""
    lines = [f"{'model':<24}{'pool':<8}{'n':>9}{'MAE':>8}{'RMSE':>8}{'Spearman':>10}{'bias':>8}"
             f"{'top12 rho':>11}"]
    for r in results:
        m = r.metrics
        top = m.get("showdown_top12") or {}
        top_rho = f"{top['spearman']:.3f}" if top else "-"
        lines.append(f"{r.model:<24}{m.get('pool', 'played'):<8}{m['n']:>9,}{m['mae']:>8.2f}"
                     f"{m['rmse']:>8.2f}{m['spearman']:>10.3f}{m['bias']:>+8.2f}{top_rho:>11}")
    return "\n".join(lines)
