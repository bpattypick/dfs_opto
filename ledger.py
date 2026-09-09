"""
Experiment ledger — every DFS entry, the code version that built it, and the result.

Why this exists: without it, process improvements and variance are
indistinguishable. GPP results are noisy enough that a few good weeks feel
like edge. The ledger is what turns "I think this is working" into a number.

Usage:
    python ledger.py add --slate 2026-w01-showdown-sea-ne \
        --contest 12345 --type showdown_gpp --field-size 5000 \
        --fee 5 --lineup "Drake Maye|Jaxon Smith-Njigba|AJ Barner|..." \
        --sim-mean 98.8 --sim-ceiling 123.8 --chalk 134

    python ledger.py result --entry-id 3 --score 112.4 --rank 380 --payout 12.50

    python ledger.py report

Conventions (see CLAUDE.md):
  - model_version is the git commit hash, read automatically.
  - Refuses to log an entry from a dirty working tree: if the code that
    built a lineup isn't committed, the entry can't be attributed later.
  - Minimum-N rule is enforced in the report, not by blocking writes —
    small samples are shown, just flagged as not conclusive.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date

DB_PATH = os.environ.get("DFS_DB", "data/dfs.sqlite")

# Below this many entries in a contest_type × model_version cell, results
# are reported but flagged as non-conclusive. GPP ROI needs far more than
# this to separate skill from variance; treat even a passing cell as
# directional until the counts are in the hundreds.
MIN_N = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    slate_id TEXT NOT NULL,
    contest_id TEXT,
    contest_type TEXT NOT NULL,        -- showdown_gpp / showdown_cash / single_entry / ...
    field_size INTEGER,
    entry_fee REAL,
    payout_structure_id TEXT,          -- links to a saved payout table
    lineup TEXT NOT NULL,              -- json list; first element is CPT
    model_version TEXT NOT NULL,       -- git commit hash
    sim_mean REAL,
    sim_ceiling REAL,
    chalk_score REAL,
    dup_estimate REAL,
    actual_score REAL,
    finish_rank INTEGER,
    payout REAL,
    roi REAL
);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(SCHEMA)
    return conn


def git_commit(allow_dirty=False):
    """Current commit hash. Refuses if the tree is dirty (see CLAUDE.md)."""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        if allow_dirty:
            return "no-git"
        sys.exit("!! Not a git repo. Entries must be attributable to a commit.\n"
                 "   Use --allow-dirty only for backfilling historical entries.")

    if dirty and not allow_dirty:
        sys.exit("!! Working tree is dirty. Commit before logging an entry —\n"
                 "   an entry you can't attribute to a code version is a data point\n"
                 "   you can't learn from. (--allow-dirty to override.)")
    return commit + ("-dirty" if dirty else "")


def cmd_add(args):
    conn = connect()
    lineup = [p.strip() for p in args.lineup.split("|")] if args.lineup else []
    cur = conn.execute(
        """INSERT INTO entries (date, slate_id, contest_id, contest_type, field_size,
                                entry_fee, payout_structure_id, lineup, model_version,
                                sim_mean, sim_ceiling, chalk_score, dup_estimate)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (args.date or date.today().isoformat(), args.slate, args.contest, args.type,
         args.field_size, args.fee, args.payout_structure, json.dumps(lineup),
         git_commit(args.allow_dirty), args.sim_mean, args.sim_ceiling,
         args.chalk, args.dup),
    )
    conn.commit()
    print(f"entry {cur.lastrowid} logged ({args.type}, {args.slate})")


def cmd_result(args):
    conn = connect()
    row = conn.execute("SELECT entry_fee FROM entries WHERE entry_id=?",
                       (args.entry_id,)).fetchone()
    if row is None:
        sys.exit(f"!! No entry {args.entry_id}")
    fee = row[0]
    roi = None
    if fee:
        roi = (args.payout - fee) / fee
    conn.execute(
        """UPDATE entries SET actual_score=?, finish_rank=?, payout=?, roi=?
           WHERE entry_id=?""",
        (args.score, args.rank, args.payout, roi, args.entry_id),
    )
    conn.commit()
    roi_str = f"{roi:+.1%}" if roi is not None else "n/a"
    print(f"entry {args.entry_id} settled: {args.score} pts, rank {args.rank}, ROI {roi_str}")


def cmd_report(args):
    conn = connect()
    rows = conn.execute(
        """SELECT contest_type, model_version, COUNT(*) n,
                  SUM(COALESCE(entry_fee,0)) staked,
                  SUM(COALESCE(payout,0)) returned,
                  AVG(roi) avg_roi,
                  SUM(CASE WHEN payout > 0 THEN 1 ELSE 0 END) cashes,
                  SUM(CASE WHEN actual_score IS NULL THEN 1 ELSE 0 END) pending
           FROM entries GROUP BY contest_type, model_version
           ORDER BY contest_type, model_version"""
    ).fetchall()

    if not rows:
        print("No entries logged yet.")
        return

    print(f"{'contest_type':<20}{'version':<14}{'n':>5}{'staked':>9}{'returned':>10}"
          f"{'ROI':>9}{'cash%':>8}{'pend':>6}")
    for (ctype, ver, n, staked, returned, avg_roi, cashes, pending) in rows:
        settled = n - pending
        net_roi = ((returned - staked) / staked) if staked else None
        roi_s = f"{net_roi:+.1%}" if net_roi is not None else "n/a"
        cash_s = f"{cashes/settled:.0%}" if settled else "n/a"
        flag = "" if n >= MIN_N else "  <- small sample, not conclusive"
        print(f"{ctype:<20}{ver:<14}{n:>5}{staked:>9.2f}{returned:>10.2f}"
              f"{roi_s:>9}{cash_s:>8}{pending:>6}{flag}")

    print(f"\nCells under n={MIN_N} are directional only. GPP ROI needs hundreds of")
    print("entries before a difference between versions means anything.")


def main():
    p = argparse.ArgumentParser(description="DFS experiment ledger")
    sub = p.add_subparsers(dest="cmd", required=True)

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

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
