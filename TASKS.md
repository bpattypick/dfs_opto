# TASKS.md

The work queue. Claude Code: pick the **top unblocked task in Ready**, do it, move it to Done with a one-line result, commit. If it's blocked, move it to Blocked with the reason and take the next one.

Tasks are ordered. Order reflects the roadmap's monthly cadence — don't skip ahead to more interesting work.

---

## Human-only queue (owner must do these — code cannot)

These block downstream tasks. Each is short.

- [ ] **H1. Write the Step 0 decision.** Personal edge, community product, or both. One paragraph in `docs/decisions.md` with the success metric. *Blocks: T7 and everything in Nov+.*
- [ ] **H2. Download contest standings** after every contest entered, into `data/raw/standings/`. Recurring, every slate. *Blocks: T3, T4, and the entire ownership model.*
- [ ] **H3. Decide the news feed** for late-swap (RSS vs paid aggregator vs X API). Price it. Record in `docs/decisions.md`. *Blocks: T8. Due before mid-October.*
- [ ] **H5. Decide whether the ledger needs a `results.md` view.** Roadmap Step 1 asks for a notebook/view; T1's acceptance asked for `python -m src.ledger report`, which is what was built. Confirm the CLI report is enough or spec the view. *Blocks: nothing.*
- [ ] **H4. Vendor projection subscription** — decide yes/no and which. If yes, start archiving Thursday + Sunday exports. *Blocks: T10 only. Low urgency under current scope.*

**Resolved:** paid contest entry from home is confirmed working — real-money entries are on the table, so the ledger's ROI numbers are live money, not paper.

---

## Ready

### T2. Contest rules doc
Write `CONTEST_RULES.md` from roadmap Step 2: soft-field default, Showdown-first, rake awareness, cash/GPP allocation.
**Acceptance:** the doc exists and each rule is stated as a check that can be applied to a candidate contest before entry. No modeling.

### T3. Standings ingester
Parse DK contest standings CSVs from `data/raw/standings/` into an `actual_ownership` table (slate_id, contest_id, player_id, cpt_pct, flex_pct, total_pct) plus a `contest_payouts` table from the payout column.
**Acceptance:** idempotent; routes names through the crosswalk; join coverage ≥97% with a loud failure below; one test against a real archived file. *Blocked until H2 has produced at least one file.*

### T4. Optimal-rate ownership baseline
Implement the baseline described in the roadmap Step 3a: run the optimizer N times over lightly randomized projections, count player appearance rate. This is the zero-cost ownership estimate everything else must beat.
**Acceptance:** `estimate_ownership(pool, n=1000)` returns per-player CPT and FLEX rates; a calibration script compares its output to `actual_ownership` for any slate that has standings, reporting MAE. *Partially blocked: calibration needs T3, but the estimator itself can be built and tested now.*

### T5. Field generator (contest sim 3a)
Given ownership estimates, sample N plausible opponent lineups respecting salary cap, Showdown roster rules (1 CPT + 5 FLEX, both teams represented), and target ownership rates.
**Acceptance:** generates a field of arbitrary size; realized player ownership in the generated field matches input estimates within a stated tolerance; test asserts every generated lineup is DK-legal.

### T6. Placement + ROI (contest sim 3b)
For each Monte Carlo trial: score the generated field and the candidate lineup, rank, map to the saved payout structure, accumulate payout.
**Acceptance:** returns expected ROI, cash rate, and top-1% rate for a candidate lineup given a contest size + payout table; test with a synthetic payout structure where the correct answer is hand-computable.

### T7. Duplication model (contest sim 3c)
From the field generator, estimate exact-match frequency for a candidate lineup; expose `dup_estimate`; add a dup-penalty term to lineup selection.
**Acceptance:** `dup_estimate` recorded in the ledger for every entry; a comparison report shows dup-adjusted ROI vs raw ROI for a slate's candidate pool.

---

## Backlog (do not start before the month indicated in the roadmap)

- **T8.** Late-swap news pipeline (October–November). Blocked on H3.
- **T9.** Ownership model, LightGBM (December). Needs ≥6 weeks of standings archive.
- **T10.** Projection ensemble (January, only if the ledger argues for it). Blocked on H4.

## Blocked

*(move tasks here with a one-line reason; check each session whether the blocker cleared)*

## Done

*(append: task id, date, one-line result)*

- **T1. Experiment ledger** — 2026-09-09 — `ledger.py` moved to `src/ledger.py` and wired to the
  main SQLite DB (`entries` table added to `src/db.py`, so `python -m src.db` builds it); add /
  result / report subcommands, dirty-tree refusal verified against the real repo, 32 tests in
  `tests/test_ledger.py`. Raised H5 (roadmap's `results.md` view vs the CLI report).
