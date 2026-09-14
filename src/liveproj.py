"""Wire a live DK pool to the validated projection: v3 where possible, the DK
average where not.

T16/T18/T14, assembled. `CalibratedAverage(per_position=True)` (v3) is
validated on 2020-2025 to remove the top-of-board bias that decided SF@LAR
(docs/data-sources.md, "v3 by decile"). It needs a GSIS id and enough games of
history to run on. `src.resolve` gets most of a live pool there; whoever it
can't reach — kickers, practice-squad depth, this week's inactive-driven
role change — falls back to the pool's own AvgPointsPerGame rather than
erroring, because a live slate always has a few and the fallback is exactly
what CLAUDE.md H7 already accepted as the interim source.

Every row's provenance is kept (``proj_source``: "v3" or "avg_points") so the
output never hides which projections are backed by the validated model and
which are the same weak input that has been wrong twice already.
"""

from __future__ import annotations

import pandas as pd

from src.backtest import history
from src.projection import CalibratedAverage


def project_live_pool(
    conn,
    resolved_pool: pd.DataFrame,
    season: int,
    week: int,
    min_games: int = 3,
    model: CalibratedAverage | None = None,
) -> pd.DataFrame:
    """Overlay v3 projections onto a resolved pool. Returns pool + proj_source.

    ``season, week`` should be beyond everything loaded (e.g. one past the
    last ingested season) so ``history.as_of`` pulls the model's full history
    without excluding anything — there is no leakage risk here since nothing
    from the target slate itself is in the database yet.
    """
    model = model or CalibratedAverage(per_position=True)
    out = resolved_pool.copy()
    out["proj_source"] = "avg_points"

    resolvable = out[out["gsis_id"].notna()]
    if resolvable.empty:
        return out

    past = history.as_of(conn, season, week)
    games_played = past.groupby("player_id").size()
    eligible_ids = set(resolvable["gsis_id"]) & set(
        games_played[games_played >= min_games].index
    )
    if not eligible_ids:
        return out

    slate = pd.DataFrame({
        "player_id": list(eligible_ids),
    }).merge(
        past[["player_id", "position"]].drop_duplicates("player_id"),
        on="player_id", how="left",
    )
    projected = model.project(past, slate).set_index("player_id")["projection"]

    mask = out["gsis_id"].isin(eligible_ids)
    out.loc[mask, "projection"] = out.loc[mask, "gsis_id"].map(projected)
    out.loc[mask, "proj_source"] = "v3"
    return out
