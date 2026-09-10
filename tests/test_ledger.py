"""Experiment ledger tests (roadmap v2 Step 1, task T1)."""

from __future__ import annotations

import json
import subprocess

import pytest

from src import db as db_mod, ledger
db = db_mod


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    db.create_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def git_repo(tmp_path):
    """A real one-commit repo — the dirty-tree guard is worth testing for real."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "model.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _add(conn, **overrides):
    kwargs = dict(
        slate_id="2026-w01-showdown-sea-ne",
        contest_type="showdown_gpp",
        lineup=["Drake Maye", "Jaxon Smith-Njigba"],
        model_version="abc1234",
    )
    kwargs.update(overrides)
    return ledger.add_entry(conn, **kwargs)


class TestSchema:
    def test_entries_table_is_part_of_the_main_db(self, conn):
        # The ledger lives beside salaries and stats so entries can be joined
        # against the rest of the pipeline; it is not its own sidecar file.
        assert "entries" in db.TABLES
        assert db.table_counts(conn)["entries"] == 0

    def test_entry_ids_autoincrement(self, conn):
        assert [_add(conn), _add(conn)] == [1, 2]


class TestGitCommit:
    def test_clean_tree_returns_the_short_hash(self, git_repo):
        commit = ledger.git_commit(repo=git_repo)
        assert commit == commit.strip() and "dirty" not in commit
        expected = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=git_repo, text=True
        ).strip()
        assert commit == expected

    def test_dirty_tree_is_refused(self, git_repo):
        # An entry you can't attribute to a code version teaches you nothing.
        (git_repo / "model.py").write_text("x = 2\n")
        with pytest.raises(ledger.LedgerError, match="dirty"):
            ledger.git_commit(repo=git_repo)

    def test_untracked_file_also_counts_as_dirty(self, git_repo):
        (git_repo / "scratch.py").write_text("x = 3\n")
        with pytest.raises(ledger.LedgerError, match="dirty"):
            ledger.git_commit(repo=git_repo)

    def test_allow_dirty_tags_the_hash(self, git_repo):
        (git_repo / "model.py").write_text("x = 2\n")
        assert ledger.git_commit(allow_dirty=True, repo=git_repo).endswith("-dirty")

    def test_non_repo_is_refused(self, tmp_path):
        with pytest.raises(ledger.LedgerError, match="not a git repo"):
            ledger.git_commit(repo=tmp_path)

    def test_non_repo_backfill_is_tagged(self, tmp_path):
        assert ledger.git_commit(allow_dirty=True, repo=tmp_path) == ledger.NO_GIT


class TestParseLineup:
    def test_splits_on_pipes_and_keeps_cpt_first(self):
        assert ledger.parse_lineup("Maye | JSN|Barner") == ["Maye", "JSN", "Barner"]

    @pytest.mark.parametrize("bad", ["", "   ", "Maye||Barner", "Maye|  |Barner"])
    def test_blank_slots_are_rejected(self, bad):
        # A truncated paste would otherwise log as a legitimate short lineup.
        with pytest.raises(ledger.LedgerError):
            ledger.parse_lineup(bad)


class TestAddEntry:
    def test_stores_lineup_as_json_with_cpt_first(self, conn):
        entry_id = _add(conn, lineup=["Maye", "JSN", "Barner"])
        row = conn.execute(
            "SELECT lineup FROM entries WHERE entry_id=?", (entry_id,)
        ).fetchone()
        assert json.loads(row["lineup"]) == ["Maye", "JSN", "Barner"]

    def test_records_the_model_version(self, conn):
        _add(conn, model_version="deadbee")
        assert conn.execute("SELECT model_version FROM entries").fetchone()[0] == "deadbee"

    def test_date_defaults_to_today(self, conn):
        from datetime import date

        _add(conn)
        assert conn.execute("SELECT date FROM entries").fetchone()[0] == (
            date.today().isoformat()
        )

    def test_result_columns_start_empty(self, conn):
        _add(conn)
        row = conn.execute(
            "SELECT actual_score, finish_rank, payout, roi FROM entries"
        ).fetchone()
        assert tuple(row) == (None, None, None, None)


class TestSettleEntry:
    def test_computes_roi_from_the_entry_fee(self, conn):
        entry_id = _add(conn, entry_fee=5.0)
        roi = ledger.settle_entry(
            conn, entry_id, actual_score=112.4, finish_rank=380, payout=12.50
        )
        assert roi == pytest.approx(1.5)
        row = conn.execute(
            "SELECT actual_score, finish_rank, payout, roi FROM entries"
        ).fetchone()
        assert row["actual_score"] == pytest.approx(112.4)
        assert row["finish_rank"] == 380
        assert row["roi"] == pytest.approx(1.5)

    def test_a_miss_is_minus_one_hundred_percent(self, conn):
        entry_id = _add(conn, entry_fee=5.0)
        assert ledger.settle_entry(conn, entry_id, actual_score=80.0) == pytest.approx(-1.0)

    @pytest.mark.parametrize("fee", [0.0, None])
    def test_free_entries_have_no_roi_but_keep_their_rank(self, conn, fee):
        # Free entries still produce finish-rank data worth keeping.
        entry_id = _add(conn, entry_fee=fee)
        assert ledger.settle_entry(
            conn, entry_id, actual_score=90.0, finish_rank=12
        ) is None
        assert conn.execute("SELECT finish_rank FROM entries").fetchone()[0] == 12

    def test_unknown_entry_is_refused(self, conn):
        with pytest.raises(ledger.LedgerError, match="no entry 99"):
            ledger.settle_entry(conn, 99, actual_score=1.0)


class TestReport:
    def test_empty_ledger_says_so(self, conn):
        assert "No entries logged yet." in ledger.format_report(ledger.report_rows(conn))

    def test_the_season_metric_heads_every_report(self, conn):
        # Roadmap Step 0: the metric sits at the top of the ledger so it is read
        # every time results are, rather than remembered.
        assert ledger.format_report([]).startswith(ledger.SUCCESS_METRIC)
        _add(conn, entry_fee=5.0)
        assert ledger.format_report(ledger.report_rows(conn)).startswith(
            ledger.SUCCESS_METRIC
        )

    def test_progress_tracks_the_closest_cell_not_the_total(self, conn):
        # Entries spread across contest types never produce a conclusion, so the
        # total would flatter the position.
        for _ in range(3):
            _add(conn, contest_type="showdown_cash")
        _add(conn, contest_type="showdown_gpp")
        report = ledger.format_report(ledger.report_rows(conn))
        assert f"3/{ledger.MIN_N} in showdown_cash" in report

    def test_reaching_n_is_reported_as_such(self, conn):
        for _ in range(ledger.MIN_N):
            _add(conn, contest_type="showdown_cash")
        assert "At N: showdown_cash" in ledger.format_report(ledger.report_rows(conn))

    def test_groups_by_contest_type_and_model_version(self, conn):
        _add(conn, contest_type="showdown_gpp", model_version="aaa")
        _add(conn, contest_type="showdown_gpp", model_version="aaa")
        _add(conn, contest_type="showdown_cash", model_version="bbb")
        rows = ledger.report_rows(conn)
        assert [(r["contest_type"], r["model_version"], r["n"]) for r in rows] == [
            ("showdown_cash", "bbb", 1),
            ("showdown_gpp", "aaa", 2),
        ]

    def test_roi_is_net_of_everything_staked(self, conn):
        for payout in (0.0, 30.0):
            entry_id = _add(conn, entry_fee=10.0)
            ledger.settle_entry(conn, entry_id, actual_score=100.0, payout=payout)
        row = ledger.report_rows(conn)[0]
        assert row["staked"] == pytest.approx(20.0)
        assert row["returned"] == pytest.approx(30.0)
        assert "+50.0%" in ledger.format_report([row])

    def test_pending_entries_are_counted_separately(self, conn):
        entry_id = _add(conn, entry_fee=5.0)
        _add(conn, entry_fee=5.0)
        ledger.settle_entry(conn, entry_id, actual_score=100.0, payout=10.0)
        row = ledger.report_rows(conn)[0]
        assert (row["n"], row["pending"], row["cashes"]) == (2, 1, 1)
        # cash% is over settled entries only, so one-for-one reads 100%.
        assert "100%" in ledger.format_report([row])

    def test_entries_missing_a_dup_estimate_are_called_out(self, conn):
        _add(conn, entry_fee=5.0)                      # no dup_estimate
        _add(conn, entry_fee=5.0, dup_estimate=0.02)
        report = ledger.format_report(ledger.report_rows(conn))
        assert "1 entry logged without a duplication estimate" in report

    def test_no_callout_when_every_entry_has_one(self, conn):
        _add(conn, entry_fee=5.0, dup_estimate=0.02)
        assert "without a duplication estimate" not in ledger.format_report(
            ledger.report_rows(conn)
        )

    def test_manual_entries_do_not_count_toward_the_metric(self, conn):
        # H1's metric is entries "attributable to a commit". A hand-built lineup
        # is a legitimate ROI data point and no model can be credited for it.
        for _ in range(3):
            _add(conn, model_version=ledger.MANUAL, entry_fee=5.0, dup_estimate=2)
        report = ledger.format_report(ledger.report_rows(conn))
        assert f"Progress: 0/{ledger.MIN_N}" in report
        assert "no entries are attributable to a commit" in report
        assert "[no commit]" in report

    def test_unattributed_entries_are_excluded_but_disclosed(self, conn):
        _add(conn, model_version="abc1234", entry_fee=5.0, dup_estimate=2)
        _add(conn, model_version=ledger.MANUAL, entry_fee=5.0, dup_estimate=2)
        report = ledger.format_report(ledger.report_rows(conn))
        assert f"Progress: 1/{ledger.MIN_N}" in report
        assert "1 further entry not attributable" in report

    @pytest.mark.parametrize(("version", "ok"), [
        ("abc1234", True), ("4b221390a1b2c3", True),
        ("abc1234-dirty", False), (ledger.MANUAL, False), (ledger.NO_GIT, False),
        ("chat-session-heuristic-sim-no-git-hash", False), ("", False), (None, False),
    ])
    def test_what_counts_as_commit_attributable(self, version, ok):
        assert ledger.is_attributable(version) is ok

    def test_small_samples_are_flagged(self, conn):
        _add(conn, entry_fee=5.0)
        assert "not conclusive" in ledger.format_report(ledger.report_rows(conn))

    def test_at_minimum_n_the_flag_clears(self, conn):
        for _ in range(ledger.MIN_N):
            _add(conn, entry_fee=5.0)
        report = ledger.format_report(ledger.report_rows(conn))
        assert "not conclusive" not in report
        # The standing reminder stays regardless — 50 is a floor, not proof.
        assert f"under n={ledger.MIN_N}" in report


class TestCli:
    def test_add_help_works(self):
        with pytest.raises(SystemExit) as exc:
            ledger.main(["add", "--help"])
        assert exc.value.code == 0

    def test_add_then_result_then_report(self, tmp_path, capsys):
        db_path = str(tmp_path / "dfs.sqlite")
        rc = ledger.main([
            "--db", db_path, "add",
            "--slate", "2026-w01-showdown-sea-ne", "--contest", "12345",
            "--type", "showdown_gpp", "--field-size", "5000", "--fee", "5",
            "--lineup", "Drake Maye|Jaxon Smith-Njigba|AJ Barner",
            "--sim-mean", "98.8", "--sim-ceiling", "123.8", "--chalk", "134",
            "--dup", "0.0014", "--allow-dirty",
        ])
        assert rc == 0
        assert "entry 1 logged" in capsys.readouterr().out

        assert ledger.main([
            "--db", db_path, "result", "--entry-id", "1",
            "--score", "112.4", "--rank", "380", "--payout", "12.50",
        ]) == 0
        assert "ROI +150.0%" in capsys.readouterr().out

        assert ledger.main(["--db", db_path, "report"]) == 0
        out = capsys.readouterr().out
        assert "showdown_gpp" in out and "+150.0%" in out

    def test_dirty_tree_exits_nonzero_and_writes_nothing(self, tmp_path, capsys, monkeypatch):
        db_path = tmp_path / "dfs.sqlite"

        def refuse(*args, **kwargs):
            raise ledger.LedgerError("working tree is dirty.")

        monkeypatch.setattr(ledger, "git_commit", refuse)
        rc = ledger.main([
            "--db", str(db_path), "add", "--slate", "s", "--type", "showdown_gpp",
            "--lineup", "Maye|JSN", "--dup", "0.5",
        ])
        assert rc == 1
        assert "dirty" in capsys.readouterr().err

        conn = db.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        conn.close()

    def test_a_variant_contest_type_is_refused(self, tmp_path, capsys):
        # "gpp" and "showdown_gpp" are one contest to a human and two rows to
        # SQL, which silently halves progress toward the season metric.
        rc = ledger.main([
            "--db", str(tmp_path / "d.sqlite"), "add", "--slate", "s",
            "--type", "gpp", "--lineup", "Maye|JSN", "--dup", "2",
            "--allow-dirty",
        ])
        assert rc == 1
        err = capsys.readouterr().err
        assert "showdown_gpp" in err and "neither reaches N" in err

    def test_model_version_override_skips_the_git_check(self, tmp_path, capsys):
        # A hand-built lineup has no commit to point at; stamping the checked-out
        # one would credit code that had no part in it.
        db = tmp_path / "d.sqlite"
        rc = ledger.main([
            "--db", str(db), "add", "--slate", "s", "--type", "showdown_gpp",
            "--lineup", "Maye|JSN", "--dup", "2",
            "--model-version", ledger.MANUAL,
        ])
        assert rc == 0
        conn = db_mod.connect(db)
        assert conn.execute("SELECT model_version FROM entries").fetchone()[0] == (
            ledger.MANUAL
        )
        conn.close()

    def test_force_type_allows_a_genuinely_new_type(self, tmp_path, capsys):
        rc = ledger.main([
            "--db", str(tmp_path / "d.sqlite"), "add", "--slate", "s",
            "--type", "showdown_satellite", "--lineup", "Maye|JSN", "--dup", "2",
            "--force-type", "--allow-dirty",
        ])
        assert rc == 0

    def test_add_without_a_dup_estimate_is_refused(self, tmp_path, capsys):
        # T7: an entry with no duplication estimate can never be checked against
        # the contest's real standings.
        rc = ledger.main([
            "--db", str(tmp_path / "d.sqlite"), "add", "--slate", "s",
            "--type", "showdown_gpp", "--lineup", "Maye|JSN", "--allow-dirty",
        ])
        assert rc == 1
        assert "--dup" in capsys.readouterr().err

    def test_no_dup_flag_allows_a_backfill(self, tmp_path, capsys):
        rc = ledger.main([
            "--db", str(tmp_path / "d.sqlite"), "add", "--slate", "s",
            "--type", "showdown_gpp", "--lineup", "Maye|JSN", "--no-dup",
            "--allow-dirty",
        ])
        assert rc == 0
        assert "entry 1 logged" in capsys.readouterr().out

    def test_result_for_unknown_entry_exits_nonzero(self, tmp_path, capsys):
        rc = ledger.main([
            "--db", str(tmp_path / "dfs.sqlite"), "result",
            "--entry-id", "42", "--score", "100",
        ])
        assert rc == 1
        assert "no entry 42" in capsys.readouterr().err
