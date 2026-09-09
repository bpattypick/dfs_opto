"""Optimal-rate ownership baseline tests (roadmap Step 3a, task T4)."""

from __future__ import annotations

import pandas as pd
import pytest

from src import ownership


def make_pool(n_per_team=6, teams=("SEA", "NE")):
    """A legal two-team Showdown pool with a clear value gradient."""
    rows = []
    for t_i, team in enumerate(teams):
        for i in range(n_per_team):
            idx = t_i * n_per_team + i
            rows.append({
                "player_id": f"00-00{idx:03d}",
                "name": f"{team} Player {i}",
                "team": team,
                "salary": 10000 - i * 800,
                "projection": 20.0 - i * 1.5,
            })
    return pd.DataFrame(rows)


class TestPoolValidation:
    def test_empty_pool_is_refused(self):
        with pytest.raises(ownership.OwnershipError, match="empty"):
            ownership.estimate_ownership(pd.DataFrame(), n=1)

    def test_missing_columns_are_named(self):
        pool = make_pool().drop(columns=["projection"])
        with pytest.raises(ownership.OwnershipError, match="projection"):
            ownership.estimate_ownership(pool, n=1)

    def test_duplicate_player_ids_are_refused(self):
        # The DK export's two rows per player loaded verbatim looks like this.
        pool = pd.concat([make_pool(), make_pool().head(1)], ignore_index=True)
        with pytest.raises(ownership.OwnershipError, match="duplicate player_id"):
            ownership.estimate_ownership(pool, n=1)

    @pytest.mark.parametrize("teams", [("SEA",), ("SEA", "NE", "KC")])
    def test_pool_must_be_exactly_one_game(self, teams):
        # One team is a bad filter; three means two slates got mixed.
        with pytest.raises(ownership.OwnershipError, match="exactly 2 teams"):
            ownership.estimate_ownership(make_pool(4, teams), n=1)

    def test_null_projection_is_refused(self):
        pool = make_pool()
        pool.loc[0, "projection"] = None
        with pytest.raises(ownership.OwnershipError, match="null projection"):
            ownership.estimate_ownership(pool, n=1)

    def test_non_positive_salary_is_refused(self):
        pool = make_pool()
        pool.loc[0, "salary"] = 0
        with pytest.raises(ownership.OwnershipError, match="non-positive salary"):
            ownership.estimate_ownership(pool, n=1)

    def test_pool_too_small_to_fill_a_roster(self):
        pool = make_pool(2)  # 4 players, roster needs 6
        with pytest.raises(ownership.OwnershipError, match="at least 6"):
            ownership.estimate_ownership(pool, n=1)

    @pytest.mark.parametrize(("kwargs", "match"), [
        ({"n": 0}, "n must be at least 1"),
        ({"jitter": -0.1}, "jitter must be non-negative"),
    ])
    def test_bad_parameters_are_refused(self, kwargs, match):
        with pytest.raises(ownership.OwnershipError, match=match):
            ownership.estimate_ownership(make_pool(), **kwargs)


class TestOptimalLineup:
    def test_returns_one_captain_and_five_flex(self):
        pool = make_pool()
        captain, flex = ownership.optimal_lineup(pool, pool["projection"])
        assert captain not in flex
        assert len(flex) == 5
        assert len(set(flex)) == 5

    def test_lineup_respects_the_salary_cap(self):
        pool = make_pool()
        captain, flex = ownership.optimal_lineup(pool, pool["projection"])
        salary = pool.set_index("player_id")["salary"]
        total = salary[captain] * ownership.CAPTAIN_MULTIPLIER + salary[flex].sum()
        assert total <= 50000

    def test_lineup_uses_both_teams(self):
        # DK requires it, and a field generator that ignored it would produce
        # illegal opponents.
        pool = make_pool()
        captain, flex = ownership.optimal_lineup(pool, pool["projection"])
        team = pool.set_index("player_id")["team"]
        assert len(set(team[[captain] + flex])) == 2

    def test_a_player_cannot_fill_cpt_and_flex(self):
        # The optimizer dedupes by name, so ids are passed as names. If that
        # ever regresses, the best value player appears twice.
        pool = make_pool()
        pool.loc[0, "projection"] = 200.0
        captain, flex = ownership.optimal_lineup(pool, pool["projection"])
        assert captain not in flex

    def test_unreachable_salary_cap_fails_loudly(self):
        pool = make_pool()
        pool["salary"] = 40000  # any 6 blow the 50k cap
        with pytest.raises(ownership.OwnershipError, match="legal lineup"):
            ownership.optimal_lineup(pool, pool["projection"])


