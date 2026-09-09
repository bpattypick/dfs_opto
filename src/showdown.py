"""DK Showdown roster rules, shared by the contest-sim pieces.

One place for the rules so the ownership baseline (T4), the field generator
(T5), placement (T6) and the duplication model (T7) cannot drift apart on what
counts as a legal lineup. Everything here is DK's published Showdown format:
1 CPT + 5 FLEX from a single game, $50,000 cap, both teams represented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

SALARY_CAP = 50_000
ROSTER_SIZE = 6          # 1 CPT + 5 FLEX
FLEX_SLOTS = ROSTER_SIZE - 1

# The captain slot costs 1.5x salary and scores 1.5x points. Stored salaries are
# the FLEX/UTIL base (CLAUDE.md conventions), so the multiplier is applied in
# code rather than read off the DK export's pre-multiplied CPT row.
CAPTAIN_MULTIPLIER = 1.5


class IllegalLineup(ValueError):
    """A lineup DK would reject at upload."""


@dataclass(frozen=True)
class Lineup:
    """One Showdown entry. ``flex`` order is not meaningful."""

    captain: str
    flex: tuple[str, ...]

    @property
    def players(self) -> tuple[str, ...]:
        return (self.captain,) + self.flex

    def key(self) -> tuple[str, frozenset]:
        """Identity for duplication counting (T7): same captain, same five."""
        return (self.captain, frozenset(self.flex))


def lineup_salary(lineup: Lineup, salary: Mapping[str, float]) -> float:
    """Total DK salary, with the captain's 1.5x applied."""
    try:
        return (salary[lineup.captain] * CAPTAIN_MULTIPLIER
                + sum(salary[p] for p in lineup.flex))
    except KeyError as exc:
        raise IllegalLineup(f"player {exc} is not in the salary table") from exc


def lineup_points(lineup: Lineup, points: Mapping[str, float]) -> float:
    """Total fantasy points, with the captain's 1.5x applied."""
    try:
        return (points[lineup.captain] * CAPTAIN_MULTIPLIER
                + sum(points[p] for p in lineup.flex))
    except KeyError as exc:
        raise IllegalLineup(f"player {exc} has no score") from exc


def check_lineup(
    lineup: Lineup,
    salary: Mapping[str, float],
    team: Mapping[str, str],
    cap: int = SALARY_CAP,
) -> None:
    """Raise ``IllegalLineup`` if DK would reject this entry.

    Checks the four things DK enforces at upload: roster size, no player twice,
    the salary cap, and both teams represented.
    """
    if len(lineup.flex) != FLEX_SLOTS:
        raise IllegalLineup(
            f"expected {FLEX_SLOTS} FLEX, got {len(lineup.flex)}: {lineup.flex}"
        )
    players = lineup.players
    if len(set(players)) != ROSTER_SIZE:
        raise IllegalLineup(f"a player appears twice: {players}")

    total = lineup_salary(lineup, salary)
    if total > cap:
        raise IllegalLineup(f"salary {total:,.0f} exceeds the {cap:,} cap")

    try:
        teams = {team[p] for p in players}
    except KeyError as exc:
        raise IllegalLineup(f"player {exc} has no team") from exc
    if len(teams) < 2:
        # DK requires players from both sides of the game.
        raise IllegalLineup(f"only one team represented: {teams}")


def is_legal(lineup: Lineup, salary, team, cap: int = SALARY_CAP) -> bool:
    try:
        check_lineup(lineup, salary, team, cap)
        return True
    except IllegalLineup:
        return False
