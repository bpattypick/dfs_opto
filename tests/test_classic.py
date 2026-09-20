"""T26: DK Classic roster rules (src/classic.py), mirroring test coverage for
src/showdown.py's legality checks in tests/test_field.py.
"""

from __future__ import annotations

import pytest

from src import classic

POSITION = {
    "qb1": "QB", "qb2": "QB",
    "rb1": "RB", "rb2": "RB", "rb3": "RB",
    "wr1": "WR", "wr2": "WR", "wr3": "WR", "wr4": "WR",
    "te1": "TE", "te2": "TE",
    "dst1": "DST", "dst2": "DST",
}
SALARY = {
    "qb1": 7000, "qb2": 6000,
    "rb1": 7000, "rb2": 6000, "rb3": 5000,
    "wr1": 7000, "wr2": 6000, "wr3": 5000, "wr4": 4000,
    "te1": 4000, "te2": 3000,
    "dst1": 2500, "dst2": 2000,
}


def legal_lineup():
    return classic.Lineup(
        qb="qb1", rb=("rb1", "rb2"), wr=("wr1", "wr2", "wr3"),
        te="te1", flex="rb3", dst="dst1",
    )


class TestShape:
    def test_players_is_all_nine_in_a_fixed_order(self):
        lu = legal_lineup()
        assert lu.players == ("qb1", "rb1", "rb2", "wr1", "wr2", "wr3", "te1", "rb3", "dst1")
        assert len(set(lu.players)) == classic.ROSTER_SIZE == 9

    def test_key_is_unordered(self):
        a = classic.Lineup("qb1", ("rb1", "rb2"), ("wr1", "wr2", "wr3"), "te1", "rb3", "dst1")
        b = classic.Lineup("qb1", ("rb2", "rb1"), ("wr3", "wr2", "wr1"), "te1", "rb3", "dst1")
        assert a.key() == b.key()


class TestSalaryAndPoints:
    def test_no_captain_multiplier(self):
        lu = legal_lineup()
        expected = sum(SALARY[p] for p in lu.players)
        assert classic.lineup_salary(lu, SALARY) == expected

    def test_lineup_points_sums_all_nine_unweighted(self):
        lu = legal_lineup()
        points = {p: 10.0 for p in lu.players}
        assert classic.lineup_points(lu, points) == 90.0

    def test_unknown_player_in_salary_raises(self):
        lu = classic.Lineup("nope", ("rb1", "rb2"), ("wr1", "wr2", "wr3"), "te1", "rb3", "dst1")
        with pytest.raises(classic.IllegalLineup, match="nope"):
            classic.lineup_salary(lu, SALARY)


class TestLegality:
    def test_a_legal_lineup_passes(self):
        classic.check_lineup(legal_lineup(), SALARY, POSITION)  # raises if not

    def test_duplicate_player_is_refused(self):
        lu = classic.Lineup("qb1", ("rb1", "rb1"), ("wr1", "wr2", "wr3"), "te1", "rb3", "dst1")
        with pytest.raises(classic.IllegalLineup, match="appears twice"):
            classic.check_lineup(lu, SALARY, POSITION)

    def test_wrong_position_in_qb_slot_is_refused(self):
        lu = classic.Lineup("rb1", ("rb2", "rb3"), ("wr1", "wr2", "wr3"), "te1", "wr4", "dst1")
        with pytest.raises(classic.IllegalLineup, match="slotted QB but is RB"):
            classic.check_lineup(lu, SALARY, POSITION)

    def test_wrong_position_in_flex_slot_is_refused(self):
        lu = classic.Lineup("qb1", ("rb1", "rb2"), ("wr1", "wr2", "wr3"), "te1", "dst2", "dst1")
        with pytest.raises(classic.IllegalLineup, match="slotted FLEX but is DST"):
            classic.check_lineup(lu, SALARY, POSITION)

    def test_flex_accepts_rb_wr_or_te(self):
        for flex_player, pos in (("rb3", "RB"), ("wr4", "WR"), ("te2", "TE")):
            lu = classic.Lineup("qb1", ("rb1", "rb2"), ("wr1", "wr2", "wr3"),
                                 "te1", flex_player, "dst1")
            classic.check_lineup(lu, SALARY, POSITION)  # raises if not

    def test_salary_cap_is_enforced(self):
        expensive = dict(SALARY)
        for p in expensive:
            expensive[p] = 10_000
        with pytest.raises(classic.IllegalLineup, match="exceeds the"):
            classic.check_lineup(legal_lineup(), expensive, POSITION)

    def test_unknown_position_player_is_named(self):
        lu = classic.Lineup("qb1", ("rb1", "rb2"), ("wr1", "wr2", "wr3"), "te1", "rb3", "ghost")
        with pytest.raises(classic.IllegalLineup, match="ghost"):
            classic.check_lineup(lu, SALARY, POSITION)

    def test_no_team_count_restriction(self):
        # Unlike Showdown, a Classic lineup does not need >=2 teams represented.
        one_team_position = {p: pos for p, pos in POSITION.items()}
        one_team_salary = {p: 4000 for p in POSITION}
        classic.check_lineup(legal_lineup(), one_team_salary, one_team_position)

    def test_is_legal_matches_check_lineup(self):
        assert classic.is_legal(legal_lineup(), SALARY, POSITION)
        bad = classic.Lineup("qb1", ("rb1", "rb1"), ("wr1", "wr2", "wr3"), "te1", "rb3", "dst1")
        assert not classic.is_legal(bad, SALARY, POSITION)
