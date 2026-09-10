# TASKS.md

The work queue. Claude Code: pick the **top unblocked task in Ready**, do it, move it to Done with a one-line result, commit. If it's blocked, move it to Blocked with the reason and take the next one.

Tasks are ordered. Order reflects the roadmap's monthly cadence — don't skip ahead to more interesting work.

---

## Human-only queue (owner must do these — code cannot)

These block downstream tasks. Each is short.

- [x] **H1. Step 0 — ANSWERED 2026-09-09: both, personal edge first.** The product has nothing to sell without a track record, so the product phase depends on the personal-edge phase succeeding. Season-1 metric: 50+ entries in ONE contest type, each attributable to a commit, showing positive ROI in that cell — now printed at the top of `python -m src.ledger report`. Full reasoning and consequences in `docs/decisions.md`. **Feeds H6: concentrate entries in one contest type (cash/single-entry) rather than splitting — at ~3 slates/week for 17 weeks, splitting means neither cell reaches N.**
- [ ] **H2. Download contest standings** after every contest entered, into `data/raw/standings/`. Recurring, every slate. *Blocks: T3, T4, and the entire ownership model.*
- [ ] **H3. Decide the news feed** for late-swap (RSS vs paid aggregator vs X API). Price it. Record in `docs/decisions.md`. *Blocks: T8. Due before mid-October.*
- [ ] **H5. Decide whether the ledger needs a `results.md` view.** Roadmap Step 1 asks for a notebook/view; T1's acceptance asked for `python -m src.ledger report`, which is what was built. Confirm the CLI report is enough or spec the view. *Blocks: nothing.*
- [ ] **H6. Set the cash/GPP allocation and stake sizing.** `CONTEST_RULES.md` R5 encodes the roadmap's shape (majority cash/single-entry, minority GPP) but not the numbers — the exact split, the per-entry stake, and whether R4 should carry a hard rake ceiling are money calls. Record in `docs/decisions.md`. *Blocks: nothing in code; R5 stays soft until answered.*
- [x] **H7. Interim projection source — ANSWERED 2026-09-09: yes, `AvgPointsPerGame`.** Recorded in `docs/decisions.md` with the accepted caveats (backward-looking in week 1; must be the pre-lock archived value for backtests). `src/pool.from_export()` is the supported path.
- [ ] **H8. Player prop lines — pull now, or wait for January?** Props are a much better projection input than `AvgPointsPerGame` (forward-looking, market-priced, already absorb injury/matchup/game-script news) and would also give the score simulator real per-player variance instead of the placeholder's flat coefficient. Two reasons it is your call, not mine: player-prop markets are a **paid tier** on The Odds API (`config.yaml` has the odds block wired for spreads/totals only, currently disabled), and the roadmap schedules props for Step 5 in **January**, so doing it now is a second modelling improvement this month against the one-per-month cadence. If yes, props must be archived pre-lock with `pulled_at` or backtests leak. *Blocks: nothing; would upgrade T4, T5 and T6 inputs at once.*
- [ ] **H4. Vendor projection subscription** — decide yes/no and which. If yes, start archiving Thursday + Sunday exports. *Blocks: T10, and is one answer to H7. Low urgency only if H7 is settled another way.*

**Resolved:** paid contest entry from home is confirmed working — real-money entries are on the table, so the ledger's ROI numbers are live money, not paper.

---

## Ready

### T3. Standings ingester
Parse DK contest standings CSVs from `data/raw/standings/` into an `actual_ownership` table (slate_id, contest_id, player_id, cpt_pct, flex_pct, total_pct) plus a `contest_payouts` table from the payout column.
**Acceptance:** idempotent; routes names through the crosswalk; join coverage ≥97% with a loud failure below; one test against a real archived file. *Blocked until H2 has produced at least one file.*

---

## Backlog (do not start before the month indicated in the roadmap)

- **T8.** Late-swap news pipeline (October–November). Blocked on H3.
- **T9.** Ownership model, LightGBM (December). Needs ≥6 weeks of standings archive.
- **T10.** Projection ensemble (January, only if the ledger argues for it). Blocked on H4.

## Found while working (not yet scheduled)

