"""Read DK contest standings CSVs as lineups -- the calibration ground truth.

The minimal parser T15 needs: one ``Lineup`` per entry, by the names DK
prints, mapped to pool ids where the caller supplies a pool. T3 (the
standings ingester, blocked on H2) owns persistence, the crosswalk and the
ownership table; this stays a reader.

DK's Showdown lineup string is ``CPT <name> FLEX <name> FLEX <name> ...``,
with a DST spelled as the team nickname and an occasional trailing double
space. The right-hand columns (Player, Roster Position, %Drafted, FPTS) are
DK's own ownership summary and are read separately.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from src.ingest.crosswalk import normalize_name
from src.showdown import Lineup

_SLOT = re.compile(r"\b(CPT|FLEX)\s+")


class StandingsError(RuntimeError):
    """A standings file that cannot be read as specified."""


def parse_lineup_string(text: str) -> tuple[str, tuple[str, ...]]:
    """``'CPT A FLEX B FLEX C ...'`` -> (captain name, flex names)."""
    parts = [p.strip() for p in _SLOT.split(str(text)) if p and p.strip()]
    # split() yields [..., 'CPT', 'A', 'FLEX', 'B', ...]; pair them up.
    slots = list(zip(parts[0::2], parts[1::2]))
    captains = [n for s, n in slots if s == "CPT"]
    flex = tuple(n for s, n in slots if s == "FLEX")
    if len(captains) != 1 or len(flex) != 5:
        raise StandingsError(f"not a Showdown lineup: {text!r}")
    return captains[0], flex


def read_standings(path: str | Path) -> pd.DataFrame:
    """One row per entry: rank, entry_id, points, captain, flex (names)."""
    raw = pd.read_csv(path, encoding="utf-8-sig")
    for col in ("Rank", "EntryId", "Points", "Lineup"):
        if col not in raw.columns:
            raise StandingsError(f"{Path(path).name} is missing column {col!r}")
    entries = raw[raw["Lineup"].notna()].copy()
    parsed = [parse_lineup_string(s) for s in entries["Lineup"]]
    return pd.DataFrame({
        "rank": entries["Rank"].astype(int).to_numpy(),
        "entry_id": entries["EntryId"].astype(str).to_numpy(),
        "points": pd.to_numeric(entries["Points"], errors="coerce").to_numpy(),
        "captain": [c for c, _ in parsed],
        "flex": [f for _, f in parsed],
    })


def dk_ownership(path: str | Path) -> pd.DataFrame:
    """DK's own per-player %Drafted column: name, roster_position, drafted (fraction)."""
    raw = pd.read_csv(path, encoding="utf-8-sig")
    cols = {"Player", "Roster Position", "%Drafted"}
    if not cols <= set(raw.columns):
        raise StandingsError(f"{Path(path).name} has no %Drafted summary")
    own = raw[raw["Player"].notna()][["Player", "Roster Position", "%Drafted"]].copy()
    own.columns = ["name", "roster_position", "drafted"]
    own["drafted"] = own["drafted"].astype(str).str.rstrip("%").astype(float) / 100
    return own.reset_index(drop=True)


def to_lineups(entries: pd.DataFrame, pool: pd.DataFrame) -> tuple[list[Lineup], list[str]]:
    """Map parsed entries to pool ids by name. Returns (lineups, unmatched names).

    Entries containing a name the pool does not carry are dropped (and the
    names reported) rather than guessed -- a calibration against a field
    with invented players would be worse than one on fewer entries.
    """
    by_name = {normalize_name(n): pid for n, pid in zip(pool["name"], pool["player_id"])}
    lineups, unmatched = [], []
    for row in entries.itertuples():
        names = (row.captain,) + tuple(row.flex)
        ids = [by_name.get(normalize_name(n)) for n in names]
        if any(i is None for i in ids):
            unmatched.extend(n for n, i in zip(names, ids) if i is None)
            continue
        lineups.append(Lineup(str(ids[0]), tuple(sorted(str(i) for i in ids[1:]))))
    return lineups, sorted(set(unmatched))
