"""Crosswalk tests (spec §5.2), including each named landmine."""

from __future__ import annotations

import pandas as pd
import pytest

from src import db
from src import teams as teams_mod
from src.ingest import crosswalk


class TestNormalizeName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("D.J. Moore", "dj moore"),
            ("DJ Moore", "dj moore"),
            ("Odell Beckham Jr.", "odell beckham"),
            ("Michael Pittman Jr", "michael pittman"),
            ("Marvin Harrison Jr.", "marvin harrison"),
            ("Robert Griffin III", "robert griffin"),
            ("  Amon-Ra   St. Brown ", "amonra st brown"),
            ("Ja'Marr Chase", "jamarr chase"),
            ("Kenneth Walker III", "kenneth walker"),
            (None, ""),
        ],
    )
    def test_normalization(self, raw, expected):
        assert crosswalk.normalize_name(raw) == expected

    def test_a_name_that_is_only_a_suffix_is_preserved(self):
        # Never strip the last remaining token — "Vince Young V" must not
        # become "vince young" only to collide with someone else, and a
        # single-token name must survive intact.
        assert crosswalk.normalize_name("V") == "v"


class TestTeamNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("OAK", "LV"),      # relocation inside the backtest window
            ("LV", "LV"),
            ("JAC", "JAX"),
            ("WSH", "WAS"),
            ("LA", "LAR"),
            ("STL", "LAR"),
            ("SD", "LAC"),
            ("kc", "KC"),
            ("", None),
            (None, None),
        ],
    )
    def test_aliases(self, raw, expected):
        assert teams_mod.normalize_team(raw) == expected

    def test_all_32_codes_round_trip(self):
        for team in teams_mod.TEAMS:
            assert teams_mod.normalize_team(team.abbr) == team.abbr


class TestDstNameMap:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Chiefs", "KC"),
            ("Kansas City Chiefs", "KC"),
            ("Kansas City", "KC"),
            ("49ers", "SF"),
            ("San Francisco 49ers", "SF"),
            ("Bills D/ST", "BUF"),
            ("Ravens DST", "BAL"),
            ("Washington Football Team", "WAS"),
            ("Washington Redskins", "WAS"),
            ("Commanders", "WAS"),
            ("Oakland Raiders", "LV"),
            ("NY Giants", "NYG"),
            ("NY Jets", "NYJ"),
        ],
    )
    def test_resolves(self, raw, expected):
        assert teams_mod.dst_team_from_name(raw) == expected

    @pytest.mark.parametrize("ambiguous", ["New York", "Los Angeles", "NY", "LA"])
    def test_ambiguous_cities_refuse_to_resolve(self, ambiguous):
        # Two teams share each of these. Guessing here is exactly the silent
        # bad join the spec warns about.
        assert teams_mod.dst_team_from_name(ambiguous) is None

    def test_every_team_has_a_dst_id(self):
        ids = {teams_mod.dst_player_id(t.abbr) for t in teams_mod.TEAMS}
        assert len(ids) == 32
        assert "DST_KC" in ids


@pytest.fixture()
def reference() -> pd.DataFrame:
    """A small week-of-the-season roster to match against."""
    rows = [
        ("00-0000001", "D.J. Moore", "CHI", "WR"),
        ("00-0000002", "Josh Allen", "BUF", "QB"),
        ("00-0000003", "Josh Allen", "JAX", "LB"),      # the two-Josh-Allens landmine
        ("00-0000004", "Amon-Ra St. Brown", "DET", "WR"),
        ("00-0000005", "Christian McCaffrey", "SF", "RB"),
        ("00-0000006", "Michael Pittman Jr.", "IND", "WR"),
        ("00-0000007", "Travis Etienne", "JAX", "RB"),
        ("DST_KC", "Kansas City Chiefs", "KC", "DST"),
        ("DST_BUF", "Buffalo Bills", "BUF", "DST"),
    ]
    ref = pd.DataFrame(rows, columns=["player_id", "name", "team", "position"])
    ref["norm_name"] = ref["name"].map(crosswalk.normalize_name)
    ref["position"] = ref["position"].map(crosswalk.normalize_position)
    return ref


