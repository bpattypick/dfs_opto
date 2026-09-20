"""DK Classic main-slate roster rules, shared by the contest-sim pieces.

Mirrors ``src/showdown.py`` in rigor and role, for the Classic format instead
of Showdown: QB, RB, RB, WR, WR, WR, TE, FLEX(RB/WR/TE), DST, $50,000 cap, no
captain slot, players drawn from any team with a game in the slate window.
Confirmed against ``pydfs_lineup_optimizer.get_optimizer(Site.DRAFTKINGS,
Sport.FOOTBALL)``: budget 50000, exactly these 9 positions, no
``max_from_one_team``/``min_teams`` restriction (unlike Showdown, which
requires both teams of its one game to be represented).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

SALARY_CAP = 50_000
ROSTER_SIZE = 9  # QB, RB, RB, WR, WR, WR, TE, FLEX, DST
FLEX_ELIGIBLE = ("RB", "WR", "TE")


class IllegalLineup(ValueError):
    """A lineup DK would reject at upload."""


@dataclass(frozen=True)
class Lineup:
    """One Classic entry. Order within ``rb``/``wr`` is not meaningful."""

    qb: str
    rb: tuple[str, str]
    wr: tuple[str, str, str]
    te: str
    flex: str
    dst: str

    @property
    def players(self) -> tuple[str, ...]:
        return (self.qb,) + self.rb + self.wr + (self.te, self.flex, self.dst)

    def key(self) -> frozenset:
        """Identity for duplication counting: the 9 players, unordered."""
        return frozenset(self.players)


def lineup_salary(lineup: Lineup, salary: Mapping[str, float]) -> float:
    """Total DK salary. No captain multiplier in Classic."""
    try:
        return sum(salary[p] for p in lineup.players)
    except KeyError as exc:
        raise IllegalLineup(f"player {exc} is not in the salary table") from exc


def lineup_points(lineup: Lineup, points: Mapping[str, float]) -> float:
    """Total fantasy points. No captain multiplier in Classic."""
    try:
        return sum(points[p] for p in lineup.players)
    except KeyError as exc:
        raise IllegalLineup(f"player {exc} has no score") from exc


def check_lineup(
    lineup: Lineup,
    salary: Mapping[str, float],
    position: Mapping[str, str],
    cap: int = SALARY_CAP,
) -> None:
    """Raise ``IllegalLineup`` if DK would reject this entry.

    Checks the things DK enforces at upload: roster size, no player twice,
    each slot filled by an eligible position, and the salary cap. Unlike
    Showdown, Classic has no team-count restriction.
    """
    players = lineup.players
    if len(players) != ROSTER_SIZE:
        raise IllegalLineup(f"expected {ROSTER_SIZE} players, got {len(players)}")
    if len(set(players)) != ROSTER_SIZE:
        raise IllegalLineup(f"a player appears twice: {players}")

    try:
        pos = {p: position[p] for p in players}
    except KeyError as exc:
        raise IllegalLineup(f"player {exc} has no position") from exc

    if pos[lineup.qb] != "QB":
        raise IllegalLineup(f"{lineup.qb} is slotted QB but is {pos[lineup.qb]}")
    for p in lineup.rb:
        if pos[p] != "RB":
            raise IllegalLineup(f"{p} is slotted RB but is {pos[p]}")
    for p in lineup.wr:
        if pos[p] != "WR":
            raise IllegalLineup(f"{p} is slotted WR but is {pos[p]}")
    if pos[lineup.te] != "TE":
        raise IllegalLineup(f"{lineup.te} is slotted TE but is {pos[lineup.te]}")
    if pos[lineup.flex] not in FLEX_ELIGIBLE:
        raise IllegalLineup(
            f"{lineup.flex} is slotted FLEX but is {pos[lineup.flex]}, "
            f"not one of {FLEX_ELIGIBLE}"
        )
    if pos[lineup.dst] != "DST":
        raise IllegalLineup(f"{lineup.dst} is slotted DST but is {pos[lineup.dst]}")

    total = lineup_salary(lineup, salary)
    if total > cap:
        raise IllegalLineup(f"salary {total:,.0f} exceeds the {cap:,} cap")


def is_legal(lineup: Lineup, salary, position, cap: int = SALARY_CAP) -> bool:
    try:
        check_lineup(lineup, salary, position, cap)
        return True
    except IllegalLineup:
        return False
