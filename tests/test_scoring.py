"""Hand-calculated DK scoring tests (spec §4).

Every expected value below is worked out by hand in the docstring/comment so a
failure tells you which rule broke, not just that a number moved.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src import scoring


class TestPassingBonus:
    def test_just_under_300_yards_gets_no_bonus(self):
        # 299 * 0.04 = 11.96 | 2 TD = 8 | 1 INT = -1 | 20 rush yd = 2.0
        # 11.96 + 8 - 1 + 2 = 20.96
        assert scoring.score_offense(
            pass_yards=299, pass_tds=2, interceptions=1, rush_yards=20
        ) == pytest.approx(20.96)

    def test_exactly_300_yards_gets_the_bonus(self):
        # 300 * 0.04 = 12 | 2 TD = 8 | 1 INT = -1 | 20 rush yd = 2 | +3 bonus
        # 12 + 8 - 1 + 2 + 3 = 24.0
        assert scoring.score_offense(
            pass_yards=300, pass_tds=2, interceptions=1, rush_yards=20
        ) == pytest.approx(24.0)


class TestRushingBonus:
    def test_just_under_100_rush_yards(self):
        # 99 * 0.1 = 9.9 | 1 TD = 6 | 3 rec = 3 | 25 rec yd = 2.5
        # 9.9 + 6 + 3 + 2.5 = 21.4
        assert scoring.score_offense(
            rush_yards=99, rush_tds=1, receptions=3, rec_yards=25
        ) == pytest.approx(21.4)

    def test_exactly_100_rush_yards_gets_the_bonus(self):
        # 100 * 0.1 = 10 | 1 TD = 6 | 3 rec = 3 | 25 rec yd = 2.5 | +3 bonus
        # 10 + 6 + 3 + 2.5 + 3 = 24.5
        assert scoring.score_offense(
            rush_yards=100, rush_tds=1, receptions=3, rec_yards=25
        ) == pytest.approx(24.5)


class TestReceiving:
    def test_full_ppr_with_receiving_bonus_and_fumble(self):
        # 8 rec = 8 | 112 rec yd = 11.2 | 1 TD = 6 | 1 fumble lost = -1 | +3 bonus
        # 8 + 11.2 + 6 - 1 + 3 = 27.2
        assert scoring.score_offense(
            receptions=8, rec_yards=112, rec_tds=1, fumbles_lost=1
        ) == pytest.approx(27.2)


class TestCombinedBonuses:
    def test_dual_threat_qb_earns_both_bonuses(self):
        # A 300-yard passing AND 100-yard rushing game stacks both +3 bonuses.
        # 350 * 0.04 = 14 | 3 pass TD = 12 | 1 INT = -1
        # 120 * 0.1 = 12 | 1 rush TD = 6 | +3 pass bonus | +3 rush bonus
        # 14 + 12 - 1 + 12 + 6 + 3 + 3 = 49.0
        assert scoring.score_offense(
            pass_yards=350, pass_tds=3, interceptions=1, rush_yards=120, rush_tds=1
        ) == pytest.approx(49.0)


class TestMiscScoring:
    def test_two_point_conversion_and_return_td(self):
        # 5 rec = 5 | 40 rec yd = 4 | 1 two-pt = 2 | 1 return TD = 6
        # 5 + 4 + 2 + 6 = 17.0
        assert scoring.score_offense(
            receptions=5, rec_yards=40, two_pt=1, st_tds=1
        ) == pytest.approx(17.0)

    def test_empty_stat_line_is_zero(self):
        assert scoring.score_offense() == 0.0


class TestDstPointsAllowedTable:
    @pytest.mark.parametrize(
        ("points_allowed", "expected"),
        [
            (0, 10.0),
            (1, 7.0),
            (6, 7.0),
            (7, 4.0),
            (13, 4.0),
            (14, 1.0),
            (20, 1.0),
            (21, 0.0),
            (27, 0.0),
            (28, -1.0),
            (34, -1.0),
            (35, -4.0),
            (59, -4.0),
        ],
    )
    def test_every_tier_boundary(self, points_allowed, expected):
        assert scoring.dst_points_allowed_points(points_allowed) == expected

    def test_negative_points_allowed_is_rejected(self):
        with pytest.raises(ValueError):
            scoring.dst_points_allowed_points(-1)


class TestDstScoring:
    def test_shutout_with_defensive_touchdown(self):
        # 0 allowed = 10 | 5 sacks = 5 | 2 INT = 4 | 1 fum rec = 2 | 1 def TD = 6
        # 10 + 5 + 4 + 2 + 6 = 27.0
        assert scoring.score_dst(
            points_allowed=0, sacks=5, interceptions=2, fumbles_rec=1, def_tds=1
        ) == pytest.approx(27.0)

    def test_blowout_loss_can_go_negative(self):
        # 38 allowed = -4 | 1 sack = 1  ->  -3.0
        assert scoring.score_dst(points_allowed=38, sacks=1) == pytest.approx(-3.0)

    def test_safety_blocked_kick_and_return_td(self):
        # 17 allowed = 1 | 3 sacks = 3 | 1 safety = 2 | 1 blocked kick = 2
        # 1 special teams TD = 6
        # 1 + 3 + 2 + 2 + 6 = 14.0
        assert scoring.score_dst(
            points_allowed=17, sacks=3, safeties=1, blocked_kicks=1, special_tds=1
        ) == pytest.approx(14.0)

    def test_half_sacks_are_supported(self):
        # nflverse credits split sacks as 0.5 each.
        # 24 allowed = 0 | 2.5 sacks = 2.5
        assert scoring.score_dst(points_allowed=24, sacks=2.5) == pytest.approx(2.5)


class TestVectorizedMatchesScalar:
    def test_offense_frame_agrees_with_scalar(self):
        rows = [
            dict(pass_yards=299, pass_tds=2, interceptions=1, rush_yards=20,
                 rush_tds=0, receptions=0, rec_yards=0, rec_tds=0,
                 fumbles_lost=0, two_pt=0, st_tds=0),
            dict(pass_yards=350, pass_tds=3, interceptions=1, rush_yards=120,
                 rush_tds=1, receptions=0, rec_yards=0, rec_tds=0,
                 fumbles_lost=0, two_pt=0, st_tds=0),
            dict(pass_yards=0, pass_tds=0, interceptions=0, rush_yards=100,
                 rush_tds=1, receptions=3, rec_yards=25, rec_tds=0,
                 fumbles_lost=0, two_pt=0, st_tds=0),
            dict(pass_yards=0, pass_tds=0, interceptions=0, rush_yards=0,
                 rush_tds=0, receptions=8, rec_yards=112, rec_tds=1,
                 fumbles_lost=1, two_pt=1, st_tds=1),
        ]
        df = pd.DataFrame(rows)
        vectorized = scoring.score_offense_frame(df).tolist()
        scalar = [scoring.score_offense(**row) for row in rows]
        assert vectorized == pytest.approx(scalar)

    def test_offense_frame_handles_nulls_as_zero(self):
        df = pd.DataFrame([{c: None for c in scoring._OFFENSE_COLS}])
        assert scoring.score_offense_frame(df).tolist() == [0.0]

    def test_dst_frame_agrees_with_scalar(self):
        rows = [
            dict(points_allowed=0, sacks=5, interceptions=2, fumbles_rec=1,
                 def_tds=1, special_tds=0, safeties=0, blocked_kicks=0),
            dict(points_allowed=38, sacks=1, interceptions=0, fumbles_rec=0,
                 def_tds=0, special_tds=0, safeties=0, blocked_kicks=0),
            dict(points_allowed=17, sacks=3, interceptions=0, fumbles_rec=0,
                 def_tds=0, special_tds=1, safeties=1, blocked_kicks=1),
            dict(points_allowed=21, sacks=0, interceptions=0, fumbles_rec=0,
                 def_tds=0, special_tds=0, safeties=0, blocked_kicks=0),
            dict(points_allowed=7, sacks=2.5, interceptions=1, fumbles_rec=2,
                 def_tds=0, special_tds=0, safeties=0, blocked_kicks=0),
        ]
        df = pd.DataFrame(rows)
        vectorized = scoring.score_dst_frame(df).tolist()
        scalar = [scoring.score_dst(**row) for row in rows]
        assert vectorized == pytest.approx(scalar)
