"""Wire a live DK pool to the validated projection.

T22 makes the role-aware model (v4, ``src.projection.RoleAware``) the live
default. It is the first model validated on the pool a Showdown entrant
actually faces (everyone who dressed, 0 for no stat line — T23): top-12
Spearman 0.485 vs the trailing average's 0.438, QB bias +0.29 vs +3.42.
Two of v3's live weaknesses are gone by construction:

- **No history.** v3 needed ``min_games`` and fell back to DK's
  ``AvgPointsPerGame`` below it (Jadarian Price: 0.0 at 48% ownership). v4
  projects a rookie thrust into RB1 at the RB1 prior.
- **Team change.** v3 had to *refuse* (T20: a bench QB projected 14.6 from
  his starts elsewhere) because a trailing average carries the old role. v4
  projects from the role the player holds on *this* team's depth chart, so a
  team-changer is projected, not refused; ``team_changed`` stays as a column
  for the CLI to mention.

Who still falls back to the pool's own ``AvgPointsPerGame`` (``proj_source``
``"avg_points"``): anyone ``src.resolve`` could not reach — kickers always,
plus a name DK spells differently. A model without roster-mode history (v3,
the baseline) keeps the pre-T22 behaviour: ``min_games`` gate, team-change
refusal, ``proj_source`` ``"v3"`` / ``"team_changed"``.

Every row keeps its provenance in ``proj_source`` and its role
(``eff_rank``, ``depth_rank``, ``injury_status``) so the output never hides
which numbers are model-backed and what role each rests on.
"""

from __future__ import annotations

import pandas as pd

from src.backtest import history
from src.projection import CalibratedAverage, RoleAware

ROLE_MODEL_SOURCE = "v4"
LEGACY_MODEL_SOURCE = "v3"
# History has no kickers; nothing to project them from.
UNPROJECTABLE_POSITIONS = ("K",)


def _role_aware_overlay(conn, out: pd.DataFrame, season: int, week: int, model) -> pd.DataFrame:
    resolvable = out["gsis_id"].notna() & ~out["position"].isin(UNPROJECTABLE_POSITIONS)
    if not resolvable.any():
        return out
    past = history.as_of(conn, season, week, pool="roster")
    if past.empty:
        return out

    slate = pd.DataFrame({
        "player_id": out.loc[resolvable, "gsis_id"].values,
        "team": out.loc[resolvable, "team"].values,
        "position": out.loc[resolvable, "position"].values,
    })
    slate = history.attach_roles(conn, slate, season, week)
    projected = model.project(past, slate).set_index("player_id")["projection"]

    ids = out.loc[resolvable, "gsis_id"]
    out.loc[resolvable, "projection"] = ids.map(projected).values
    out.loc[resolvable, "proj_source"] = ROLE_MODEL_SOURCE
    roles = slate.set_index("player_id")
    for col in ("eff_rank", "depth_rank", "injury_status"):
        out.loc[resolvable, col] = ids.map(roles[col]).values
    return out


def _legacy_overlay(conn, out: pd.DataFrame, season: int, week: int, min_games: int, model) -> pd.DataFrame:
    if "team_changed" in out.columns:
        changed = out["team_changed"].fillna(False).astype(bool) & out["gsis_id"].notna()
        out.loc[changed, "proj_source"] = "team_changed"
    else:
        changed = pd.Series(False, index=out.index)

    resolvable = out[out["gsis_id"].notna() & ~changed]
    if resolvable.empty:
        return out
    past = history.as_of(conn, season, week)
    games_played = past.groupby("player_id").size()
    eligible_ids = set(resolvable["gsis_id"]) & set(games_played[games_played >= min_games].index)
    if not eligible_ids:
        return out
    slate = pd.DataFrame({"player_id": list(eligible_ids)}).merge(
        past[["player_id", "position"]].drop_duplicates("player_id"), on="player_id", how="left")
    projected = model.project(past, slate).set_index("player_id")["projection"]
    mask = out["gsis_id"].isin(eligible_ids)
    out.loc[mask, "projection"] = out.loc[mask, "gsis_id"].map(projected)
    out.loc[mask, "proj_source"] = LEGACY_MODEL_SOURCE
    return out


def project_live_pool(
    conn,
    resolved_pool: pd.DataFrame,
    season: int,
    week: int,
    min_games: int = 3,
    model=None,
) -> pd.DataFrame:
    """Overlay model projections onto a resolved pool. Returns pool + proj_source (+ role).

    ``season, week`` is the slate's own week: history strictly before it is
    what the model sees, and the week's depth chart and injury report are
    the roles. Default model is v4 (``RoleAware``); pass a ``CalibratedAverage``
    for the pre-T22 v3 path.
    """
    model = model or RoleAware()
    out = resolved_pool.copy()
    out["proj_source"] = "avg_points"
    for col in ("eff_rank", "depth_rank", "injury_status"):
        out[col] = pd.Series([None] * len(out), index=out.index, dtype=object)
    if out.empty:
        return out

    if getattr(model, "history_pool", "played") == "roster":
        return _role_aware_overlay(conn, out, season, week, model)
    return _legacy_overlay(conn, out, season, week, min_games, model)
