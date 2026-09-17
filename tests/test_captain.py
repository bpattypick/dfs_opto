"""T19: captain selection by floor and ceiling."""

from __future__ import annotations

import itertools

import numpy as np
import pandas as pd
import pytest

from src import captain
from src.captain import (CaptainError, best_complement, captain_board, captain_candidates,
                         player_quantiles, rank_lineups)
from src.scoremodel import CorrelatedScores
from src.showdown import CAPTAIN_MULTIPLIER, Lineup, is_legal


def pool_frame(rows):
    return pd.DataFrame(rows, columns=["player_id", "name", "team", "position", "salary", "projection"])


@pytest.fixture
def pool():
    # Two teams, DEN and KC. Mahomes and Dobbins share a salary and a projection:
    # the QB's fitted sd is much smaller than the RB's at that level.
    return pool_frame([
        ("qb_kc", "Mahomes", "KC", "QB", 11000, 20.0),
        ("rb_den", "Dobbins", "DEN", "RB", 11000, 20.0),
        ("qb_den", "Nix", "DEN", "QB", 10000, 18.0),
        ("wr_kc", "Rice", "KC", "WR", 9000, 15.0),
        ("wr_den", "Sutton", "DEN", "WR", 8000, 12.0),
        ("te_kc", "Kelce", "KC", "TE", 7000, 10.0),
        ("rb_kc", "Walker", "KC", "RB", 8500, 13.0),
        ("wr2_den", "Franklin", "DEN", "WR", 4000, 6.0),
        ("wr2_kc", "Worthy", "KC", "WR", 6000, 9.0),
        ("dst_kc", "Chiefs", "KC", "DST", 3500, 7.0),
        ("dst_den", "Broncos", "DEN", "DST", 3500, 6.0),
        ("k_den", "Lutz", "DEN", "K", 3000, 8.0),
        ("bench", "Backup", "KC", "QB", 2000, 0.0),
    ])


class TestPlayerQuantiles:
    def test_quantiles_are_ordered_and_the_median_sits_below_the_mean(self, pool):
        q = player_quantiles(pool).set_index("player_id")
        for pid in ("qb_kc", "rb_den", "wr_kc"):
            assert q.loc[pid, "p10"] < q.loc[pid, "p25"] < q.loc[pid, "p50"] < q.loc[pid, "p75"] < q.loc[pid, "p90"]
            assert q.loc[pid, "p50"] < q.loc[pid, "projection"]       # right-skewed

    def test_same_mean_different_position_means_different_floor_and_ceiling(self, pool):
        # This is the Dobbins lesson in one row: same projection, wider spread.
        q = player_quantiles(pool).set_index("player_id")
        assert q.loc["rb_den", "sd"] > q.loc["qb_kc", "sd"]
        assert q.loc["rb_den", "p10"] < q.loc["qb_kc", "p10"]
        assert q.loc["rb_den", "p90"] > q.loc["qb_kc", "p90"]

    def test_a_zero_projection_does_not_crash(self, pool):
        q = player_quantiles(pool).set_index("player_id")
        assert q.loc["bench", "p90"] < 0.5

    def test_unknown_quantile_and_bad_pool_are_refused(self, pool):
        with pytest.raises(CaptainError, match="z-score"):
            player_quantiles(pool, quantiles=(0.33,))
        with pytest.raises(CaptainError, match="missing"):
            player_quantiles(pool.drop(columns="salary"))
        with pytest.raises(CaptainError, match="duplicate"):
            player_quantiles(pd.concat([pool, pool.head(1)]))


class TestBestComplement:
    def brute_force(self, cpt, pool):
        p = pool.set_index("player_id")
        budget = 50_000 - CAPTAIN_MULTIPLIER * p.loc[cpt, "salary"]
        best, best_pts = None, -1
        for combo in itertools.combinations([i for i in p.index if i != cpt], 5):
            if p.loc[list(combo), "salary"].sum() > budget:
                continue
            if not any(p.loc[c, "team"] != p.loc[cpt, "team"] for c in combo):
                continue
            pts = p.loc[list(combo), "projection"].sum()
            if pts > best_pts:
                best, best_pts = combo, pts
        return Lineup(cpt, tuple(sorted(best))), best_pts

    def test_matches_an_exhaustive_search_and_is_legal(self, pool):
        salary = dict(zip(pool.player_id, pool.salary)); team = dict(zip(pool.player_id, pool.team))
        for cpt in ("qb_kc", "rb_den", "wr_kc", "dst_den"):
            got = best_complement(cpt, pool)
            want, _ = self.brute_force(cpt, pool)
            assert got == want
            assert is_legal(got, salary, team)

    def test_both_teams_rule_is_enforced(self):
        # Everyone but one player is on KC; the complement must include him.
        p = pool_frame([("c", "C", "KC", "QB", 5000, 30.0)] +
                       [(f"k{i}", f"K{i}", "KC", "WR", 5000, 20.0 - i) for i in range(6)] +
                       [("d", "D", "DEN", "WR", 5000, 1.0)])
        built = best_complement("c", p)
        assert "d" in built.flex

    def test_priced_out_captain_returns_none(self, pool):
        p = pool.copy(); p.loc[p.player_id == "qb_kc", "salary"] = 32_000   # 48k at 1.5x
        assert best_complement("qb_kc", p) is None

    def test_unknown_captain_is_refused(self, pool):
        with pytest.raises(CaptainError, match="not in the pool"):
            best_complement("nobody", pool)

    def test_a_cheap_filler_outside_the_top_n_is_still_reachable(self):
        # Thirty-two good-but-expensive players and one cheap filler needed for the cap.
        rows = [("c", "C", "KC", "QB", 10000, 25.0)]
        rows += [(f"e{i}", f"E{i}", "DEN" if i % 2 else "KC", "WR", 8600, 15.0 - i * 0.01) for i in range(32)]
        rows += [("filler", "F", "DEN", "WR", 500, 0.1)]
        built = best_complement("c", pool_frame(rows), pool_size=30)
        assert built is not None and "filler" in built.flex


