"""Building a Showdown player pool from DK salary data.

Task T12. The T4/T5 runs filtered unavailable players by hand; this is that step
made explicit and testable, because a pool that still contains OUT players will
happily build lineups around people who never take the field — and the optimizer
loves them, since they carry a real projection at a discounted salary.

The DK ``Status`` field is also the free late-swap signal roadmap Step 4 wants:
it ships in every export, no news feed required. Polling the export for status
changes before lock is Step 4's cheapest version.
"""

from __future__ import annotations

import logging

import pandas as pd

from src.ingest.dk_salaries import parse_dk_export

log = logging.getLogger(__name__)

# DK will not score these, so they must never enter a pool.
UNAVAILABLE = ("OUT", "IR", "IR-R", "SUSP", "NA")

# Questionable and doubtful players usually do play, and excluding them would
# throw away real leverage — they are often under-owned. They stay in the pool;
# `questionable()` exists so a caller can look at them deliberately.
QUESTIONABLE = ("Q", "D", "GTD")

POOL_COLUMNS = ("player_id", "name", "team", "salary", "projection")


class PoolError(RuntimeError):
    """A pool that cannot be built as specified."""


def build_pool(
    salaries: pd.DataFrame,
    projection_column: str = "avg_points",
    id_column: str = "source_id",
    include_unavailable: bool = False,
) -> pd.DataFrame:
    """Canonical pool frame from parsed DK salary rows.

    Expects one row per player (see ``dk_salaries.collapse_captain_rows`` — a
    Showdown export's CPT rows must already be folded away).

    ``id_column`` defaults to DK's own id, which is what a single-slate
    simulation needs. Pass ``player_id`` once the crosswalk has resolved GSIS
    ids, which is what anything joining across sources needs.
    """
    required = {"dk_name", "dk_salary", "team", id_column, projection_column}
    missing = sorted(required - set(salaries.columns))
    if missing:
        raise PoolError(f"salary rows are missing columns {missing}")
    if salaries.empty:
        raise PoolError("no salary rows")

    frame = salaries.copy()
    status = frame["status"].fillna("").astype(str).str.upper() if "status" in frame else ""
    if not include_unavailable:
        unavailable = frame[pd.Series(status, index=frame.index).isin(UNAVAILABLE)]
        if len(unavailable):
            log.info("excluding %d unavailable players: %s", len(unavailable),
                     ", ".join(sorted(unavailable["dk_name"])[:8]))
        frame = frame[~pd.Series(status, index=frame.index).isin(UNAVAILABLE)]
        if frame.empty:
            raise PoolError("every player in this slate is OUT or IR")

    if frame[id_column].isna().any() or (frame[id_column].astype(str) == "").any():
        # An unresolved id would collide with other blanks and silently merge
        # two players into one pool entry.
        blank = frame.loc[frame[id_column].isna(), "dk_name"].tolist()
        raise PoolError(f"missing {id_column} for {blank[:5] or 'some players'}")

    pool = pd.DataFrame({
        "player_id": frame[id_column].astype(str),
        "name": frame["dk_name"].astype(str).str.strip(),
        "team": frame["team"].astype(str),
        "salary": frame["dk_salary"].astype(float),
        "projection": pd.to_numeric(frame[projection_column], errors="coerce"),
    }).reset_index(drop=True)

    if pool["projection"].isna().any():
        bad = pool.loc[pool["projection"].isna(), "name"].tolist()
        raise PoolError(
            f"no {projection_column} for {bad[:5]} — a pool with missing "
            "projections silently treats them as unrosterable"
        )
    pool["projection"] = pool["projection"].clip(lower=0.0)

    if pool["player_id"].duplicated().any():
        dupes = pool["player_id"][pool["player_id"].duplicated()].unique().tolist()
        raise PoolError(f"duplicate player_id in pool: {dupes[:5]}")
    return pool


def questionable(salaries: pd.DataFrame) -> pd.DataFrame:
    """Players DK flags as questionable/doubtful — in the pool, worth a look.

    These are where late-swap and leverage overlap: they suppress ownership
    while usually still playing.
    """
    if "status" not in salaries.columns:
        return salaries.iloc[0:0]
    status = salaries["status"].fillna("").astype(str).str.upper()
    return salaries[status.isin(QUESTIONABLE)]


def status_changes(before: pd.DataFrame, after: pd.DataFrame) -> pd.DataFrame:
    """Players whose DK status changed between two pulls of the same slate.

    The cheapest version of roadmap Step 4: poll the export before lock and
    diff. Returns dk_name with the old and new status.
    """
    for frame, label in ((before, "before"), (after, "after")):
        if not {"dk_name", "status"} <= set(frame.columns):
            raise PoolError(f"{label} frame needs dk_name and status columns")

    old = before.set_index("dk_name")["status"].fillna("").astype(str).str.upper()
    new = after.set_index("dk_name")["status"].fillna("").astype(str).str.upper()
    shared = old.index.intersection(new.index)
    changed = shared[old[shared].to_numpy() != new[shared].to_numpy()]
    return pd.DataFrame({
        "dk_name": changed,
        "was": old[changed].to_numpy(),
        "now": new[changed].to_numpy(),
    }).reset_index(drop=True)


def from_export(
    path,
    projection_column: str = "avg_points",
    include_unavailable: bool = False,
) -> pd.DataFrame:
    """Pool straight from a DK Showdown export, bypassing the database.

    This is the only path available for a live slate. ``dk_salaries.load_file``
    resolves names through the crosswalk, whose reference is built from
    ``player_week_stats`` for the slate's own week — rows that do not exist
    until the games have been played. Fine for backtesting a historical slate,
    impossible for one that locks tonight (see T14).

    So the ids here are DK's own, not canonical GSIS ids. That is sufficient for
    simulating a single slate, where every component keys off the same pool, and
    insufficient for anything joining across sources or weeks.
    """
    return build_pool(
        parse_dk_export(path),
        projection_column=projection_column,
        id_column="source_id",
        include_unavailable=include_unavailable,
    )
