"""Field generator tests (roadmap Step 3a, task T5)."""

from __future__ import annotations

import pandas as pd
import pytest

from src import field as field_mod
from src import showdown
from src.ownership import estimate_ownership
from tests.test_ownership import make_pool

# Realized ownership tracks the input to within this per-player bound. The
# residual is structural, not sampling noise: the cap makes players compete for
# the same lineup, so a legal field cannot always hit every marginal at once.
# Measured max error is ~0.03 per slot across field sizes 100-5000.
OWNERSHIP_TOLERANCE = 0.05


@pytest.fixture(scope="module")
def pool():
    return make_pool()


@pytest.fixture(scope="module")
def ownership(pool):
    return estimate_ownership(pool, n=200, seed=0)


@pytest.fixture
def entries(pool):
    return pool.loc[:, ["player_id", "team", "salary"]]


def lookups(pool):
    return (dict(zip(pool["player_id"], pool["salary"].astype(float))),
            dict(zip(pool["player_id"], pool["team"])))


class TestValidation:
    def test_empty_pool_is_refused(self, ownership):
        with pytest.raises(field_mod.FieldError, match="pool is empty"):
            field_mod.generate_field(pd.DataFrame(), ownership, size=5)

    def test_missing_pool_column_is_named(self, entries, ownership):
        with pytest.raises(field_mod.FieldError, match="salary"):
            field_mod.generate_field(entries.drop(columns=["salary"]), ownership, size=5)

    def test_missing_ownership_column_is_named(self, entries, ownership):
        with pytest.raises(field_mod.FieldError, match="cpt_pct"):
            field_mod.generate_field(entries, ownership.drop(columns=["cpt_pct"]), size=5)

    def test_player_without_an_ownership_estimate_is_refused(self, entries, ownership):
        # Treating them as 0% would quietly shrink the pool instead of erroring.
        with pytest.raises(field_mod.FieldError, match="no ownership estimate"):
            field_mod.generate_field(entries, ownership.iloc[1:], size=5)

    def test_negative_rates_are_refused(self, entries, ownership):
        bad = ownership.copy()
        bad.loc[0, "flex_pct"] = -0.1
        with pytest.raises(field_mod.FieldError, match="negative flex_pct"):
            field_mod.generate_field(entries, bad, size=5)

    def test_pool_must_be_exactly_one_game(self, pool, ownership):
        one_team = pool[pool["team"] == "SEA"].loc[:, ["player_id", "team", "salary"]]
        with pytest.raises(field_mod.FieldError, match="exactly 2 teams"):
            field_mod.generate_field(one_team, ownership, size=5)

    def test_size_must_be_positive(self, entries, ownership):
        with pytest.raises(field_mod.FieldError, match="at least 1"):
            field_mod.generate_field(entries, ownership, size=0)

    def test_unreachable_cap_fails_loudly(self, entries, ownership):
        # A cap no legal lineup can satisfy must error, not spin or return short.
        with pytest.raises(field_mod.FieldError, match="could not draw a legal lineup"):
            field_mod.generate_field(entries, ownership, size=5, seed=0, cap=1000)


class TestFieldLegality:
    def test_generates_the_requested_size(self, entries, ownership):
        assert len(field_mod.generate_field(entries, ownership, size=25, seed=0)) == 25

    def test_every_lineup_is_dk_legal(self, pool, entries, ownership):
        # The acceptance criterion: an illegal opponent makes placement meaningless.
        salary, team = lookups(pool)
        for lineup in field_mod.generate_field(entries, ownership, size=200, seed=0):
            showdown.check_lineup(lineup, salary, team)  # raises if not

    def test_every_lineup_has_one_captain_and_five_flex(self, entries, ownership):
        for lineup in field_mod.generate_field(entries, ownership, size=50, seed=0):
            assert len(lineup.flex) == showdown.FLEX_SLOTS
            assert len(set(lineup.players)) == showdown.ROSTER_SIZE

    def test_every_lineup_uses_both_teams(self, pool, entries, ownership):
        _, team = lookups(pool)
        for lineup in field_mod.generate_field(entries, ownership, size=50, seed=0):
            assert len({team[p] for p in lineup.players}) == 2

    def test_same_seed_reproduces_the_field(self, entries, ownership):
        a = field_mod.generate_field(entries, ownership, size=30, seed=7)
        b = field_mod.generate_field(entries, ownership, size=30, seed=7)
        assert [l.key() for l in a] == [l.key() for l in b]

    def test_different_seeds_give_different_fields(self, entries, ownership):
        a = field_mod.generate_field(entries, ownership, size=30, seed=1)
        b = field_mod.generate_field(entries, ownership, size=30, seed=2)
        assert [l.key() for l in a] != [l.key() for l in b]


