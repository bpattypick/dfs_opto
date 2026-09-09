"""Experiment ledger — every DFS entry, the code version that built it, and the result.

Why this exists: without it, process improvements and variance are
indistinguishable. GPP results are noisy enough that a few good weeks feel like
edge. The ledger is what turns "I think this is working" into a number
(roadmap v2 Step 1).

Usage:
    python -m src.ledger add --slate 2026-w01-showdown-sea-ne \
        --contest 12345 --type showdown_gpp --field-size 5000 \
        --fee 5 --lineup "Drake Maye|Jaxon Smith-Njigba|AJ Barner|..." \
        --sim-mean 98.8 --sim-ceiling 123.8 --chalk 134

    python -m src.ledger result --entry-id 3 --score 112.4 --rank 380 --payout 12.50

    python -m src.ledger report

Conventions (see CLAUDE.md):
  - Rows live in the main SQLite DB (``paths.db``) beside salaries and stats,
    so entries can be joined against the rest of the pipeline later.
  - model_version is the git commit hash, read automatically.
  - Refuses to log an entry from a dirty working tree: if the code that built
    a lineup isn't committed, the entry can't be attributed later.
  - The minimum-N rule is enforced in the report, not by blocking writes —
    small samples are shown, just flagged as not conclusive.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import date as _date
from pathlib import Path
from typing import Sequence

from src import config as config_mod
from src import db

# Below this many entries in a contest_type x model_version cell, results are
# reported but flagged as non-conclusive. GPP ROI needs far more than this to
# separate skill from variance; treat even a passing cell as directional until
# the counts are in the hundreds.
MIN_N = 50

# model_version for entries backfilled outside a git checkout.
NO_GIT = "no-git"

# Roadmap Step 0 asks for the season's success metric at the top of the ledger,
# so it is read every time results are, rather than remembered. Set by H1 —
# see docs/decisions.md for the reasoning and what follows from it.
SUCCESS_METRIC = (
    f"Season 1: {MIN_N}+ entries in ONE contest type, each attributable to a "
    "commit, showing positive ROI in that cell."
)


class LedgerError(RuntimeError):
    """A ledger operation that must not silently succeed."""


def git_commit(allow_dirty: bool = False, repo: Path | str | None = None) -> str:
    """Current commit hash. Refuses if the tree is dirty (see CLAUDE.md).

    ``allow_dirty`` exists for backfilling historical entries, where no commit
    corresponds to the lineup anyway; such rows are tagged so the report can't
    mistake them for attributable ones.
    """
    repo = Path(repo) if repo is not None else config_mod.REPO_ROOT
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True, cwd=str(repo), stderr=subprocess.DEVNULL,
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"],
            text=True, cwd=str(repo), stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError) as exc:
        if allow_dirty:
            return NO_GIT
        raise LedgerError(
            "not a git repo, or git is unavailable, so this entry could not be "
            "attributed to a code version. Use --allow-dirty only for "
            "backfilling historical entries."
        ) from exc

    if dirty and not allow_dirty:
        raise LedgerError(
            "working tree is dirty. Commit before logging an entry — an entry "
            "you can't attribute to a code version is a data point you can't "
            "learn from. (--allow-dirty to override.)"
        )
    return commit + ("-dirty" if dirty else "")


def parse_lineup(raw: str) -> list[str]:
    """Pipe-separated roster, CPT first.

    A blank slot means a truncated copy/paste, which would otherwise be logged
    as a legitimate short lineup and quietly corrupt the record.
    """
    if raw is None or not raw.strip():
        raise LedgerError("--lineup is empty")
    players = [p.strip() for p in raw.split("|")]
    if any(not p for p in players):
        raise LedgerError(f"--lineup has a blank slot: {raw!r}")
    return players


def add_entry(
    conn: sqlite3.Connection,
    *,
    slate_id: str,
    contest_type: str,
    lineup: Sequence[str],
    model_version: str,
    entry_date: str | None = None,
    contest_id: str | None = None,
    field_size: int | None = None,
    entry_fee: float | None = None,
    payout_structure_id: str | None = None,
    sim_mean: float | None = None,
    sim_ceiling: float | None = None,
    chalk_score: float | None = None,
    dup_estimate: float | None = None,
) -> int:
    """Log an entry before lock. Returns the new entry_id.

    Deliberately append-only: the same lineup entered into two contests is two
    rows, so re-running this is not an idempotent upsert like the ingests are.
    """
    cur = conn.execute(
        """INSERT INTO entries (date, slate_id, contest_id, contest_type, field_size,
                                entry_fee, payout_structure_id, lineup, model_version,
                                sim_mean, sim_ceiling, chalk_score, dup_estimate)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (entry_date or _date.today().isoformat(), slate_id, contest_id, contest_type,
         field_size, entry_fee, payout_structure_id, json.dumps(list(lineup)),
         model_version, sim_mean, sim_ceiling, chalk_score, dup_estimate),
    )
    return int(cur.lastrowid)


def settle_entry(
    conn: sqlite3.Connection,
    entry_id: int,
    *,
    actual_score: float,
    finish_rank: int | None = None,
    payout: float = 0.0,
) -> float | None:
    """Record the result. Returns ROI, or None for a free entry.

    Free contests still get a row — finish rank is signal even when there is no
    money in it — but they have no ROI to compute.
    """
    row = conn.execute(
        "SELECT entry_fee FROM entries WHERE entry_id=?", (entry_id,)
    ).fetchone()
    if row is None:
        raise LedgerError(f"no entry {entry_id}")
    fee = row["entry_fee"]
    roi = (payout - fee) / fee if fee else None
    conn.execute(
        """UPDATE entries SET actual_score=?, finish_rank=?, payout=?, roi=?
           WHERE entry_id=?""",
        (actual_score, finish_rank, payout, roi, entry_id),
    )
    return roi


