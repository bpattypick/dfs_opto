"""Reading DK standings as lineups (T15's ground truth)."""

from __future__ import annotations

import pandas as pd
import pytest

from src import standings
from src.field import field_shape

CSV = (
    "﻿Rank,EntryId,EntryName,TimeRemaining,Points,Lineup,,Player,Roster Position,%Drafted,FPTS\n"
    "1,111,alpha,0,101.0,CPT Jaxon Smith-Njigba FLEX Drake Maye FLEX Rhamondre Stevenson FLEX Jadarian Price FLEX Seahawks  FLEX Mack Hollins,,Drake Maye,FLEX,59.92%,12.82\n"
    "2,222,beta,0,97.1,CPT Jaxon Smith-Njigba FLEX Drake Maye FLEX Rhamondre Stevenson FLEX Jadarian Price FLEX Seahawks  FLEX Mack Hollins,,Jaxon Smith-Njigba,CPT,20.00%,29.2\n"
    "3,333,gamma,0,90.0,CPT Drake Maye FLEX Jaxon Smith-Njigba FLEX Rhamondre Stevenson FLEX Jason Myers FLEX Seahawks  FLEX Patriots ,,Patriots,FLEX,10.00%,4.0\n"
    ",,,,,,,Rhamondre Stevenson,FLEX,63.70%,12.0\n"
)


@pytest.fixture
def path(tmp_path):
    p = tmp_path / "2026-w01_dk_showdown-ne-sea_contest1.csv"
    p.write_text(CSV, encoding="utf-8")
    return p


@pytest.fixture
def pool():
    names = ["Jaxon Smith-Njigba", "Drake Maye", "Rhamondre Stevenson", "Jadarian Price",
             "Seahawks", "Mack Hollins", "Jason Myers", "Patriots"]
    return pd.DataFrame({"player_id": [f"p{i}" for i in range(len(names))], "name": names})


class TestParse:
    def test_lineup_string_splits_into_captain_and_five_flex(self):
        cpt, flex = standings.parse_lineup_string(
            "CPT Jaxon Smith-Njigba FLEX Drake Maye FLEX Rhamondre Stevenson FLEX Jadarian Price FLEX Seahawks  FLEX Mack Hollins")
        assert cpt == "Jaxon Smith-Njigba"
        assert flex == ("Drake Maye", "Rhamondre Stevenson", "Jadarian Price", "Seahawks", "Mack Hollins")

    def test_a_non_showdown_string_is_refused(self):
        with pytest.raises(standings.StandingsError, match="not a Showdown"):
            standings.parse_lineup_string("QB Lamar Jackson RB Jahmyr Gibbs")

    def test_read_standings_keeps_entries_only(self, path):
        e = standings.read_standings(path)
        assert list(e["rank"]) == [1, 2, 3]
        assert e.iloc[2]["captain"] == "Drake Maye"
        assert e.iloc[2]["flex"][-1] == "Patriots"

    def test_dk_ownership_reads_the_summary_columns(self, path):
        own = standings.dk_ownership(path).set_index("name")
        assert own.loc["Rhamondre Stevenson", "drafted"] == pytest.approx(0.637)
        assert own.loc["Jaxon Smith-Njigba", "roster_position"] == "CPT"

    def test_missing_columns_fail_loudly(self, tmp_path):
        p = tmp_path / "bad.csv"; p.write_text("Rank,Lineup\n1,CPT A\n")
        with pytest.raises(standings.StandingsError, match="missing column"):
            standings.read_standings(p)


class TestToLineups:
    def test_maps_names_to_pool_ids_and_reports_unmatched(self, path, pool):
        e = standings.read_standings(path)
        lineups, unmatched = standings.to_lineups(e, pool)
        assert len(lineups) == 3 and unmatched == []
        assert lineups[0].captain == "p0" and "p4" in lineups[0].flex

    def test_an_entry_with_an_unknown_name_is_dropped_not_guessed(self, path, pool):
        e = standings.read_standings(path)
        lineups, unmatched = standings.to_lineups(e, pool[pool.name != "Jason Myers"])
        assert len(lineups) == 2 and unmatched == ["Jason Myers"]

    def test_field_shape_on_the_real_structure(self, path, pool):
        lineups, _ = standings.to_lineups(standings.read_standings(path), pool)
        shape = field_shape(lineups)
        assert shape["entries"] == 3
        assert shape["distinct_share"] == pytest.approx(2 / 3)
        assert shape["top_build_share"] == pytest.approx(2 / 3)