class TestOwnershipMatching:
    def test_realized_ownership_matches_the_input(self, pool, entries, ownership):
        # The other acceptance criterion. A field whose ownership doesn't match
        # the estimate isn't a model of the contest, it's just random lineups.
        generated = field_mod.generate_field(entries, ownership, size=300, seed=0)
        realized = field_mod.realized_ownership(generated, pool)
        merged = ownership.set_index("player_id")[["cpt_pct", "flex_pct"]].join(
            realized.set_index("player_id")[["cpt_pct", "flex_pct"]], rsuffix="_real"
        )
        for column in ("cpt_pct", "flex_pct"):
            error = (merged[f"{column}_real"] - merged[column]).abs().max()
            assert error <= OWNERSHIP_TOLERANCE, f"{column} off by {error:.3f}"

    def test_a_zero_ownership_player_never_appears(self, pool, entries, ownership):
        benched = ownership["player_id"].iloc[-1]
        muted = ownership.copy()
        muted.loc[muted["player_id"] == benched, ["cpt_pct", "flex_pct"]] = 0.0
        generated = field_mod.generate_field(entries, muted, size=100, seed=0)
        assert not any(benched in lineup.players for lineup in generated)

    def test_realized_rates_reflect_the_roster_shape(self, pool, entries, ownership):
        generated = field_mod.generate_field(entries, ownership, size=40, seed=0)
        realized = field_mod.realized_ownership(generated, pool)
        assert realized["cpt_pct"].sum() == pytest.approx(1.0)
        assert realized["flex_pct"].sum() == pytest.approx(showdown.FLEX_SLOTS)

    def test_realized_ownership_needs_a_field(self, pool):
        with pytest.raises(field_mod.FieldError, match="field is empty"):
            field_mod.realized_ownership([], pool)


class TestShowdownRules:
    def test_lineup_salary_applies_the_captain_multiplier(self):
        salary = {"a": 10000.0, "b": 5000.0, "c": 5000.0,
                  "d": 5000.0, "e": 5000.0, "f": 5000.0}
        lineup = showdown.Lineup("a", ("b", "c", "d", "e", "f"))
        assert showdown.lineup_salary(lineup, salary) == 15000 + 25000

    def test_lineup_points_applies_the_captain_multiplier(self):
        points = dict.fromkeys("abcdef", 10.0)
        lineup = showdown.Lineup("a", tuple("bcdef"))
        assert showdown.lineup_points(lineup, points) == 10 * 1.5 + 50

    def test_over_cap_is_illegal(self):
        salary = dict.fromkeys("abcdef", 10000.0)
        lineup = showdown.Lineup("a", tuple("bcdef"))
        team = {p: ("SEA" if p in "abc" else "NE") for p in "abcdef"}
        with pytest.raises(showdown.IllegalLineup, match="exceeds"):
            showdown.check_lineup(lineup, salary, team)

    def test_one_team_is_illegal(self):
        salary = dict.fromkeys("abcdef", 5000.0)
        team = dict.fromkeys("abcdef", "SEA")
        with pytest.raises(showdown.IllegalLineup, match="only one team"):
            showdown.check_lineup(showdown.Lineup("a", tuple("bcdef")), salary, team)

    def test_duplicate_player_is_illegal(self):
        salary = dict.fromkeys("abcdef", 5000.0)
        team = {p: ("SEA" if p in "abc" else "NE") for p in "abcdef"}
        with pytest.raises(showdown.IllegalLineup, match="appears twice"):
            showdown.check_lineup(showdown.Lineup("a", ("a", "c", "d", "e", "f")),
                                  salary, team)

    def test_key_ignores_flex_order(self):
        # Duplication counting (T7) must treat reordered FLEX as the same entry.
        a = showdown.Lineup("x", ("a", "b", "c", "d", "e"))
        b = showdown.Lineup("x", ("e", "d", "c", "b", "a"))
        assert a.key() == b.key()