class TestCandidatesAndRanking:
    def test_one_lineup_per_candidate_captain_and_the_mean_leader_is_always_there(self, pool):
        lineups = captain_candidates(pool, objective="gpp", n_captains=3)
        captains = [l.captain for l in lineups]
        assert len(captains) == len(set(captains))
        assert "qb_kc" in captains or "rb_den" in captains         # the two mean leaders
        assert all(len(l.flex) == 5 for l in lineups)

    def test_objective_changes_which_captains_lead(self, pool):
        gpp = captain_candidates(pool, objective="gpp", n_captains=1)[0].captain
        cash = captain_candidates(pool, objective="cash", n_captains=1)[0].captain
        assert gpp == "rb_den"      # widest ceiling at the top projection
        assert cash == "qb_kc"      # highest floor at the top projection

    def test_unknown_objective_is_refused(self, pool):
        with pytest.raises(CaptainError, match="objective"):
            captain_candidates(pool, objective="yolo")

    def test_ranking_applies_the_captain_multiplier_on_deterministic_scores(self, pool):
        players = pool.player_id.tolist()
        pts = dict(zip(players, pool.projection))
        constant = lambda rng, n: np.tile(pool.projection.to_numpy(dtype=float), (n, 1))
        lu = Lineup("qb_kc", ("qb_den", "wr_kc", "wr_den", "te_kc", "dst_den"))
        out = rank_lineups([lu], players, constant, trials=100).table.iloc[0]
        expected = 1.5 * pts["qb_kc"] + sum(pts[p] for p in lu.flex)
        assert out["mean"] == pytest.approx(expected)
        assert out["p10"] == pytest.approx(expected) and out["p90"] == pytest.approx(expected)

    def test_cash_prefers_the_steady_captain_and_gpp_the_volatile_one(self):
        # Same five FLEX, same captain projection; only the captain's spread
        # differs (QB sd 8.6 vs RB sd 10.0 at 20 points, times 1.5). The FLEX
        # are other-team RBs and kickers, which the correlation table treats
        # as independent of both captains, so nothing but the marginal moves.
        p = pool_frame([("qb", "QB", "KC", "QB", 11000, 20.0),
                        ("rb", "RB", "KC", "RB", 11000, 20.0)] +
                       [(f"f{i}", f"F{i}", "DEN", "RB" if i < 3 else "K", 5000, 8.0) for i in range(5)])
        scores = CorrelatedScores(p[["player_id", "position", "team", "projection"]])
        flex = tuple(f"f{i}" for i in range(5))
        steady, volatile = Lineup("qb", flex), Lineup("rb", flex)
        ranked = rank_lineups([steady, volatile], scores.players, scores, trials=8000, seed=1)
        t = ranked.table.set_index("captain")
        assert t.loc["qb", "mean"] == pytest.approx(t.loc["rb", "mean"], rel=0.03)
        assert ranked.ordered("cash").iloc[0]["captain"] == "qb"
        assert ranked.ordered("gpp").iloc[0]["captain"] == "rb"

    def test_lineup_quantiles_see_correlation_not_just_the_captains_marginal(self, pool):
        # Mahomes with his own pass-catchers in the FLEX is a wider lineup than
        # Dobbins with the same FLEX, whatever the two marginals say alone.
        scores = CorrelatedScores(pool[["player_id", "position", "team", "projection"]])
        flex = ("wr_kc", "te_kc", "wr2_kc", "dst_den", "k_den")
        ranked = rank_lineups([Lineup("qb_kc", flex), Lineup("rb_den", flex)],
                              scores.players, scores, trials=8000, seed=2).table.set_index("captain")
        spread = ranked["p90"] - ranked["p10"]
        assert spread["qb_kc"] > spread["rb_den"]

    def test_board_carries_captain_and_lineup_distributions(self, pool):
        scores = CorrelatedScores(pool[["player_id", "position", "team", "projection"]])
        board = captain_board(pool, scores.players, scores, objective="cash", n_captains=4,
                              trials=1000, seed=0)
        assert {"captain", "mean", "p10", "p25", "p50", "p90", "cpt_p10", "cpt_p90",
                "cpt_sd", "cpt_position"} <= set(board.columns)
        assert (board["p10"] <= board["p50"]).all() and (board["p50"] <= board["p90"]).all()
        assert list(board["p25"]) == sorted(board["p25"], reverse=True)

    def test_ranking_refuses_nothing_and_too_few_trials(self, pool):
        with pytest.raises(CaptainError, match="no lineups"):
            rank_lineups([], [], lambda r, n: None)
        with pytest.raises(CaptainError, match="trials"):
            rank_lineups([Lineup("a", ("b",) * 5)], ["a", "b"], lambda r, n: None, trials=5)
