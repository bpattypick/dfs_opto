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
- [ ] **H6. Set the cash/GPP allocation and stake sizing.** `CONTEST_RULES.md` R5 encodes the roadmap's shape (majority cash/single-entry, minority GPP) but not the numbers — the exact split, the per-entry stake, and whether R4 should carry a hard rake ceiling are money calls. Record in `docs/decisions.md`. *Blocks: nothing in code; R5 stays soft until answered.*
- [ ] **H7. Decide the projection source for Showdown slates.** The optimal-rate baseline (T4) randomizes projections, but nothing populates the `projections` table and the DK salary parser discards `AvgPointsPerGame`, so there is no projection input on a real slate today. Cheapest interim fix is retaining `AvgPointsPerGame` in the salaries ingest — free, already in every export — but for backtests it must be the value archived pre-lock, not a later file, or it breaks the leakage guarantee. Confirm the interim source, or answer H4. *Blocks: running T4 and T5 on real data (unit tests are unaffected).*
- [ ] **H4. Vendor projection subscription** — decide yes/no and which. If yes, start archiving Thursday + Sunday exports. *Blocks: T10, and is one answer to H7. Low urgency only if H7 is settled another way.*

**Resolved:** paid contest entry from home is confirmed working — real-money entries are on the table, so the ledger's ROI numbers are live money, not paper.

---

## Ready

### T3. Standings ingester
Parse DK contest standings CSVs from `data/raw/standings/` into an `actual_ownership` table (slate_id, contest_id, player_id, cpt_pct, flex_pct, total_pct) plus a `contest_payouts` table from the payout column.
**Acceptance:** idempotent; routes names through the crosswalk; join coverage ≥97% with a loud failure below; one test against a real archived file. *Blocked until H2 has produced at least one file.*

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

- **T4. Optimal-rate ownership baseline** — estimator half is done and committed
  (`src/ownership.py`, `estimate_ownership(pool, n=1000)` returning CPT/FLEX/total rates, 23 tests).
  Remaining: the calibration script comparing it to `actual_ownership` and reporting MAE, which
  needs T3, which needs H2 to produce a standings file. Running it on a real slate additionally
  needs H7 (no projection source exists yet).

## Done

*(append: task id, date, one-line result)*

- **T1. Experiment ledger** — 2026-09-09 — `ledger.py` moved to `src/ledger.py` and wired to the
  main SQLite DB (`entries` table added to `src/db.py`, so `python -m src.db` builds it); add /
  result / report subcommands, dirty-tree refusal verified against the real repo, 32 tests in
  `tests/test_ledger.py`. Raised H5 (roadmap's `results.md` view vs the CLI report).
- **T2. Contest rules doc** — 2026-09-09 — `CONTEST_RULES.md` written from roadmap Step 2 as seven
  pre-entry checks (R1 Showdown-only, R2 soft field, R3 no marquee GPP with a ledger-based unlock,
  R4 effective rake + overlay, R5 allocation, R6 human enters, R7 logged before lock), plus a
  deviation-recording convention. Doc only, no modeling. Raised H6 (the allocation numbers are a
  money call, so R5 is deliberately soft).
