# Decisions

Written decisions, so future-you can't relitigate them from memory. Roadmap
Step 0 asks for a decision "in the repo, that future-you can't argue with" —
this is that file. Each entry: what was decided, when, and what was knowingly
accepted along with it.

Still open, and where they go: **H1** (Step 0 — personal edge vs community
product, with its success metric), **H3** (late-swap news feed), **H6** (cash/GPP
allocation and stake sizing). See the human-only queue in `TASKS.md`.

---

## H7 — DK `AvgPointsPerGame` is the interim projection source

**Decided 2026-09-09. Owner: yes, provisionally.**

The optimal-rate ownership baseline, the field generator and the contest sim all
need a per-player projection. Nothing populates the `projections` table and no
vendor feed is subscribed, so the pipeline had no projection input on a real
slate. DK ships `AvgPointsPerGame` in every salary export, free, already
archived with the file.

**Knowingly accepted:**

- **It is backward-looking.** In week 1 it carries *last season's* average — on
  the 2026-w01 NE@SEA slate, 16 of 46 playable players sat at zero. Treat early
  season output as ordinal, not literal, and remember that everything downstream
  (ownership, field, duplication, ROI) inherits this weakness.
- **Leakage boundary.** For backtests it must be the value archived *pre-lock*
  with the export, never re-read from a file pulled after kickoff. Reading a
  later file would let post-lock information influence a pre-lock decision,
  which is exactly the failure principle 1 exists to prevent. The archived raw
  export in `data/raw/salaries/` is the record of what was knowable at the time.
- **This is interim.** H8 (player props) and H4 (a vendor feed) are the upgrade
  paths; roadmap Step 5 schedules the real projection ensemble for January. The
  ledger's `model_version` is what will show whether replacing this improved
  anything.

**Consequence:** `src/pool.from_export()` is the supported way to build a pool
for a live slate, keyed on DK's own player ids. The database path does not work
for a forward-looking slate — see T14 in `TASKS.md`.
