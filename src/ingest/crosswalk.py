"""Canonical ID mapping — the name-matching gauntlet (spec §5.2).

Matching waterfall, in order, recording ``match_method`` for every row:

1. ``manual``        — ``data/manual_overrides.csv``, loaded first on every run.
2. ``dst_map``       — team defenses via the hardcoded 32-row map (:mod:`src.teams`).
3. ``id_map``        — join on a vendor ID present in ``nfl_data_py.import_ids()``.
4. ``exact``         — normalized name + team + position.
5. ``exact_name_pos``— normalized name + position, *only* when unambiguous. This is
                       the spec's traded-player path: match on name+position, then
                       accept the reference roster's team.
6. ``fuzzy``         — ``token_sort_ratio >= 90`` within the same team AND position.
                       Never across teams.
7. unmatched         — appended to ``data/unmatched_review.csv`` for the manual queue.

Methods 2 and 5 are additions to the spec's four-value ``match_method`` list; both
are recorded distinctly so join provenance stays auditable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process

from src import config as config_mod
from src import teams as teams_mod

log = logging.getLogger(__name__)

MANUAL_OVERRIDE_COLUMNS = ["source", "source_name", "source_id", "player_id", "note"]
UNMATCHED_COLUMNS = ["source", "source_name", "source_id", "team", "position", "season", "week"]

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_PUNCT = re.compile(r"[^a-z0-9\s]")


def normalize_name(name: str | None) -> str:
    """lowercase, strip punctuation, strip generational suffixes, collapse space.

    ``D.J. Moore`` -> ``dj moore``; ``Odell Beckham Jr.`` -> ``odell beckham``.
    """
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return ""
    text = _PUNCT.sub("", str(name).lower())
    tokens = [t for t in text.split() if t]
    while len(tokens) > 1 and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_position(position: str | None) -> str | None:
    """Collapse feed-specific position spellings onto DK's roster positions."""
    if position is None or (isinstance(position, float) and pd.isna(position)):
        return None
    pos = str(position).strip().upper()
    if pos in {"DST", "D/ST", "DEF", "D"}:
        return "DST"
    if pos in {"FB", "HB"}:
        return "RB"
    if pos in {"PK"}:
        return "K"
    return pos or None


@dataclass
class MatchResult:
    """Outcome of running a source's rows through the waterfall."""

    matched: pd.DataFrame          # source rows + player_id + match_method
    unmatched: pd.DataFrame        # source rows that fell through every tier
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.matched) + len(self.unmatched)

    @property
    def coverage(self) -> float:
        return len(self.matched) / self.total if self.total else 0.0

    def summary(self) -> str:
        by_method = ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        return (
            f"{len(self.matched)}/{self.total} matched ({self.coverage:.1%}) [{by_method}]"
        )


# --- Reference universe -------------------------------------------------------


def build_reference(conn, season: int, week: int | None = None) -> pd.DataFrame:
    """Candidate players to match against: everyone who has a stat row.

    Restricted to ``season`` (and ``week`` when given) so a match is validated
    against the roster as it actually was, not an all-time name list.
    """
    params: list = [season]
    week_clause = ""
    if week is not None:
        week_clause = " AND s.week = ?"
        params.append(week)

    query = f"""
        SELECT DISTINCT s.player_id, p.name, s.team, p.position
        FROM player_week_stats s
        JOIN players p ON p.player_id = s.player_id
        WHERE s.season = ?{week_clause}
    """
    ref = pd.read_sql_query(query, conn, params=params)
    ref["norm_name"] = ref["name"].map(normalize_name)
    ref["team"] = ref["team"].map(teams_mod.normalize_team)
    ref["position"] = ref["position"].map(normalize_position)
    return ref.dropna(subset=["player_id"])


# --- Manual overrides ---------------------------------------------------------


def load_manual_overrides(path: Path | None = None, cfg=None) -> pd.DataFrame:
    """Read ``manual_overrides.csv``; returns an empty frame if absent."""
    cfg = cfg or config_mod.load()
    path = Path(path) if path else cfg.path("manual_overrides")
    if not path.exists():
        return pd.DataFrame(columns=MANUAL_OVERRIDE_COLUMNS)
    df = pd.read_csv(path, dtype=str).fillna("")
    missing = [c for c in ("source", "source_name", "player_id") if c not in df.columns]
    if missing:
        raise ValueError(f"{path} is missing required columns: {missing}")
    for col in MANUAL_OVERRIDE_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df["norm_name"] = df["source_name"].map(normalize_name)
    return df


