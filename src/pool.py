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


OVERRIDE_COLUMNS = ("slate_id", "dk_name", "projection", "reason")


def load_overrides(path, slate_id: str) -> pd.DataFrame:
    """Hand-set projections for one slate, from a tracked CSV.

    `AvgPointsPerGame` is blind to role changes — it rated Seattle's starting
    back 0.0 on a slate where the field owned him 48% (docs/data-sources.md).
    No amount of field modelling recovers a player the projection scores at
    zero, so the owner's read has to enter somewhere, and it enters here:
    committed, reviewable, and attributable to a commit like any other input.

    ``reason`` is required. An override without one is indistinguishable from a
    typo six weeks later, and the ledger's whole premise is that inputs stay
    recoverable.
    """
    frame = pd.read_csv(path)
    missing = sorted(set(OVERRIDE_COLUMNS) - set(frame.columns))
    if missing:
        raise PoolError(f"{path}: override file is missing columns {missing}")
    frame = frame[frame["slate_id"].astype(str) == slate_id]
    blank = frame[frame["reason"].isna() | (frame["reason"].astype(str).str.strip() == "")]
    if len(blank):
        raise PoolError(
            f"{path}: overrides without a reason: {blank['dk_name'].tolist()}"
        )
    return frame.reset_index(drop=True)


def apply_overrides(pool: pd.DataFrame, overrides: pd.DataFrame) -> pd.DataFrame:
    """Replace projections for the named players. Fails loudly on a bad name.

    A misspelled name would otherwise silently do nothing, leaving the caller
    believing an override took effect — which is worse than no override, because
    the lineup then looks reviewed when it is not.
    """
    if overrides is None or overrides.empty:
        return pool
    out = pool.copy()
    known = set(out["name"])
    unknown = [n for n in overrides["dk_name"] if n not in known]
    if unknown:
        raise PoolError(
            f"override names not in the pool: {unknown}. Check the spelling "
            "against the DK export, or whether the player is OUT/IR."
        )
    for _, row in overrides.iterrows():
        mask = out["name"] == row["dk_name"]
        was = out.loc[mask, "projection"].iloc[0]
        out.loc[mask, "projection"] = float(row["projection"])
        log.info("override %s: %.1f -> %.1f (%s)", row["dk_name"], was,
                 float(row["projection"]), row["reason"])
    return out
