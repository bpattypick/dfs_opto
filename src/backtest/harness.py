"""The replay loop (spec §7): project each historical week, score against truth.

For every (season, week) in range: hand the model a time-boxed history and a
scoreless slate, take its projections, join to the actual dk_points the model
never saw, accumulate. Metrics per spec §7 — MAE, RMSE, Spearman rank
correlation — by position, by season, overall. Results persist to
``backtest_runs`` keyed by run_id so a model version's numbers are reproducible.

Evaluation set: players with at least ``min_games`` of prior history who took a
snap in week W. The first keeps every model on the same footing (a trailing
average is undefined below it); the second is the spec's stated approximation
for pre-lock inactives, which no historical backtest can know exactly. Both are
limitations, stated rather than hidden.
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
            f"model {self.model}  run {self.run_id[:8]}  "
            f"{m['n']:,} player-weeks over {m['weeks']} weeks",
            f"  overall  MAE {m['mae']:.2f}  RMSE {m['rmse']:.2f}  "
            f"Spearman {m['spearman']:.3f}  bias {m['bias']:+.2f}",
            "  by position:",
        ]
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


def evaluate_week(
    conn: sqlite3.Connection,
    model: ProjectionModel,
    season: int,
    week: int,
    min_games: int = 3,
) -> pd.DataFrame:
    """One week of the replay. Returns rows the model was evaluated on."""
    past = history.as_of(conn, season, week)
    if past.empty:
        raise BacktestError(f"no history before {season} week {week}")
    slate = history.slate(conn, season, week)
    if slate.empty:
        raise BacktestError(f"no stat rows for {season} week {week}")
    assert "dk_points" not in slate.columns  # the structural guarantee

    projected = model.project(past, slate)
    missing = set(projected.columns) ^ {"player_id", "projection"}
    if missing:
        raise BacktestError(f"{model.name} returned columns {list(projected.columns)}")

    games_played = past.groupby("player_id").size().rename("n_games")
    truth = history.actuals(conn, season, week)

    rows = (slate[["player_id", "position", "team"]]
            .merge(projected, on="player_id", how="left")
            .merge(truth, on="player_id", how="inner")
            .merge(games_played, on="player_id", how="left"))
    rows["n_games"] = rows["n_games"].fillna(0)
    rows = rows.rename(columns={"dk_points": "actual"})

    eligible = (rows["n_games"] >= min_games) & rows["projection"].notna()
    # Spec §7's inactive approximation: a recorded 0 snaps means they did not
    # play, which no pre-lock projection could know. Unknown snaps are kept.
    played = rows["snaps"].isna() | (rows["snaps"] > 0)
    rows = rows[eligible & played].copy()
    rows["season"] = season
    rows["week"] = week
    return rows[["season", "week", "player_id", "position", "team",
                 "n_games", "projection", "actual"]]


def run(
    conn: sqlite3.Connection,
    model: ProjectionModel,
    seasons: Sequence[int],
    min_games: int = 3,
    weeks: Sequence[int] | None = None,
    persist: bool = True,
) -> BacktestResult:
    """Replay every week in ``seasons`` and score the model."""
    pairs = history.weeks_available(conn, seasons)
    if weeks is not None:
        pairs = [(s, w) for s, w in pairs if w in set(weeks)]
    if not pairs:
        raise BacktestError(f"no weeks to evaluate in seasons {list(seasons)}")

    frames = []
    for season, week in pairs:
        try:
            frames.append(evaluate_week(conn, model, season, week, min_games))
        except BacktestError as exc:
            if "no history before" in str(exc):
                continue        # the very first week has nothing to learn from
            raise
    rows = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if rows.empty:
        raise BacktestError("no evaluable player-weeks — is the database loaded?")

    metrics = _metrics(rows)
    metrics["weeks"] = int(rows[["season", "week"]].drop_duplicates().shape[0])
    metrics["by_position"] = {p: _metrics(g) for p, g in rows.groupby("position")}
    metrics["by_season"] = {int(s): _metrics(g) for s, g in rows.groupby("season")}

    config = {"model": model.name, "params": {k: v for k, v in vars(model).items()
                                                if k != "name"},
              "seasons": [int(s) for s in seasons], "min_games": min_games,
              "weeks": list(weeks) if weeks else None}
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
    lines = [f"{'model':<16}{'n':>9}{'MAE':>8}{'RMSE':>8}{'Spearman':>10}{'bias':>8}"]
    for r in results:
        m = r.metrics
        lines.append(f"{r.model:<16}{m['n']:>9,}{m['mae']:>8.2f}{m['rmse']:>8.2f}"
                     f"{m['spearman']:>10.3f}{m['bias']:>+8.2f}")
    return "\n".join(lines)