def _rows(records: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(records)


class TestWaterfall:
    def test_exact_match_on_name_team_position(self, reference):
        result = crosswalk.resolve(
            _rows([{"source_name": "D.J. Moore", "team": "CHI", "position": "WR"}]),
            reference, source="dk",
        )
        assert result.matched.iloc[0]["player_id"] == "00-0000001"
        assert result.matched.iloc[0]["match_method"] == "exact"

    def test_punctuation_and_suffix_differences_still_match_exactly(self, reference):
        result = crosswalk.resolve(
            _rows([
                {"source_name": "DJ Moore", "team": "CHI", "position": "WR"},
                {"source_name": "Michael Pittman", "team": "IND", "position": "WR"},
            ]),
            reference, source="dk",
        )
        assert set(result.matched["player_id"]) == {"00-0000001", "00-0000006"}
        assert set(result.matched["match_method"]) == {"exact"}

    def test_position_disambiguates_duplicate_names(self, reference):
        # Two Josh Allens exist; only position tells them apart.
        result = crosswalk.resolve(
            _rows([
                {"source_name": "Josh Allen", "team": "BUF", "position": "QB"},
                {"source_name": "Josh Allen", "team": "JAX", "position": "LB"},
            ]),
            reference, source="dk",
        )
        got = dict(zip(result.matched["team"], result.matched["player_id"]))
        assert got == {"BUF": "00-0000002", "JAX": "00-0000003"}

    def test_traded_player_matches_on_name_and_position(self, reference):
        # Vendor still lists the old team; name+position is unambiguous, so we
        # take it and record the weaker method.
        result = crosswalk.resolve(
            _rows([{"source_name": "Travis Etienne", "team": "CAR", "position": "RB"}]),
            reference, source="vendorA",
        )
        assert result.matched.iloc[0]["player_id"] == "00-0000007"
        assert result.matched.iloc[0]["match_method"] == "exact_name_pos"

    def test_fuzzy_matches_within_team_and_position(self, reference):
        result = crosswalk.resolve(
            _rows([{"source_name": "Chris McCaffrey", "team": "SF", "position": "RB"}]),
            reference, source="vendorA", fuzzy_threshold=70,
        )
        assert result.matched.iloc[0]["player_id"] == "00-0000005"
        assert result.matched.iloc[0]["match_method"] == "fuzzy"

    def test_fuzzy_never_crosses_teams(self, reference):
        # Same misspelling, wrong team -> must NOT match, even at a low cutoff.
        result = crosswalk.resolve(
            _rows([{"source_name": "Chris McCaffrey", "team": "CHI", "position": "RB"}]),
            reference, source="vendorA", fuzzy_threshold=50,
        )
        assert result.matched.empty
        assert len(result.unmatched) == 1

    def test_dst_rows_resolve_through_the_team_map(self, reference):
        result = crosswalk.resolve(
            _rows([
                {"source_name": "Chiefs", "team": "KC", "position": "DST"},
                {"source_name": "Buffalo Bills", "team": "BUF", "position": "DST"},
            ]),
            reference, source="dk",
        )
        assert set(result.matched["player_id"]) == {"DST_KC", "DST_BUF"}
        assert set(result.matched["match_method"]) == {"dst_map"}

    def test_id_map_takes_priority_over_name_matching(self, reference):
        id_map = pd.DataFrame([{"source_id": "9999", "player_id": "00-0000005"}])
        result = crosswalk.resolve(
            # Name is wrong on purpose; the ID must win.
            _rows([{"source_name": "Wrong Name", "source_id": "9999",
                    "team": "SF", "position": "RB"}]),
            reference, source="espn", id_map=id_map,
        )
        assert result.matched.iloc[0]["player_id"] == "00-0000005"
        assert result.matched.iloc[0]["match_method"] == "id_map"

    def test_manual_override_beats_every_other_tier(self, reference):
        manual = pd.DataFrame([{
            "source": "dk", "source_name": "D.J. Moore", "source_id": "",
            "player_id": "00-0000004", "note": "deliberate override",
            "norm_name": crosswalk.normalize_name("D.J. Moore"),
        }])
        result = crosswalk.resolve(
            _rows([{"source_name": "D.J. Moore", "team": "CHI", "position": "WR"}]),
            reference, source="dk", manual=manual,
        )
        assert result.matched.iloc[0]["player_id"] == "00-0000004"
        assert result.matched.iloc[0]["match_method"] == "manual"

    def test_manual_override_for_a_different_source_is_ignored(self, reference):
        manual = pd.DataFrame([{
            "source": "vendorB", "source_name": "D.J. Moore", "source_id": "",
            "player_id": "00-0000004", "note": "",
            "norm_name": crosswalk.normalize_name("D.J. Moore"),
        }])
        result = crosswalk.resolve(
            _rows([{"source_name": "D.J. Moore", "team": "CHI", "position": "WR"}]),
            reference, source="dk", manual=manual,
        )
        assert result.matched.iloc[0]["match_method"] == "exact"

    def test_unknown_player_lands_in_the_manual_queue(self, reference):
        result = crosswalk.resolve(
            _rows([{"source_name": "Nobody Atall", "team": "CHI", "position": "WR"}]),
            reference, source="dk",
        )
        assert result.matched.empty
        assert result.unmatched.iloc[0]["source_name"] == "Nobody Atall"
        assert result.unmatched.iloc[0]["source"] == "dk"

    def test_coverage_reporting(self, reference):
        result = crosswalk.resolve(
            _rows([
                {"source_name": "D.J. Moore", "team": "CHI", "position": "WR"},
                {"source_name": "Nobody Atall", "team": "CHI", "position": "WR"},
            ]),
            reference, source="dk",
        )
        assert result.total == 2
        assert result.coverage == pytest.approx(0.5)
        assert "exact=1" in result.summary()


class TestManualOverrideRoundTrip:
    def test_write_and_reload_unmatched_queue(self, tmp_path):
        rows = pd.DataFrame([{
            "source": "dk", "source_name": "Nobody Atall", "source_id": "",
            "team": "CHI", "position": "WR", "season": 2024, "week": 3,
        }])
        path = crosswalk.write_unmatched(rows, path=tmp_path / "unmatched_review.csv")
        # Writing the same row twice must not duplicate the review queue.
        crosswalk.write_unmatched(rows, path=path)
        assert len(pd.read_csv(path)) == 1

    def test_missing_overrides_file_is_not_an_error(self, tmp_path):
        assert load_empty(tmp_path).empty

    def test_overrides_file_missing_columns_is_rejected(self, tmp_path):
        bad = tmp_path / "manual_overrides.csv"
        bad.write_text("source,source_name\ndk,Foo\n")
        with pytest.raises(ValueError, match="player_id"):
            crosswalk.load_manual_overrides(path=bad)


def load_empty(tmp_path):
    return crosswalk.load_manual_overrides(path=tmp_path / "does_not_exist.csv")


class TestPersistence:
    def test_crosswalk_rows_are_idempotent(self, tmp_path, reference):
        conn = db.connect(tmp_path / "test.sqlite")
        db.create_schema(conn)
        result = crosswalk.resolve(
            _rows([{"source_name": "D.J. Moore", "team": "CHI", "position": "WR"}]),
            reference, source="dk",
        )
        crosswalk.persist(conn, "dk", result.matched)
        crosswalk.persist(conn, "dk", result.matched)
        assert conn.execute("SELECT COUNT(*) FROM id_crosswalk").fetchone()[0] == 1
        conn.close()

    def test_build_reference_reads_only_the_requested_season(self, tmp_path):
        conn = db.connect(tmp_path / "test.sqlite")
        db.create_schema(conn)
        db.upsert(conn, "players", [
            {"player_id": "00-0000001", "name": "D.J. Moore", "position": "WR",
             "first_season": 2018},
        ])
        db.upsert(conn, "player_week_stats", [
            {"player_id": "00-0000001", "season": 2023, "week": 1, "team": "CHI"},
            {"player_id": "00-0000001", "season": 2024, "week": 1, "team": "CHI"},
        ])
        ref = crosswalk.build_reference(conn, season=2023)
        assert list(ref["player_id"]) == ["00-0000001"]
        assert ref.iloc[0]["norm_name"] == "dj moore"
        conn.close()
