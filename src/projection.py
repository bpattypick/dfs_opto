"""Projection models — the layer that was missing.

Two slates of real standings showed the dominant error in the pipeline is the
projection, not the field: ``AvgPointsPerGame`` overshot by 5.1 points per
owned player, -11 to -17 on the top plays, because a prior-season average
selects for players who sustained a high rate — exactly who a new season pulls
back (docs/data-sources.md, docs/decisions.md).

Every model here has the same shape so the backtest harness can compare them:

    model.project(history, slate) -> DataFrame[player_id, projection]

``history`` is strictly before the target week and ``slate`` carries only
pre-lock facts (see src/backtest/history.py). A model that respects that
signature cannot look ahead.

``PriorAverage`` reproduces what DK's AvgPointsPerGame does, so it is the
baseline every other model has to beat on held-out seasons. ``ShrunkVegas`` is
v1: the same average, shrunk toward a positional mean by how many games it
rests on, then scaled by the game's Vegas implied team total.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
import pandas as pd

OUTPUT_COLUMNS = ("player_id", "projection")


class ProjectionError(RuntimeError):
    """A projection that cannot be produced as specified."""


class ProjectionModel(Protocol):
    name: str

    def project(self, history: pd.DataFrame, slate: pd.DataFrame) -> pd.DataFrame: ...


def _check_inputs(history: pd.DataFrame, slate: pd.DataFrame) -> None:
    if "dk_points" in slate.columns:
        # The harness never passes this. A model seeing it is a leak.
        raise ProjectionError("slate carries dk_points — that is week W's answer key")
    for col in ("player_id", "position"):
        if col not in slate.columns:
            raise ProjectionError(f"slate is missing {col}")
    if history.empty:
        raise ProjectionError("history is empty — nothing to project from")


def _trailing(history: pd.DataFrame, window: int) -> pd.DataFrame:
    """Per player: mean dk_points over their last ``window`` games, and n."""
    ordered = history.sort_values(["player_id", "season", "week"])
    last = ordered.groupby("player_id", sort=False).tail(window)
    agg = last.groupby("player_id")["dk_points"].agg(["mean", "count"])
    return agg.rename(columns={"mean": "trailing_mean", "count": "n_games"})


@dataclass
class PriorAverage:
    """What DK's AvgPointsPerGame is: a per-game average over recent games.

    ``window`` of 17 approximates "last season". Players with no history get
    no projection (NaN) — the baseline genuinely has nothing to say about a
    rookie, and pretending otherwise would flatter it.
    """

    window: int = 17
    name: str = "prior_average"

    def project(self, history: pd.DataFrame, slate: pd.DataFrame) -> pd.DataFrame:
        _check_inputs(history, slate)
        trailing = _trailing(history, self.window)
        out = slate[["player_id"]].merge(trailing, on="player_id", how="left")
        return pd.DataFrame({
            "player_id": out["player_id"],
            "projection": out["trailing_mean"],
        })


@dataclass
class ShrunkVegas:
    """v1: trailing average, shrunk toward the positional mean, scaled by Vegas.

    Shrinkage weight is n / (n + k): a player with many games keeps their own
    average, one with few is pulled toward what a typical player at the
    position scores. This is the direct fix for the measured -5.1 bias — the
    top of the board is where averages rest on the fewest, luckiest games.

    Vegas scaling multiplies by implied_total / league-mean implied total, so a
    player in a 52-point game projects higher than the same player in a 40-point
    game. Free, published pre-lock, and the one input that knows about tonight
    rather than last year.

    A player with no history projects to the positional mean — the honest prior
    for a rookie, and still Vegas-scaled.
    """

    window: int = 17
    k: float = 4.0                 # games of "prior evidence" the shrinkage carries
    vegas: bool = True
    name: str = "shrunk_vegas"

    def project(self, history: pd.DataFrame, slate: pd.DataFrame) -> pd.DataFrame:
        _check_inputs(history, slate)
        if self.k < 0:
            raise ProjectionError("k must be non-negative")

        trailing = _trailing(history, self.window)
        # Positional prior from the same time-boxed history.
        pos_mean = (history.groupby("position")["dk_points"].mean()
                    .rename("position_mean"))

        out = (slate[["player_id", "position"] +
                     (["implied_total"] if "implied_total" in slate else [])]
               .merge(trailing, on="player_id", how="left")
               .merge(pos_mean, on="position", how="left"))
        out["n_games"] = out["n_games"].fillna(0)
        out["trailing_mean"] = out["trailing_mean"].fillna(out["position_mean"])
        out["position_mean"] = out["position_mean"].fillna(0.0)

        w = out["n_games"] / (out["n_games"] + self.k)
        proj = w * out["trailing_mean"] + (1 - w) * out["position_mean"]

        if self.vegas and "implied_total" in out:
            league = out["implied_total"].mean()
            scale = (out["implied_total"] / league).fillna(1.0) if league else 1.0
            proj = proj * scale

        return pd.DataFrame({"player_id": out["player_id"], "projection": proj})


MODELS: dict[str, type] = {
    PriorAverage.name: PriorAverage,
    ShrunkVegas.name: ShrunkVegas,
}