def write_unmatched(rows: pd.DataFrame, path: Path | None = None, cfg=None) -> Path:
    """Append unmatched rows to the review queue, de-duplicated."""
    cfg = cfg or config_mod.load()
    path = Path(path) if path else cfg.path("unmatched_review")
    path.parent.mkdir(parents=True, exist_ok=True)

    frame = rows.reindex(columns=UNMATCHED_COLUMNS)
    if path.exists():
        frame = pd.concat([pd.read_csv(path, dtype=str), frame], ignore_index=True)
    # Round-tripping through CSV turns "" into NaN, so normalize both sides to
    # plain strings before de-duplicating or the same row appends forever.
    frame = frame.astype(str).replace({"nan": "", "None": ""})
    frame = frame.drop_duplicates(subset=["source", "source_name", "source_id", "season", "week"])
    frame.to_csv(path, index=False)
    return path


# --- The waterfall ------------------------------------------------------------


def resolve(
    source_rows: pd.DataFrame,
    reference: pd.DataFrame,
    *,
    source: str,
    id_map: pd.DataFrame | None = None,
    manual: pd.DataFrame | None = None,
    fuzzy_threshold: int = 90,
) -> MatchResult:
    """Run ``source_rows`` through the waterfall against ``reference``.

    ``source_rows`` must have ``source_name``; ``team``, ``position`` and
    ``source_id`` are used when present. ``id_map`` is an optional frame with
    ``source_id`` and ``player_id`` columns.
    """
    rows = source_rows.copy().reset_index(drop=True)
    for col in ("source_id", "team", "position"):
        if col not in rows.columns:
            rows[col] = None

    rows["norm_name"] = rows["source_name"].map(normalize_name)
    rows["team"] = rows["team"].map(teams_mod.normalize_team)
    rows["position"] = rows["position"].map(normalize_position)
    rows["source_id"] = rows["source_id"].fillna("").astype(str)

    rows["player_id"] = None
    rows["match_method"] = None

    def open_mask() -> pd.Series:
        return rows["player_id"].isna()

    # 1. Manual overrides win outright.
    if manual is not None and len(manual):
        mine = manual[manual["source"] == source]
        if len(mine):
            by_name = dict(zip(mine["norm_name"], mine["player_id"]))
            by_id = {
                sid: pid
                for sid, pid in zip(mine["source_id"].astype(str), mine["player_id"])
                if sid
            }
            hit_id = rows["source_id"].map(by_id)
            hit_name = rows["norm_name"].map(by_name)
            resolved = hit_id.fillna(hit_name)
            mask = open_mask() & resolved.notna()
            rows.loc[mask, "player_id"] = resolved[mask]
            rows.loc[mask, "match_method"] = "manual"

    # 2. Team defenses: name -> canonical team code -> DST_<TEAM>.
    dst_mask = open_mask() & (rows["position"] == "DST")
    if dst_mask.any():
        # A DK defense row carries the team in either the name or the team column.
        from_name = rows.loc[dst_mask, "source_name"].map(teams_mod.dst_team_from_name)
        resolved_team = from_name.fillna(rows.loc[dst_mask, "team"])
        ids = resolved_team.map(
            lambda t: teams_mod.dst_player_id(t) if pd.notna(t) else None
        )
        known = set(reference["player_id"])
        ids = ids.map(lambda pid: pid if pid in known else None)
        hit = ids.notna()
        idx = ids[hit].index
        rows.loc[idx, "player_id"] = ids[hit]
        rows.loc[idx, "match_method"] = "dst_map"

    # 3. Vendor ID map.
    if id_map is not None and len(id_map):
        lookup = {
            str(sid): pid
            for sid, pid in zip(id_map["source_id"].astype(str), id_map["player_id"])
            if str(sid) and pd.notna(pid)
        }
        resolved = rows["source_id"].map(lookup)
        mask = open_mask() & resolved.notna()
        rows.loc[mask, "player_id"] = resolved[mask]
        rows.loc[mask, "match_method"] = "id_map"

    # 4. Exact: normalized name + team + position.
    ref_exact = reference.dropna(subset=["team", "position"]).drop_duplicates(
        subset=["norm_name", "team", "position"], keep=False
    )
    key_to_id = dict(
        zip(
            zip(ref_exact["norm_name"], ref_exact["team"], ref_exact["position"]),
            ref_exact["player_id"],
        )
    )
    keys = list(zip(rows["norm_name"], rows["team"], rows["position"]))
    resolved = pd.Series([key_to_id.get(k) for k in keys], index=rows.index)
    mask = open_mask() & resolved.notna()
    rows.loc[mask, "player_id"] = resolved[mask]
    rows.loc[mask, "match_method"] = "exact"

    # 5. Name + position, only where unambiguous across the season. Catches
    #    mid-season trades, where the vendor's team is stale.
    ref_np = reference.dropna(subset=["position"]).drop_duplicates(
        subset=["norm_name", "position"], keep=False
    )
    np_to_id = dict(
        zip(zip(ref_np["norm_name"], ref_np["position"]), ref_np["player_id"])
    )
    keys = list(zip(rows["norm_name"], rows["position"]))
    resolved = pd.Series([np_to_id.get(k) for k in keys], index=rows.index)
    mask = open_mask() & resolved.notna()
    rows.loc[mask, "player_id"] = resolved[mask]
    rows.loc[mask, "match_method"] = "exact_name_pos"

    # 6. Fuzzy, strictly within the same team AND position.
    still_open = rows[open_mask() & rows["team"].notna() & rows["position"].notna()]
    if len(still_open):
        for (team, position), group in still_open.groupby(["team", "position"]):
            candidates = reference[
                (reference["team"] == team) & (reference["position"] == position)
            ]
            if candidates.empty:
                continue
            choices = candidates["norm_name"].tolist()
            ids = candidates["player_id"].tolist()
            for idx, norm_name in zip(group.index, group["norm_name"]):
                if not norm_name:
                    continue
                best = process.extractOne(
                    norm_name, choices, scorer=fuzz.token_sort_ratio,
                    score_cutoff=fuzzy_threshold,
                )
                if best is not None:
                    rows.at[idx, "player_id"] = ids[best[2]]
                    rows.at[idx, "match_method"] = "fuzzy"

    matched = rows[rows["player_id"].notna()].copy()
    unmatched = rows[rows["player_id"].isna()].copy()
    unmatched["source"] = source
    counts = matched["match_method"].value_counts().to_dict()
    return MatchResult(matched=matched, unmatched=unmatched, counts=counts)