def report_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """ROI aggregated by contest_type x model_version."""
    return conn.execute(
        """SELECT contest_type, model_version, COUNT(*) n,
                  SUM(COALESCE(entry_fee,0)) staked,
                  SUM(COALESCE(payout,0)) returned,
                  SUM(CASE WHEN payout > 0 THEN 1 ELSE 0 END) cashes,
                  SUM(CASE WHEN actual_score IS NULL THEN 1 ELSE 0 END) pending
           FROM entries GROUP BY contest_type, model_version
           ORDER BY contest_type, model_version"""
    ).fetchall()


def format_report(rows: Sequence[sqlite3.Row]) -> str:
    header = [SUCCESS_METRIC, ""]
    if not rows:
        return "\n".join(header + ["No entries logged yet."])

    # Progress against the metric is the closest cell to N, not the total:
    # entries spread across contest types never produce a conclusion.
    best = max(rows, key=lambda r: r["n"])
    if best["n"] >= MIN_N:
        header.append(
            f"At N: {best['contest_type']} has {best['n']} entries on "
            f"{best['model_version']}."
        )
    else:
        header.append(
            f"Progress: {best['n']}/{MIN_N} in {best['contest_type']} "
            f"({best['model_version']}) — the closest cell to a conclusion."
        )
    header.append("")

    lines = header + [
        f"{'contest_type':<20}{'version':<14}{'n':>5}{'staked':>9}{'returned':>10}"
        f"{'ROI':>9}{'cash%':>8}{'pend':>6}"
    ]
    for row in rows:
        n, pending = row["n"], row["pending"]
        staked, returned = row["staked"], row["returned"]
        settled = n - pending
        net_roi = ((returned - staked) / staked) if staked else None
        roi_s = f"{net_roi:+.1%}" if net_roi is not None else "n/a"
        cash_s = f"{row['cashes'] / settled:.0%}" if settled else "n/a"
        flag = "" if n >= MIN_N else "  <- small sample, not conclusive"
        lines.append(
            f"{row['contest_type']:<20}{row['model_version']:<14}{n:>5}"
            f"{staked:>9.2f}{returned:>10.2f}{roi_s:>9}{cash_s:>8}{pending:>6}{flag}"
        )

    lines.append("")
    lines.append(
        f"Cells under n={MIN_N} are directional only. GPP ROI needs hundreds of"
    )
    lines.append("entries before a difference between versions means anything.")
    return "\n".join(lines)


def cmd_add(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    lineup = parse_lineup(args.lineup)
    entry_id = add_entry(
        conn,
        slate_id=args.slate,
        contest_type=args.type,
        lineup=lineup,
        model_version=git_commit(args.allow_dirty),
        entry_date=args.date,
        contest_id=args.contest,
        field_size=args.field_size,
        entry_fee=args.fee,
        payout_structure_id=args.payout_structure,
        sim_mean=args.sim_mean,
        sim_ceiling=args.sim_ceiling,
        chalk_score=args.chalk,
        dup_estimate=args.dup,
    )
    print(f"entry {entry_id} logged ({args.type}, {args.slate})")
    return 0


def cmd_result(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    roi = settle_entry(
        conn, args.entry_id,
        actual_score=args.score, finish_rank=args.rank, payout=args.payout,
    )
    roi_str = f"{roi:+.1%}" if roi is not None else "n/a"
    print(
        f"entry {args.entry_id} settled: {args.score} pts, "
        f"rank {args.rank}, ROI {roi_str}"
    )
    return 0


def cmd_report(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    print(format_report(report_rows(conn)))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DFS experiment ledger")
    parser.add_argument("--db", default=None, help="override the configured db path")
    sub = parser.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="log an entry before the contest locks")
    a.add_argument("--slate", required=True)
    a.add_argument("--contest")
    a.add_argument("--type", required=True, help="showdown_gpp / showdown_cash / ...")
    a.add_argument("--field-size", type=int)
    a.add_argument("--fee", type=float)
    a.add_argument("--payout-structure", help="id of saved payout table")
    a.add_argument("--lineup", required=True, help="pipe-separated, CPT first")
    a.add_argument("--sim-mean", type=float)
    a.add_argument("--sim-ceiling", type=float)
    a.add_argument("--chalk", type=float)
    a.add_argument("--dup", type=float, help="duplication estimate (task T7)")
    a.add_argument("--date")
    a.add_argument("--allow-dirty", action="store_true")
    a.set_defaults(func=cmd_add)

    r = sub.add_parser("result", help="settle an entry after the contest")
    r.add_argument("--entry-id", type=int, required=True)
    r.add_argument("--score", type=float, required=True)
    r.add_argument("--rank", type=int)
    r.add_argument("--payout", type=float, default=0.0)
    r.set_defaults(func=cmd_result)

    rep = sub.add_parser("report", help="ROI by contest type and model version")
    rep.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)

    try:
        # session() rolls back on error, so a refused entry writes nothing.
        with db.session(args.db) as conn:
            db.create_schema(conn)
            return args.func(conn, args)
    except LedgerError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
