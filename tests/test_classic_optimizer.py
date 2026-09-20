"""T26: Classic optimizer wrapper (src/classic_optimizer.py)."""

from __future__ import annotations

import pandas as pd
import pytest

from src import classic
from src import classic_optimizer as opt


def make_pool():
    # Two teams' worth of players, roomy enough for a unique clear optimum:
    # one obviously-best player at every position, the rest filler.
    rows = [
        ("qb1", "QB One", "AAA", "QB", 6000, 20.0),
        ("qb2", "QB Two", "BBB", "QB", 5500, 15.0),
        ("rb1", "RB One", "AAA", "RB", 7000, 18.0),
        ("rb2", "RB Two", "AAA", "RB", 6000, 15.0),
        ("rb3", "RB Three", "BBB", "RB", 5000, 12.0),
        ("rb4", "RB Four", "BBB", "RB", 4000, 9.0),
        ("wr1", "WR One", "AAA", "WR", 7000, 17.0),
        ("wr2", "WR Two", "BBB", "WR", 6000, 14.0),
        ("wr3", "WR Three", "BBB", "WR", 5000, 11.0),
        ("wr4", "WR Four", "AAA", "WR", 4000, 10.0),
        ("wr5", "WR Five", "AAA", "WR", 3000, 6.0),
        ("te1", "TE One", "AAA", "TE", 4000, 9.0),
        ("te2", "TE Two", "BBB", "TE", 3000, 7.0),
        ("dst1", "AAA DST", "AAA", "DST", 3000, 8.0),
        ("dst2", "BBB DST", "BBB", "DST", 2500, 6.0),
    ]
    pool = pd.DataFrame(rows, columns=["player_id", "name", "team", "position", "salary", "projection"])
    return pool


class TestOptimalLineup:
    def test_returns_a_dk_legal_lineup(self):
        pool = make_pool()
        lu = opt.optimal_lineup(pool, pool["projection"])
        salary = dict(zip(pool.player_id, pool.salary.astype(float)))
        position = dict(zip(pool.player_id, pool.position))
        classic.check_lineup(lu, salary, position)  # raises if not

    def test_picks_the_highest_projected_player_at_each_locked_slot(self):
        pool = make_pool()
        lu = opt.optimal_lineup(pool, pool["projection"])
        assert lu.qb == "qb1"
        assert lu.te == "te1"
        assert lu.dst == "dst1"
        assert set(lu.rb) | {lu.flex} >= {"rb1", "rb2"}

    def test_missing_column_is_named(self):
        pool = make_pool().drop(columns=["position"])
        with pytest.raises(opt.OptimizerError, match="position"):
            opt.optimal_lineup(pool, pool["projection"])

    def test_mismatched_length_is_refused(self):
        pool = make_pool()
        with pytest.raises(opt.OptimizerError, match="same length"):
            opt.optimal_lineup(pool, pool["projection"].iloc[:-1])

    def test_unreachable_cap_fails_loudly(self):
        pool = make_pool()
        pool = pool.assign(salary=100_000)
        with pytest.raises(opt.OptimizerError, match="could not build"):
            opt.optimal_lineup(pool, pool["projection"])

    def test_too_few_players_at_a_position_fails_loudly(self):
        pool = make_pool()
        pool = pool[pool.position != "TE"].reset_index(drop=True)
        with pytest.raises(opt.OptimizerError, match="could not build"):
            opt.optimal_lineup(pool, pool["projection"])
