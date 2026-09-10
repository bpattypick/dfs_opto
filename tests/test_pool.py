"""Showdown pool construction and status filtering (task T12)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src import pool as pool_mod
from src.pool import PoolError, build_pool


def salaries(**overrides):
    rows = [
        # name, team, salary, avg_points, status
        ("Jaxon Smith-Njigba", "SEA", 10600, 21.99, ""),
        ("Drake Maye",         "NE",  10000, 20.90, ""),
        ("Zach Charbonnet",    "SEA",  8200, 10.96, "OUT"),
        ("Jason Myers",        "SEA",  5400, 11.95, ""),
        ("Hunter Henry",       "NE",   4800,  9.90, ""),
        ("Tory Horton",        "SEA",  2800,  8.14, "Q"),
        ("Patriots",           "NE",   3400,  8.38, "IR"),
    ]
    frame = pd.DataFrame({
        "source_id": [f"id{i}" for i in range(len(rows))],
        "player_id": [f"00-{i:05d}" for i in range(len(rows))],
        "dk_name": [r[0] for r in rows],
        "team": [r[1] for r in rows],
        "dk_salary": [r[2] for r in rows],
        "avg_points": [r[3] for r in rows],
        "status": [r[4] for r in rows],
    })
    for column, value in overrides.items():
        frame[column] = value
    return frame


class TestStatusFiltering:
    def test_out_and_ir_are_excluded(self):
        # The optimizer loves an OUT player: real projection, discounted salary.
        result = build_pool(salaries())
        assert "Zach Charbonnet" not in set(result["name"])
        assert "Patriots" not in set(result["name"])
        assert len(result) == 5

    def test_questionable_players_stay_in(self):
        # Q players usually play, and they are often under-owned — excluding
        # them throws away leverage.
        assert "Tory Horton" in set(build_pool(salaries())["name"])

    @pytest.mark.parametrize("status", ["OUT", "IR", "SUSP", "NA"])
    def test_every_unavailable_status_is_filtered(self, status):
        frame = salaries()
        frame.loc[frame["dk_name"] == "Jason Myers", "status"] = status
        assert "Jason Myers" not in set(build_pool(frame)["name"])

    def test_lowercase_status_is_still_filtered(self):
        frame = salaries()
        frame.loc[frame["dk_name"] == "Jason Myers", "status"] = "out"
        assert "Jason Myers" not in set(build_pool(frame)["name"])

    def test_include_unavailable_keeps_everyone(self):
        assert len(build_pool(salaries(), include_unavailable=True)) == 7

    def test_a_slate_with_nobody_available_fails_loudly(self):
        with pytest.raises(PoolError, match="every player"):
            build_pool(salaries(status="OUT"))

    def test_missing_status_column_is_tolerated(self):
        # Older archived exports may predate the column.
        assert len(build_pool(salaries().drop(columns=["status"]))) == 7


class TestPoolShape:
    def test_returns_the_canonical_columns(self):
        assert list(build_pool(salaries()).columns) == list(pool_mod.POOL_COLUMNS)

    def test_uses_dk_id_by_default(self):
        assert build_pool(salaries())["player_id"].iloc[0] == "id0"

    def test_can_key_on_the_canonical_gsis_id(self):
        # What anything joining across sources needs (CLAUDE.md conventions).
        result = build_pool(salaries(), id_column="player_id")
        assert result["player_id"].iloc[0] == "00-00000"

    def test_negative_projections_are_floored(self):
        frame = salaries()
        frame.loc[frame["dk_name"] == "Jason Myers", "avg_points"] = -0.2
        result = build_pool(frame).set_index("name")
        assert result.loc["Jason Myers", "projection"] == 0.0

    def test_missing_projection_fails_loudly(self):
        frame = salaries()
        frame.loc[frame["dk_name"] == "Drake Maye", "avg_points"] = None
        with pytest.raises(PoolError, match="no avg_points"):
            build_pool(frame)

    def test_blank_id_fails_loudly(self):
        # Blank ids would collide and silently merge two players into one entry.
        frame = salaries()
        frame.loc[0, "source_id"] = None
        with pytest.raises(PoolError, match="missing source_id"):
            build_pool(frame)

    def test_duplicate_ids_fail_loudly(self):
        frame = salaries()
        frame.loc[1, "source_id"] = frame.loc[0, "source_id"]
        with pytest.raises(PoolError, match="duplicate player_id"):
            build_pool(frame)

    def test_missing_columns_are_named(self):
        with pytest.raises(PoolError, match="dk_salary"):
            build_pool(salaries().drop(columns=["dk_salary"]))

    def test_empty_input_is_refused(self):
        with pytest.raises(PoolError, match="no salary rows"):
            build_pool(salaries().iloc[0:0])


class TestQuestionable:
    def test_lists_questionable_players(self):
        assert pool_mod.questionable(salaries())["dk_name"].tolist() == ["Tory Horton"]

    def test_empty_when_the_column_is_absent(self):
        assert pool_mod.questionable(salaries().drop(columns=["status"])).empty


class TestStatusChanges:
    def test_detects_a_player_ruled_out(self):
        # The cheapest form of roadmap Step 4: poll the export and diff.
        before = salaries()
        after = before.copy()
        after.loc[after["dk_name"] == "Tory Horton", "status"] = "OUT"
        changed = pool_mod.status_changes(before, after)
        assert len(changed) == 1
        assert changed.iloc[0].to_dict() == {
            "dk_name": "Tory Horton", "was": "Q", "now": "OUT"
        }

    def test_no_changes_is_empty(self):
        assert pool_mod.status_changes(salaries(), salaries()).empty

    def test_players_absent_from_one_pull_are_ignored(self):
        after = salaries().iloc[:-1]
        assert pool_mod.status_changes(salaries(), after).empty

    def test_missing_columns_are_refused(self):
        with pytest.raises(PoolError, match="needs dk_name and status"):
            pool_mod.status_changes(salaries(), salaries().drop(columns=["status"]))


class TestAgainstTheRealExport:
    def test_the_real_slate_filters_to_the_expected_pool(self):
        from src.ingest.dk_salaries import parse_dk_export
        from pathlib import Path
        fixture = Path(__file__).parent / "fixtures" / "2026-w01_dk_showdown-ne-sea.csv"
        parsed = parse_dk_export(fixture)
        result = build_pool(parsed)
        assert len(parsed) == 10 and len(result) == 8   # Charbonnet OUT, Patriots IR
        assert set(result["team"]) == {"NE", "SEA"}


class TestFromExport:
    """The live-slate path: export straight to pool, no database."""

    FIXTURE = Path(__file__).parent / "fixtures" / "2026-w01_dk_showdown-ne-sea.csv"

    def test_builds_a_pool_from_a_showdown_export(self):
        result = pool_mod.from_export(self.FIXTURE)
        assert list(result.columns) == list(pool_mod.POOL_COLUMNS)
        assert len(result) == 8              # 10 players less Charbonnet and Patriots
        assert set(result["team"]) == {"NE", "SEA"}

    def test_captain_rows_do_not_leak_into_the_pool(self):
        # A CPT row surviving would double every player at 1.5x salary.
        result = pool_mod.from_export(self.FIXTURE)
        assert result["player_id"].is_unique
        assert result.set_index("name").loc["Jaxon Smith-Njigba", "salary"] == 10600

    def test_projection_comes_from_avg_points(self):
        result = pool_mod.from_export(self.FIXTURE).set_index("name")
        assert result.loc["Drake Maye", "projection"] == pytest.approx(20.90)

    def test_unavailable_players_can_be_kept(self):
        assert len(pool_mod.from_export(self.FIXTURE, include_unavailable=True)) == 10

    def test_the_pool_drives_the_simulation_end_to_end(self):
        # H7's actual acceptance: a real export runs T4/T5/T6 without hand-wiring.
        from src.contest import PayoutTable, independent_normal_scores, simulate_contest
        from src.field import generate_field
        from src.ownership import estimate_ownership

        players = pool_mod.from_export(self.FIXTURE)
        rates = estimate_ownership(players, n=15, seed=0)
        field = generate_field(players[["player_id", "team", "salary"]], rates,
                               size=40, seed=1)
        result = simulate_contest(
            field[0], field[1:], players["player_id"].tolist(),
            independent_normal_scores(players["projection"].to_numpy()),
            PayoutTable.from_tiers([(1, 1, 100.0), (2, 5, 20.0)]),
            entry_fee=5.0, trials=25, seed=2,
        )
        assert 0.0 <= result.cash_rate <= 1.0
        assert result.entries == 40


class TestProjectionOverrides:
    """The owner's read on role changes, committed so it is attributable."""

    def write(self, tmp_path, rows, header=True):
        path = tmp_path / "ov.csv"
        lines = ["slate_id,dk_name,projection,reason"] if header else ["slate_id,dk_name"]
        lines += rows
        path.write_text("\n".join(lines) + "\n")
        return path

    def test_replaces_the_projection(self, tmp_path):
        path = self.write(tmp_path, ['s1,Drake Maye,30.0,"owner: new role"'])
        ov = pool_mod.load_overrides(path, "s1")
        result = pool_mod.apply_overrides(build_pool(salaries()), ov).set_index("name")
        assert result.loc["Drake Maye", "projection"] == 30.0
        # untouched players keep theirs
        assert result.loc["Jason Myers", "projection"] == pytest.approx(11.95)

    def test_only_the_named_slate_applies(self, tmp_path):
        path = self.write(tmp_path, ['other,Drake Maye,30.0,"owner: note"'])
        ov = pool_mod.load_overrides(path, "s1")
        assert ov.empty
        result = pool_mod.apply_overrides(build_pool(salaries()), ov).set_index("name")
        assert result.loc["Drake Maye", "projection"] == pytest.approx(20.90)

    def test_a_misspelled_name_fails_loudly(self, tmp_path):
        # Silently doing nothing would leave a lineup looking reviewed when the
        # override never took effect.
        path = self.write(tmp_path, ['s1,Drake May,30.0,"owner: typo"'])
        ov = pool_mod.load_overrides(path, "s1")
        with pytest.raises(PoolError, match="not in the pool"):
            pool_mod.apply_overrides(build_pool(salaries()), ov)

    def test_an_override_without_a_reason_is_refused(self, tmp_path):
        # Six weeks later an unexplained override is indistinguishable from a bug.
        path = self.write(tmp_path, ['s1,Drake Maye,30.0,'])
        with pytest.raises(PoolError, match="without a reason"):
            pool_mod.load_overrides(path, "s1")

    def test_missing_columns_are_named(self, tmp_path):
        path = self.write(tmp_path, ["s1,Drake Maye"], header=False)
        with pytest.raises(PoolError, match="missing columns"):
            pool_mod.load_overrides(path, "s1")

    def test_no_overrides_is_a_no_op(self):
        base = build_pool(salaries())
        pd.testing.assert_frame_equal(pool_mod.apply_overrides(base, None), base)

    def test_the_committed_overrides_file_loads(self):
        from src import config as config_mod
        path = config_mod.load().path("projection_overrides")
        rows = pool_mod.load_overrides(path, "2026-w01-showdown-sf-lar")
        assert "De'Zhaun Stribling" in set(rows["dk_name"])
