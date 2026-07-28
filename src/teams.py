"""Canonical NFL team codes, historical aliases, and the DST name map.

Spec §5.2 calls DST naming a known landmine: DK spells defenses as team names
while the stats feed uses abbreviations. This module is the hardcoded 32-row map
it asks for, plus the alias table that makes joins survive relocations and the
several abbreviations different feeds use for the same franchise.

Team codes are normalized to the *current* franchise code on both sides of every
join (nflverse's own convention), so a 2019 Oakland row and a 2019 DK "OAK" row
both become ``LV`` and match.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Team:
    abbr: str
    city: str
    nickname: str

    @property
    def full_name(self) -> str:
        return f"{self.city} {self.nickname}"


TEAMS: tuple[Team, ...] = (
    Team("ARI", "Arizona", "Cardinals"),
    Team("ATL", "Atlanta", "Falcons"),
    Team("BAL", "Baltimore", "Ravens"),
    Team("BUF", "Buffalo", "Bills"),
    Team("CAR", "Carolina", "Panthers"),
    Team("CHI", "Chicago", "Bears"),
    Team("CIN", "Cincinnati", "Bengals"),
    Team("CLE", "Cleveland", "Browns"),
    Team("DAL", "Dallas", "Cowboys"),
    Team("DEN", "Denver", "Broncos"),
    Team("DET", "Detroit", "Lions"),
    Team("GB", "Green Bay", "Packers"),
    Team("HOU", "Houston", "Texans"),
    Team("IND", "Indianapolis", "Colts"),
    Team("JAX", "Jacksonville", "Jaguars"),
    Team("KC", "Kansas City", "Chiefs"),
    Team("LAC", "Los Angeles", "Chargers"),
    Team("LAR", "Los Angeles", "Rams"),
    Team("LV", "Las Vegas", "Raiders"),
    Team("MIA", "Miami", "Dolphins"),
    Team("MIN", "Minnesota", "Vikings"),
    Team("NE", "New England", "Patriots"),
    Team("NO", "New Orleans", "Saints"),
    Team("NYG", "New York", "Giants"),
    Team("NYJ", "New York", "Jets"),
    Team("PHI", "Philadelphia", "Eagles"),
    Team("PIT", "Pittsburgh", "Steelers"),
    Team("SEA", "Seattle", "Seahawks"),
    Team("SF", "San Francisco", "49ers"),
    Team("TB", "Tampa Bay", "Buccaneers"),
    Team("TEN", "Tennessee", "Titans"),
    Team("WAS", "Washington", "Commanders"),
)

assert len(TEAMS) == 32, "there are 32 NFL teams"

BY_ABBR: dict[str, Team] = {t.abbr: t for t in TEAMS}
VALID_ABBRS: frozenset[str] = frozenset(BY_ABBR)

# Alternate abbreviations seen across nflverse eras, DK exports, PFR, and ESPN.
# Maps alias -> current canonical code.
_ALIASES: dict[str, str] = {
    # relocations
    "OAK": "LV",
    "SD": "LAC",
    "SDG": "LAC",
    "STL": "LAR",
    "LA": "LAR",
    "RAM": "LAR",
    # spelling variants
    "JAC": "JAX",
    "WSH": "WAS",
    "WFT": "WAS",
    "ARZ": "ARI",
    "BLT": "BAL",
    "CLV": "CLE",
    "HST": "HOU",
    "GNB": "GB",
    "KAN": "KC",
    "NWE": "NE",
    "NOR": "NO",
    "TAM": "TB",
    "SFO": "SF",
    "LVR": "LV",
    "NORL": "NO",
}


def normalize_team(value: str | None) -> str | None:
    """Map any known team spelling to its current canonical abbreviation.

    Returns ``None`` for blank/unknown input so callers can decide whether an
    unmatched team is fatal.
    """
    if value is None:
        return None
    key = str(value).strip().upper()
    if not key or key in {"NAN", "NONE", "FA"}:
        return None
    if key in VALID_ABBRS:
        return key
    if key in _ALIASES:
        return _ALIASES[key]
    return _DST_NAME_TO_ABBR.get(_simple_name_key(key))


def _simple_name_key(value: str) -> str:
    return " ".join(str(value).lower().replace(".", "").split())


def _build_dst_name_map() -> dict[str, str]:
    """Every reasonable spelling of a defense -> canonical team code."""
    mapping: dict[str, str] = {}
    for team in TEAMS:
        for variant in (team.full_name, team.nickname, team.city, team.abbr):
            mapping[_simple_name_key(variant)] = team.abbr
        for suffix in ("d/st", "dst", "defense", "d"):
            mapping[_simple_name_key(f"{team.full_name} {suffix}")] = team.abbr
            mapping[_simple_name_key(f"{team.nickname} {suffix}")] = team.abbr
            mapping[_simple_name_key(f"{team.city} {suffix}")] = team.abbr

    # Franchises whose names changed inside or near the backtest window.
    historical = {
        "oakland raiders": "LV",
        "oakland": "LV",
        "san diego chargers": "LAC",
        "san diego": "LAC",
        "st louis rams": "LAR",
        "st louis": "LAR",
        "washington redskins": "WAS",
        "washington football team": "WAS",
        "redskins": "WAS",
        "football team": "WAS",
        # DK and several feeds abbreviate the Giants/Jets by city+nickname only.
        "ny giants": "NYG",
        "ny jets": "NYJ",
        "new york giants": "NYG",
        "new york jets": "NYJ",
        "la rams": "LAR",
        "la chargers": "LAC",
    }
    for name, abbr in historical.items():
        mapping[_simple_name_key(name)] = abbr
        for suffix in ("d/st", "dst", "defense"):
            mapping[_simple_name_key(f"{name} {suffix}")] = abbr

    # "New York" and "Los Angeles" alone are ambiguous (two teams each) — a
    # silent wrong guess here is exactly the kind of bad join that poisons the
    # model, so refuse to resolve them.
    for ambiguous in ("new york", "los angeles", "la", "ny"):
        mapping.pop(_simple_name_key(ambiguous), None)

    return mapping


_DST_NAME_TO_ABBR: dict[str, str] = _build_dst_name_map()


def dst_team_from_name(name: str | None) -> str | None:
    """Resolve a DK-style defense name ('Chiefs', 'Kansas City Chiefs') to a code."""
    if name is None:
        return None
    return _DST_NAME_TO_ABBR.get(_simple_name_key(name))


def dst_player_id(team_abbr: str) -> str:
    """Synthetic canonical ID for a team defense (defenses have no GSIS ID)."""
    canonical = normalize_team(team_abbr)
    if canonical is None:
        raise ValueError(f"unknown team: {team_abbr!r}")
    return f"DST_{canonical}"
