"""DraftKings NFL Classic fantasy scoring.

Spec §4. Implemented once, unit tested, and used at ingest time so that
``dk_points`` is stored rather than recomputed at every call site.

Scalar functions (:func:`score_offense`, :func:`score_dst`) are the reference
implementation and the thing the tests exercise. The vectorized ``*_frame``
helpers exist for ingest throughput and are covered by a test asserting they
agree with the scalar path row for row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

# --- Offense -----------------------------------------------------------------

PASS_YARD = 0.04
PASS_TD = 4.0
INTERCEPTION = -1.0
PASS_BONUS_YARDS = 300
PASS_BONUS = 3.0

RUSH_YARD = 0.1
RUSH_TD = 6.0
RUSH_BONUS_YARDS = 100
RUSH_BONUS = 3.0

REC_YARD = 0.1
RECEPTION = 1.0  # full PPR
REC_TD = 6.0
REC_BONUS_YARDS = 100
REC_BONUS = 3.0

FUMBLE_LOST = -1.0
TWO_PT = 2.0
RETURN_TD = 6.0

# --- DST ---------------------------------------------------------------------

DST_SACK = 1.0
DST_INTERCEPTION = 2.0
DST_FUMBLE_RECOVERY = 2.0
DST_TD = 6.0  # INT return, fumble return, blocked-kick return, kick/punt return
DST_SAFETY = 2.0
DST_BLOCKED_KICK = 2.0
DST_TWO_PT_RETURN = 2.0

# (inclusive_upper_bound, points). Ordered low to high; 35+ is the fallthrough.
DST_POINTS_ALLOWED_TABLE: tuple[tuple[int, float], ...] = (
    (0, 10.0),
    (6, 7.0),
    (13, 4.0),
    (20, 1.0),
    (27, 0.0),
    (34, -1.0),
)
DST_POINTS_ALLOWED_35_PLUS = -4.0


def dst_points_allowed_points(points_allowed: int) -> float:
    """DK's points-allowed tier table.

    0 → +10, 1-6 → +7, 7-13 → +4, 14-20 → +1, 21-27 → 0, 28-34 → −1, 35+ → −4.
    """
    if points_allowed < 0:
        raise ValueError(f"points_allowed cannot be negative: {points_allowed}")
    for upper, pts in DST_POINTS_ALLOWED_TABLE:
        if points_allowed <= upper:
            return pts
    return DST_POINTS_ALLOWED_35_PLUS


def score_offense(
    *,
    pass_yards: float = 0.0,
    pass_tds: int = 0,
    interceptions: int = 0,
    rush_yards: float = 0.0,
    rush_tds: int = 0,
    receptions: int = 0,
    rec_yards: float = 0.0,
    rec_tds: int = 0,
    fumbles_lost: int = 0,
    two_pt: int = 0,
    st_tds: int = 0,
) -> float:
    """DK points for a skill-position stat line.

    ``two_pt`` counts conversions by pass, run, or catch — DK pays +2 for each,
    including to the passer. ``st_tds`` covers punt/kickoff/FG-return TDs.
    """
    points = 0.0

    points += pass_yards * PASS_YARD
    points += pass_tds * PASS_TD
    points += interceptions * INTERCEPTION
    if pass_yards >= PASS_BONUS_YARDS:
        points += PASS_BONUS

    points += rush_yards * RUSH_YARD
    points += rush_tds * RUSH_TD
    if rush_yards >= RUSH_BONUS_YARDS:
        points += RUSH_BONUS

    points += rec_yards * REC_YARD
    points += receptions * RECEPTION
    points += rec_tds * REC_TD
    if rec_yards >= REC_BONUS_YARDS:
        points += REC_BONUS

    points += fumbles_lost * FUMBLE_LOST
    points += two_pt * TWO_PT
    points += st_tds * RETURN_TD

    return round(points, 2)


def score_dst(
    *,
    points_allowed: int,
    sacks: float = 0.0,
    interceptions: int = 0,
    fumbles_rec: int = 0,
    def_tds: int = 0,
    special_tds: int = 0,
    safeties: int = 0,
    blocked_kicks: int = 0,
    two_pt_returns: int = 0,
) -> float:
    """DK points for a team defense/special teams unit.

    ``def_tds`` are defensive scores (INT/fumble returns); ``special_tds`` are
    kick/punt/blocked-kick return TDs. Both pay +6.

    Known limitation: ``points_allowed`` is sourced as the opponent's final
    score. DK excludes points the opposing *defense* scored against your own
    offense (e.g. a pick-six you threw). Correcting that needs play-by-play
    attribution — see docs/data-sources.md. It affects a small number of
    games and is the single most likely source of DST scoring drift.
    """
    points = dst_points_allowed_points(points_allowed)
    points += sacks * DST_SACK
    points += interceptions * DST_INTERCEPTION
    points += fumbles_rec * DST_FUMBLE_RECOVERY
    points += (def_tds + special_tds) * DST_TD
    points += safeties * DST_SAFETY
    points += blocked_kicks * DST_BLOCKED_KICK
    points += two_pt_returns * DST_TWO_PT_RETURN
    return round(points, 2)


# --- Vectorized helpers used by ingest ---------------------------------------

_OFFENSE_COLS = (
    "pass_yards",
    "pass_tds",
    "interceptions",
    "rush_yards",
    "rush_tds",
    "receptions",
    "rec_yards",
    "rec_tds",
    "fumbles_lost",
    "two_pt",
    "st_tds",
)


def score_offense_frame(df: "pd.DataFrame") -> "pd.Series":
    """Vectorized :func:`score_offense` over a DataFrame with ``_OFFENSE_COLS``."""
    import pandas as pd

    cols = {c: pd.to_numeric(df[c], errors="coerce").fillna(0.0) for c in _OFFENSE_COLS}

    points = (
        cols["pass_yards"] * PASS_YARD
        + cols["pass_tds"] * PASS_TD
        + cols["interceptions"] * INTERCEPTION
        + cols["rush_yards"] * RUSH_YARD
        + cols["rush_tds"] * RUSH_TD
        + cols["rec_yards"] * REC_YARD
        + cols["receptions"] * RECEPTION
        + cols["rec_tds"] * REC_TD
        + cols["fumbles_lost"] * FUMBLE_LOST
        + cols["two_pt"] * TWO_PT
        + cols["st_tds"] * RETURN_TD
    )
    points += (cols["pass_yards"] >= PASS_BONUS_YARDS) * PASS_BONUS
    points += (cols["rush_yards"] >= RUSH_BONUS_YARDS) * RUSH_BONUS
    points += (cols["rec_yards"] >= REC_BONUS_YARDS) * REC_BONUS
    return points.round(2)


_DST_COLS = (
    "points_allowed",
    "sacks",
    "interceptions",
    "fumbles_rec",
    "def_tds",
    "special_tds",
    "safeties",
    "blocked_kicks",
)


def score_dst_frame(df: "pd.DataFrame") -> "pd.Series":
    """Vectorized :func:`score_dst` over a DataFrame with ``_DST_COLS``."""
    import numpy as np
    import pandas as pd

    cols = {c: pd.to_numeric(df[c], errors="coerce").fillna(0.0) for c in _DST_COLS}

    pa = cols["points_allowed"]
    # Build the tier stepwise so the table stays the single source of truth.
    tier = pd.Series(DST_POINTS_ALLOWED_35_PLUS, index=df.index, dtype=float)
    for upper, pts in reversed(DST_POINTS_ALLOWED_TABLE):
        tier = np.where(pa <= upper, pts, tier)
    tier = pd.Series(tier, index=df.index, dtype=float)

    points = (
        tier
        + cols["sacks"] * DST_SACK
        + cols["interceptions"] * DST_INTERCEPTION
        + cols["fumbles_rec"] * DST_FUMBLE_RECOVERY
        + (cols["def_tds"] + cols["special_tds"]) * DST_TD
        + cols["safeties"] * DST_SAFETY
        + cols["blocked_kicks"] * DST_BLOCKED_KICK
    )
    return points.round(2)