class TestChalkCluster:
    """T15: a share of the field converges on the same few builds."""

    def two(self, entries, ownership):
        # Two legal lineups from the plain sampler, used as the cluster.
        return field_mod.generate_field(entries, ownership, size=2, seed=9)

    def cluster(self, entries, ownership):
        from collections import Counter
        a, b = self.two(entries, ownership)
        return Counter({a: 3, b: 1})

    def test_the_cluster_share_is_present_and_untouched(self, pool, entries, ownership):
        from src.field import field_shape
        chalk = self.cluster(entries, ownership)
        a = self.two(entries, ownership)[0]
        field = field_mod.generate_field(pool, ownership, size=200, seed=1, chalk=chalk, chalk_share=0.25)
        assert len(field) == 200
        keys = [lu.key() for lu in field]
        top = max(set(keys), key=keys.count)
        assert top == a.key()
        # 50 cluster lineups split ~3:1 -> the top build is ~19% of the field
        assert 0.12 <= field_shape(field)["top_build_share"] <= 0.25

    def test_no_cluster_reproduces_the_plain_sampler(self, pool, entries, ownership):
        plain = field_mod.generate_field(pool, ownership, size=60, seed=3)
        same = field_mod.generate_field(pool, ownership, size=60, seed=3, chalk=None, chalk_share=0.0)
        assert [lu.key() for lu in plain] == [lu.key() for lu in same]

    def test_every_lineup_is_still_legal_and_ownership_still_tracks(self, pool, entries, ownership):
        chalk = self.cluster(entries, ownership)
        field = field_mod.generate_field(pool, ownership, size=300, seed=2, chalk=chalk, chalk_share=0.2)
        salary = dict(zip(pool.player_id, pool.salary)); team = dict(zip(pool.player_id, pool.team))
        assert all(showdown.is_legal(lu, salary, team) for lu in field)
        got = field_mod.realized_ownership(field, pool).set_index("player_id")
        want = ownership.set_index("player_id")
        err = (got.loc[want.index, "total_pct"] - want["cpt_pct"] - want["flex_pct"]).abs()
        assert err.mean() < 0.08

    def test_bad_share_or_empty_cluster_is_refused(self, pool, entries, ownership):
        with pytest.raises(field_mod.FieldError, match="chalk_share"):
            field_mod.generate_field(pool, ownership, size=10, chalk=self.cluster(entries, ownership), chalk_share=1.5)
        with pytest.raises(field_mod.FieldError, match="non-empty"):
            field_mod.generate_field(pool, ownership, size=10, chalk=None, chalk_share=0.3)

    def test_an_illegal_chalk_lineup_is_refused(self, pool, entries, ownership):
        from collections import Counter
        a = self.two(entries, ownership)[0]
        bad = showdown.Lineup(a.captain, a.flex[:4] + (a.captain,))
        with pytest.raises(field_mod.FieldError, match="not legal"):
            field_mod.generate_field(pool, ownership, size=10, chalk=Counter({bad: 1}), chalk_share=0.5)

    def test_chalk_builds_come_from_the_optimizer_with_projections(self, pool):
        from src.field import chalk_builds
        rich = pool.copy()
        rich["projection"] = [20.0 - i for i in range(len(rich))]
        rich["name"] = rich["player_id"]; rich["position"] = "WR"
        builds = chalk_builds(rich, runs=20, jitter=0.0, seed=0)
        assert sum(builds.values()) == 20 and len(builds) == 1     # no jitter: one build
        with pytest.raises(field_mod.FieldError, match="projection"):
            chalk_builds(pool.drop(columns="projection"), runs=5)

    def test_field_shape_metrics(self, entries, ownership):
        from src.field import field_shape
        a, b = self.two(entries, ownership)
        shape = field_shape([a, a, b])
        assert shape["entries"] == 3
        assert shape["distinct_share"] == pytest.approx(2 / 3)
        assert shape["top_build_share"] == pytest.approx(2 / 3)
        assert shape["top5_share"] == pytest.approx(1.0)

