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

A player `src.resolve` flags as having changed teams since our last ingested
data is a separate, harder case and is handled first, unconditionally: their
trailing average is a snapshot of the *role* they held at their old team, and
nothing says that role carries over. This is not a theoretical risk — on the
first live slate this ran on, it silently gave a bench QB a confident
14.6-point projection built entirely from his 2025 starts at a different team.
Such a player never gets v3, whether or not they clear ``min_games``; they
keep the pool's own projection and are tagged ``proj_source="team_changed"``,
a distinct value from plain ``"avg_points"`` so the CLI can flag them loudly
rather than let them blend in with an ordinary unresolved player.

Every row's provenance is kept (``proj_source``: "v3", "avg_points", or
"team_changed") so the output never hides which projections are backed by the
validated model, which are the same weak input that has been wrong before, and
which are a specific, known-dangerous case that needs a human's own knowledge
of the depth chart — exactly what caught this the first time.
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

    if "team_changed" in out.columns:
        changed = out["team_changed"].fillna(False) & out["gsis_id"].notna()
        out.loc[changed, "proj_source"] = "team_changed"
    else:
        changed = pd.Series(False, index=out.index)

    resolvable = out[out["gsis_id"].notna() & ~changed]
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