- **T14. The crosswalk cannot resolve a forward-looking slate.** `resolve_and_store` builds its reference from `player_week_stats` for the slate's *own* season/week, which does not exist until the games have been played — so `load_file` raises on any slate that has not happened yet, and a live Showdown export cannot be stored at all. Phase 1 was built for backtesting; Steps 3-4 are forward-looking, and the roadmap does not note the difference. `src/pool.from_export()` works around it by keying on DK ids, which is fine for one slate and useless for joining across sources or weeks. Real fix is to build the reference from the most recent *prior* week (or a roster snapshot) — leakage-safe, since it is name-to-id mapping only, but it touches the crosswalk's "validated against the roster as it actually was" guarantee, so it wants a deliberate decision rather than a quiet patch.
- **T15. Field generator needs a chalk-cluster component — MEASURED, no longer hypothetical.** The 2026-w01 standings show real fields are 61.5% distinct *and* have a most-entered build at 1.10%. `jitter=0.50` reproduces the diversity (65.7%) but understates the top build 6x (0.18%); `jitter=0.30` gets closer on duplication (0.82%) at 38.9% distinct, far too concentrated. Marginal sampling cannot produce both, so set `jitter=0.5` for ownership (MAE 5.7 pts, best of the values tested) and add an explicit cluster term: some share of the field enters one of the few optimal builds, the rest sampled diffusely as now. Calibrate the share against the 1.10% figure and re-check on the next slate's standings. *Blocks: quantitative trust in duplication and dup-adjusted ROI.*
- **T16. Projections are blind to role changes, and that is the dominant error.** `AvgPointsPerGame` rated Jadarian Price 0.0; the real field owned him 48.1% — he started with Charbonnet and Henderson OUT. No field tuning recovers a player the projection scores at zero, so this outranks T15 in impact. H7 accepted this weakness knowingly; the measurement now sizes it. Cheapest fixes, in order: treat a 0.0 `AvgPointsPerGame` on a non-trivial salary as unknown rather than zero and flag it for review; then implied-team-total x usage share from `player_week_stats` (free, backtestable to 2019 — see H8 discussion). *Blocks: the optimizer ever surfacing a role-change play.*
- **T17. Salary floor for the player pool.** Minimum-priced players wreck the optimizer's value ratio: DK's $200-$300 tier means "will not play", but a small non-zero `AvgPointsPerGame` from garbage time gives e.g. Xavier Smith 12.5 pts/$1k, 5x the best real play. The optimizer used a <=$1,000 player in 23% of runs on 2026-w01 SF@LAR. A ~$1,200 floor in `build_pool` removes the class without touching real contributors (cheapest genuine plays were $1,400 and $2,600). Make it configurable, since the right floor is slate- and sport-dependent, and prefer it to hand-zeroing players — the salary carries the playing-time information the projection lacks. Pairs with T16: the projection is wrong in both directions, too low on role changes and too generous on minimum-priced depth. *Blocks: trusting an unfiltered optimizer run.*
- **T13. Real score simulator.** `src/contest.py` takes an injected score model and ships `independent_normal_scores` as a deliberate placeholder. It draws players independently, so a QB and his WR1 are uncorrelated — which understates the variance of a stacked lineup and overstates it for a spread one. Any conclusion about stacking from the current model is an artefact of the model. Roadmap Step 3b assumed an "existing sim"; there isn't one. Needs per-player distributions and a correlation structure (props would supply both — see H8).

**One-off:** `scripts/showdown_dup_report.py` ranks candidate lineups by projection against
duplication for a single slate. Written for 2026-w01 NE@SEA at the owner's request, outside the
queue, and committed so that any lineup entered from it is attributable to a commit. T7 supersedes
it — that one prices duplication against ROI rather than showing the trade by eye.

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
- **T5. Field generator** — 2026-09-09 — `src/field.py` samples DK-legal opponent lineups matching
  target ownership, plus `src/showdown.py` holding the shared roster rules (cap, 1 CPT + 5 FLEX,
  both teams, `Lineup.key()` for T7 duplication). Deficit-weighted sampling plus a legality-preserving
  repair pass; realized ownership within ~0.03 per slot, stated tolerance 0.05 in tests. 24 tests.
- **T6. Placement + ROI** — 2026-09-09 — `src/contest.py`: `PayoutTable` (with `effective_rake` for
  CONTEST_RULES R4) and `simulate_contest()` returning expected ROI, cash rate, top-1%, win rate and
  rank distribution. Ties split the pooled prize the way DK settles them, so duplication is priced
  without a separate model. Score model is injected, not owned — see T13. 28 tests, all against
  deterministic scores with hand-computable answers.
- **T11. Showdown-aware salary loading** — 2026-09-09 — `collapse_captain_rows()` folds a Showdown
  export's CPT/FLEX pairs to one base-salary row per player instead of relying on DK's sort order,
  and verifies the 1.5x captain price rather than assuming it. `status` and `avg_points` are retained
  (both were discarded); `salaries` gained those columns with an ALTER-based migration so existing
  databases don't hit "no such column". Classic slates are untouched — detection keys on CPT, since
  Classic has its own FLEX slot. 9 tests + a Showdown fixture.
- **T7. Duplication model** — 2026-09-09 — `src/duplication.py`: `dup_estimate()` scales exact-match
  frequency to the contest actually entered, `compare_duplication()` reports raw vs dup-adjusted ROI
  per candidate (counterfactual field keeps contest size fixed by substituting, not deleting), and
  `most_duplicated()` names the builds to differentiate from. Selection is by dup-adjusted ROI, which
  is the dup penalty done exactly rather than as a heuristic. `python -m src.ledger add` now refuses
  an entry without `--dup` (`--no-dup` for backfills) and the report counts entries missing it.
  23 tests. Recorded in data-sources.md: duplication cannot invert a ranking without score variance.
- **T12. Player status filtering** — 2026-09-09 — `src/pool.py`: `build_pool()` excludes OUT/IR
  (46 of 68 on the real slate, matching the hand filtering) and keeps Q players, who usually play and
  are often under-owned. Plus `questionable()` and `status_changes()`, the cheapest form of roadmap
  Step 4 — diff two pulls of the same export before lock, no news feed needed. 26 tests.