class TestEstimateOwnership:
    def test_returns_a_row_per_player_including_the_unselected(self):
        pool = make_pool()
        rates = ownership.estimate_ownership(pool, n=5, seed=0)
        assert len(rates) == len(pool)
        assert set(rates["player_id"]) == set(pool["player_id"])

    def test_rates_are_fractions_and_total_is_their_sum(self):
        rates = ownership.estimate_ownership(make_pool(), n=10, seed=0)
        for column in ("cpt_pct", "flex_pct", "total_pct"):
            assert rates[column].between(0.0, 1.0).all()
        assert (rates["total_pct"] == rates["cpt_pct"] + rates["flex_pct"]).all()

    def test_exactly_one_captain_and_five_flex_slots_per_trial(self):
        # Rates are counts/n, so the columns must sum to the roster shape.
        n = 8
        rates = ownership.estimate_ownership(make_pool(), n=n, seed=1)
        assert rates["cpt_pct"].sum() == pytest.approx(1.0)
        assert rates["flex_pct"].sum() == pytest.approx(5.0)

    def test_sorted_by_total_ownership(self):
        rates = ownership.estimate_ownership(make_pool(), n=10, seed=0)
        assert rates["total_pct"].is_monotonic_decreasing

    def test_same_seed_reproduces_the_estimate(self):
        pool = make_pool()
        a = ownership.estimate_ownership(pool, n=10, seed=42)
        b = ownership.estimate_ownership(pool, n=10, seed=42)
        pd.testing.assert_frame_equal(a, b)

    def test_zero_jitter_collapses_onto_one_lineup(self):
        # Without noise every trial is the same solve, so exactly 6 players are
        # ever selected. This is what the jitter parameter exists to avoid.
        rates = ownership.estimate_ownership(make_pool(), n=3, jitter=0.0, seed=0)
        assert (rates["total_pct"] > 0).sum() == ownership.ROSTER_SIZE
        assert set(rates.loc[rates["total_pct"] > 0, "total_pct"]) == {1.0}

    def test_jitter_spreads_ownership_beyond_one_lineup(self):
        rates = ownership.estimate_ownership(make_pool(), n=30, jitter=0.4, seed=0)
        assert (rates["total_pct"] > 0).sum() > ownership.ROSTER_SIZE

    def test_better_value_players_are_owned_more(self):
        # The whole premise of the baseline: ownership tracks obvious value.
        # Flatten salary so value is purely projection, then make three players
        # strictly dominant. They should be owned far above everyone else.
        pool = make_pool()
        pool["salary"] = 7000        # 1.5x CPT + 5 FLEX = 45,500, inside the cap
        pool["projection"] = 10.0
        bargains = {pool.loc[0, "player_id"], pool.loc[1, "player_id"],
                    pool.loc[6, "player_id"]}
        pool.loc[pool["player_id"].isin(bargains), "projection"] = 18.0

        rates = ownership.estimate_ownership(pool, n=30, jitter=0.15, seed=7)
        assert set(rates.head(3)["player_id"]) == bargains
        assert rates.head(3)["total_pct"].min() > rates.tail(1)["total_pct"].item()