def persist(conn, source: str, matched: pd.DataFrame) -> int:
    """Write resolved matches into ``id_crosswalk`` (idempotent)."""
    from src import db

    if matched.empty:
        return 0
    frame = pd.DataFrame(
        {
            "player_id": matched["player_id"],
            "source": source,
            "source_id": matched.get("source_id", pd.Series("", index=matched.index)).fillna(""),
            "source_name": matched["source_name"],
            "match_method": matched["match_method"],
        }
    ).drop_duplicates(subset=["source", "source_name", "source_id"])
    return db.upsert_df(conn, "id_crosswalk", frame)


def load_id_map(source_id_column: str) -> pd.DataFrame:
    """Build a ``source_id -> player_id`` frame from ``nfl_data_py.import_ids()``.

    ``source_id_column`` is a column of that file, e.g. ``espn_id``,
    ``sleeper_id``, ``pfr_id``, ``fantasypros_id``.
    """
    import nfl_data_py as nfl

    ids = nfl.import_ids()
    if source_id_column not in ids.columns:
        raise ValueError(
            f"{source_id_column!r} is not an import_ids() column; "
            f"available: {sorted(ids.columns)}"
        )
    frame = ids[[source_id_column, "gsis_id"]].dropna()
    frame.columns = ["source_id", "player_id"]
    frame["source_id"] = frame["source_id"].map(
        lambda v: str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
    )
    return frame[frame["player_id"].astype(str).str.len() > 0]
