"""DK salary parsing and join-coverage tests (spec §5.4, Milestone 3)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src import db
from src.ingest import crosswalk, dk_salaries

FIXTURE = Path(__file__).parent / "fixtures" / "2023-w01_dk_main.csv"


class TestFilenameConvention:
    def test_parses_season_week_and_slate(self):
        meta = dk_salaries.parse_slate_filename("2026-w01_dk_main.csv")
        assert meta == {
            "season": 2026, "week": 1, "slate": "main", "slate_id": "2026-w01-main",
        }

    def test_slate_id_zero_pads_the_week(self):
        assert dk_salaries.parse_slate_filename("2025-w8_dk_main.csv")["slate_id"] == (
            "2025-w08-main"
        )

    def test_non_main_slates_are_supported(self):
        meta = dk_salaries.parse_slate_filename("2025-w12_dk_showdown.csv")
        assert meta["slate"] == "showdown"

    def test_a_showdown_can_name_its_game(self):
        # Every Showdown in a week would otherwise share one slate_id, and the
        # second file loaded would overwrite the first.
        meta = dk_salaries.parse_slate_filename("2026-w01_dk_showdown-ne-sea.csv")
        assert meta["slate"] == "showdown-ne-sea"
        assert meta["slate_id"] == "2026-w01-showdown-ne-sea"

    @pytest.mark.parametrize(
        "bad",
        ["dk_main.csv", "2026-week1_dk_main.csv", "2026-w01_main.csv", "salaries.csv"],
    )
    def test_off_convention_names_are_rejected(self, bad):
        # Silently guessing the week would misfile a whole slate.
        with pytest.raises(dk_salaries.SalaryFormatError):
            dk_salaries.parse_slate_filename(bad)


class TestOpponentParsing:
    @pytest.mark.parametrize(
        ("game_info", "team", "expected"),
        [
            ("BUF@LAR 09/08/2024 08:20PM ET", "BUF", "LAR"),
            ("BUF@LAR 09/08/2024 08:20PM ET", "LAR", "BUF"),
            ("KC@OAK 09/10/2019 01:00PM ET", "KC", "LV"),   # historical alias
            ("BUF@LAR 09/08/2024 08:20PM ET", "SF", None),  # team not in the game
            (None, "BUF", None),
            ("garbage", "BUF", None),
        ],
    )
    def test_opponent_extraction(self, game_info, team, expected):
        assert dk_salaries._opponent_from_game_info(game_info, team) == expected


class TestDkExportParsing:
    def test_parses_the_fixture(self):
        frame = dk_salaries.parse_dk_export(FIXTURE)
        assert len(frame) == 242
        assert frame["season"].unique().tolist() == [2023]
        assert frame["week"].unique().tolist() == [1]
        assert frame["slate_id"].unique().tolist() == ["2023-w01-main"]
        assert frame["dk_salary"].dtype.kind == "i"
        assert frame["dk_salary"].between(2000, 12000).all()

    def test_dst_rows_survive_parsing(self):
        frame = dk_salaries.parse_dk_export(FIXTURE)
        dst = frame[frame["position"] == "DST"]
        assert len(dst) == 26
        assert "Falcons" in set(dst["dk_name"])

    def test_teams_and_opponents_are_normalized(self):
        frame = dk_salaries.parse_dk_export(FIXTURE)
        from src import teams as teams_mod

        assert set(frame["team"]).issubset(teams_mod.VALID_ABBRS)
        assert frame["opponent"].notna().all()
        assert (frame["team"] != frame["opponent"]).all()

    def test_salary_with_currency_formatting(self, tmp_path):
        path = tmp_path / "2025-w01_dk_main.csv"
        path.write_text(
            "Position,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev\n"
            "QB,Josh Allen,1,QB,\"$8,200\",BUF@NYJ 09/07/2025 01:00PM ET,BUF\n"
        )
        assert dk_salaries.parse_dk_export(path)["dk_salary"].iloc[0] == 8200

    def test_contest_preamble_before_the_header_is_skipped(self, tmp_path):
        # Some DK exports prepend a contest-info block.
        path = tmp_path / "2025-w01_dk_main.csv"
        path.write_text(
            "Contest,NFL $1M Play-Action\n"
            "Entries,100000\n"
            "\n"
            "Position,Name,ID,Roster Position,Salary,Game Info,TeamAbbrev\n"
            "QB,Josh Allen,1,QB,8200,BUF@NYJ 09/07/2025 01:00PM ET,BUF\n"
        )
        frame = dk_salaries.parse_dk_export(path)
        assert len(frame) == 1
        assert frame["dk_name"].iloc[0] == "Josh Allen"

    def test_a_file_with_no_salary_header_is_rejected(self, tmp_path):
        path = tmp_path / "2025-w01_dk_main.csv"
        path.write_text("foo,bar\n1,2\n")
        with pytest.raises(dk_salaries.SalaryFormatError):
            dk_salaries.parse_dk_export(path)


class TestRotoguruParsing:
    """The RotoGuru endpoint is unverified (network-blocked); the parser is not."""

    SAMPLE = (
        "Week;Year;GID;Name;Pos;Team;h/a;Oppt;DK points;DK salary\n"
        "1;2023;1234;Allen, Josh;QB;buf;a;nyj;28.5;8200\n"
        "1;2023;1235;Moore, D.J.;WR;chi;h;gb;12.1;6100\n"
    )

    def test_parses_semicolon_export(self):
        frame = dk_salaries.parse_rotoguru(self.SAMPLE, 2023, 1)
        assert len(frame) == 2
        assert frame["slate_id"].iloc[0] == "2023-w01-main"

    def test_last_first_names_are_flipped(self):
        frame = dk_salaries.parse_rotoguru(self.SAMPLE, 2023, 1)
        assert frame["dk_name"].tolist() == ["Josh Allen", "D.J. Moore"]

    def test_lowercase_team_codes_are_normalized(self):
        frame = dk_salaries.parse_rotoguru(self.SAMPLE, 2023, 1)
        assert frame["team"].tolist() == ["BUF", "CHI"]
        assert frame["opponent"].tolist() == ["NYJ", "GB"]

    def test_a_changed_format_fails_loudly(self):
        # Better to raise than to silently backfill garbage salaries.
        with pytest.raises(dk_salaries.SalaryFormatError):
            dk_salaries.parse_rotoguru("<html>site moved</html>", 2023, 1)


def _db_has_2023_week1() -> bool:
    try:
        conn = db.connect()
    except Exception:  # noqa: BLE001
        return False
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM player_week_stats WHERE season=2023 AND week=1"
        ).fetchone()[0]
        return n > 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        conn.close()


@pytest.mark.skipif(
    not _db_has_2023_week1(),
    reason="needs a populated db: python -m src.ingest.nfl_stats --season 2023",
)
class TestJoinCoverage:
    """Milestone 3's acceptance test: >=97% match rate on a DK salary file."""

    def test_meets_the_97_percent_threshold(self):
        conn = db.connect()
        salaries = dk_salaries.parse_dk_export(FIXTURE)
        reference = crosswalk.build_reference(conn, season=2023, week=1)
        result = crosswalk.resolve(
            salaries.rename(columns={"dk_name": "source_name"}),
            reference,
            source="dk",
        )
        conn.close()
        assert result.coverage >= 0.97, result.summary()

    def test_every_defense_resolves_through_the_dst_map(self):
        conn = db.connect()
        salaries = dk_salaries.parse_dk_export(FIXTURE)
        reference = crosswalk.build_reference(conn, season=2023, week=1)
        result = crosswalk.resolve(
            salaries.rename(columns={"dk_name": "source_name"}),
            reference,
            source="dk",
        )
        conn.close()
        dst = result.matched[result.matched["position"] == "DST"]
        assert len(dst) == 26
        assert (dst["match_method"] == "dst_map").all()
        assert dst["player_id"].str.startswith("DST_").all()

    def test_no_row_is_matched_to_a_player_on_another_team(self):
        conn = db.connect()
        salaries = dk_salaries.parse_dk_export(FIXTURE)
        reference = crosswalk.build_reference(conn, season=2023, week=1)
        result = crosswalk.resolve(
            salaries.rename(columns={"dk_name": "source_name"}),
            reference,
            source="dk",
        )
        conn.close()
        # Only exact_name_pos is allowed to disagree on team (traded players).
        strict = result.matched[result.matched["match_method"].isin(["exact", "fuzzy"])]
        ref_team = reference.set_index("player_id")["team"].to_dict()
        mismatched = [
            (r["source_name"], r["team"], ref_team.get(r["player_id"]))
            for _, r in strict.iterrows()
            if ref_team.get(r["player_id"]) != r["team"]
        ]
        assert not mismatched, f"cross-team matches: {mismatched}"
