# TASKS.md

The work queue. Claude Code: pick the **top unblocked task in Ready**, do it, move it to Done with a one-line result, commit. If it's blocked, move it to Blocked with the reason and take the next one.

Tasks are ordered. Order reflects the roadmap's monthly cadence — don't skip ahead to more interesting work.

---

## Human-only queue (owner must do these — code cannot)

These block downstream tasks. Each is short.

- [x] **H1. Step 0 — ANSWERED 2026-09-09: both, personal edge first.** The product has nothing to sell without a track record, so the product phase depends on the personal-edge phase succeeding. Season-1 metric: 50+ entries in ONE contest type, each attributable to a commit, showing positive ROI in that cell — now printed at the top of `python -m src.ledger report`. Full reasoning and consequences in `docs/decisions.md`. **Feeds H6: concentrate entries in one contest type (cash/single-entry) rather than splitting — at ~3 slates/week for 17 weeks, splitting means neither cell reaches N.**
- [ ] **H2. Download contest standings** after every contest entered, into `data/raw/standings/`. Recurring, every slate. **First miss: DEN@KC's standings expired before download** — a real calibration slate lost for good, not a hypothetical risk. *Blocks: T3, T4, and the entire ownership model.* after every contest entered, into `data/raw/standings/`. Recurring, every slate. *Blocks: T3, T4, and the entire ownership model.*
- [ ] **H3. Decide the news feed** for late-swap (RSS vs paid aggregator vs X API). Price it. Record in `docs/decisions.md`. *Blocks: T8. Due before mid-October.*
- [ ] **H5. Decide whether the ledger needs a `results.md` view.** Roadmap Step 1 asks for a notebook/view; T1's acceptance asked for `python -m src.ledger report`, which is what was built. Confirm the CLI report is enough or spec the view. *Blocks: nothing.*
- [ ] **H6. Set the cash/GPP allocation and stake sizing.** `CONTEST_RULES.md` R5 encodes the roadmap's shape (majority cash/single-entry, minority GPP) but not the numbers — the exact split, the per-entry stake, and whether R4 should carry a hard rake ceiling are money calls. Record in `docs/decisions.md`. *Blocks: nothing in code; R5 stays soft until answered.*
- [x] **H7. Interim projection source — ANSWERED 2026-09-09: yes, `AvgPointsPerGame`.** Recorded in `docs/decisions.md` with the accepted caveats (backward-looking in week 1; must be the pre-lock archived value for backtests). `src/pool.from_export()` is the supported path.
- [ ] **H8. Player prop lines — pull now, or wait for January?** Props are a much better projection input than `AvgPointsPerGame` (forward-looking, market-priced, already absorb injury/matchup/game-script news) and would also give the score simulator real per-player variance instead of the placeholder's flat coefficient. Two reasons it is your call, not mine: player-prop markets are a **paid tier** on The Odds API (`config.yaml` has the odds block wired for spreads/totals only, currently disabled), and the roadmap schedules props for Step 5 in **January**, so doing it now is a second modelling improvement this month against the one-per-month cadence. If yes, props must be archived pre-lock with `pulled_at` or backtests leak. *Blocks: nothing; would upgrade T4, T5 and T6 inputs at once.* **Priority raised 2026-09-17:** the held-out measurement in data-sources.md shows role/availability knowledge is the single largest projection lever (2.5× all stat-history features combined); props are the market's already-role-aware number and would cover the top of the board in one step while T22 is built from free sources.
- [x] **H9. Which model drives live slates until T22 lands — ANSWERED BY T22 2026-09-17: neither; v4 (`RoleAware`) beats both on the honest pool and is the live default.** Owner may still veto — the reasoning below stands as the record of why the question arose.
  Original question: T23's full-roster backtest reverses T18's verdict. On the pool a Showdown entrant actually faces (everyone who dressed, 0 for no stat line), `calibrated_by_position` (v3, currently wired into `project_live_pool`) is *worse* than the plain trailing average: top-12-per-game Spearman 0.405 vs 0.438, MAE 5.13 vs 4.90, and it hands zero-scorers 5.2 points vs 4.2. The mechanism is the per-position intercept: it lifts low projections (right for the played pool, where the bottom decile ran ~1 point low) and that is exactly wrong when 28% of the pool — 40% of dressed QBs — scores 0. v3 still has the better top-12 *bias* (+1.04 vs +1.59). **Recommendation: keep v3 live and treat T22 as the real fix** (role gating removes the zero-scorers v3 mis-lifts, after which the calibration is right again), but this is a model-selection call on live money, so it is yours. Either way, `--pool roster` is the ship criterion from now on. *Blocks: nothing in code.*
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

- **T16. Projections are blind to role changes, and that is the dominant error.** `AvgPointsPerGame` rated Jadarian Price 0.0; the real field owned him 48.1% — he started with Charbonnet and Henderson OUT. No field tuning recovers a player the projection scores at zero, so this outranks T15 in impact. H7 accepted this weakness knowingly; the measurement now sizes it. Cheapest fixes, in order: treat a 0.0 `AvgPointsPerGame` on a non-trivial salary as unknown rather than zero and flag it for review; then implied-team-total x usage share from `player_week_stats` (free, backtestable to 2019 — see H8 discussion). *Blocks: the optimizer ever surfacing a role-change play.*
- **T17. Salary floor for the optimizer's pool only — NOT the field model.** Validated on SF@LAR: Xavier Smith, the 12.5 pts/$1k artefact, drew 3.4% ownership and scored 0.0, so the floor is right for selection. But applying it to the ownership estimate made calibration worse (MAE 8.8 vs 6.4 unfloored) because real fields do roster a few minimum-priced players. Filter what the optimizer picks from; leave the opponent model unfiltered. Minimum-priced players wreck the optimizer's value ratio: DK's $200-$300 tier means "will not play", but a small non-zero `AvgPointsPerGame` from garbage time gives e.g. Xavier Smith 12.5 pts/$1k, 5x the best real play. The optimizer used a <=$1,000 player in 23% of runs on 2026-w01 SF@LAR. A ~$1,200 floor in `build_pool` removes the class without touching real contributors (cheapest genuine plays were $1,400 and $2,600). Make it configurable, since the right floor is slate- and sport-dependent, and prefer it to hand-zeroing players — the salary carries the playing-time information the projection lacks. Pairs with T16: the projection is wrong in both directions, too low on role changes and too generous on minimum-priced depth. *Blocks: trusting an unfiltered optimizer run.*
- **T20. Team-change blind spot — CAUGHT LIVE, fixed.** A trailing average is a snapshot of the role a player held when the history was recorded; an offseason team change invalidates it completely and is structurally invisible (2026 isn't ingested at all). On the first live v3 run this gave a bench QB (full-time NYJ starter through 2025, now third-string at KC) a confident 14.6-point projection, and a second bench QB 7.9 off a single 2022 start at a different team. AvgPointsPerGame carries the identical blind spot, so falling back to it does not help. Mirrors T16 in mechanism, opposite in shape, and more dangerous — T16 produces an obvious 0 that invites scrutiny, this produces a plausible resolved number that doesn't. Fixed: `resolve_pool` flags `team_changed`; `project_live_pool` refuses v3 for anyone flagged and tags them distinctly; the CLI prints them as a loud warning before any recommendation. The fix can only say 'do not trust this,' not supply a better number — that still needs a human's depth-chart knowledge via a committed override, same mechanism as Stribling. Two overrides added for tonight (Fields, Ehlinger, both DEN@KC bench QBs). 4 new tests. *A finding the owner caught, not a test.*
- **T18. Level-dependent projection calibration — measured; v3 built, verdict a wash on MAE.** Regression to the mean is real and monotonic in projection level on 2020-2025 (top 5% projects +2.0 high, +2.5 on week 1) but overall bias is only +0.17, not the +5.1 two live slates suggested. v1's n/(n+k) shrinkage cannot touch it and its Vegas multiplier made things worse alone; v2's global line over-corrects QBs; **v3 fits one line per position and brings every position's bias within 0.3 of zero** (MAE 5.01 vs 5.00, RMSE 6.73 vs 6.77, Spearman 0.604 vs 0.606). Calibration removes bias, not variance, so MAE was the wrong ship criterion for it. **Owner decision:** wire v3 or the baseline into the live pool when a model replaces `AvgPointsPerGame` (T16 path); evidence in data-sources.md. **Confirmed by decile:** baseline top-5% bias +2.03 -> v3 -0.19; the monotone ramp is gone. Residual: week-1 top-5% is +0.91 (from +2.50) — pooled calibration under-corrects the stalest week, so week 1 wants its own line. Reducing MAE itself needs new information (usage trends, matchup), which is the next projection task, not another recalibration.




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

- **T15. Chalk cluster in the field generator** — 2026-09-17 — `generate_field(chalk=, chalk_share=)`:
  a share of the field is drawn from the optimizer's near-optimal builds (`field.chalk_builds`,
  frequency-weighted), the rest sampled diffusely against the residual ownership, cluster lineups
  frozen through repair. `src/standings.py` reads standings as lineups; `scripts/calibrate_field.py`
  fits/checks. **Fitted on NE@SEA (jitter 0.30, share 0.30): 63.5% distinct / 0.97% top build vs
  real 61.5% / 1.10%; checked untouched on SF@LAR: 73.5% / 0.63% vs 68.7% / 1.06%** (no cluster:
  87–89% / 0.25%). Ownership error unchanged. Live defaults set; the live script prints the field's
  shape beside the real reference on every run. 15 new tests; 537 passing. Re-check the share on
  every new standings file (H2).
- **T14. Crosswalk for a forward-looking slate** — 2026-09-17 — `build_reference` now unions the
  week's `rosters` (every status; published before the games), the week's stat rows, and a DST per
  scheduled team, falling back to the latest earlier roster week with a logged `reference_week`.
  Kickers kept in `rosters` (backtest pool filters to skill positions). `resolve_pool(conn, pool,
  season, week)` runs the real waterfall and persists the DK-id map; without a week the stop-gap is
  unchanged. Live resolution 37/33/38 → **46/46, 49/49, 49/51** (two long snappers left); the salary
  loader stores forward-looking slates (98.5 / 98.1 / 96.4% join). 14 new tests; 537 passing.
- **T24. Score model's floor** — 2026-09-17 — `EmpiricalMarginals` in `src/scoremodel.py`: the
  quantile curve of actual/projection per position and projection band (zeros included, interpolated
  between bands), fitted on the v4 roster-pool replay, behind the unchanged Gaussian copula; the
  lognormal path is untouched when no marginal is passed. Calibration-first (curves keep the empirical
  mean ratio; `normalise_mean` is the opt-out). **Held out 2024–25, projected top-12: ≤p10 0.094
  (lognormal 0.210), ≤p25 0.232 (0.318), ≤p50 0.487, ≤p90 0.906, ≤p95 0.955** — every position within
  ~0.05 of ideal. Shipped as `data/score_marginals.json` (refit with `scripts/fit_score_marginals.py`);
  the captain board and contest sim read it. DEN@KC: Dobbins' own p10 5.2 → 2.7. 16 new tests; 510 passing.
- **T19. Captain selection by floor and ceiling** — 2026-09-17 — `src/captain.py`: per-player
  floor/median/ceiling closed-form from T13's fitted lognormal; one candidate lineup per plausible
  captain with the exact best five-FLEX complement (verified against exhaustive search); lineup-level
  quantiles from one correlated draw so stacks count. `scripts/live_showdown.py --objective cash|gpp`
  prints the captain board, shortlists by the lineup's p25 (cash) or p90 (gpp) instead of by mean, and
  orders the final table by cash rate or top-1% rate. Calibration measured on the roster pool
  (top-12 slice, n=19,380): median and ceiling calibrated, floor ~10 percentile points optimistic →
  T24. Ceiling ordering finds the game's top scorer 26.3% vs 23.9% by mean. Synthetic payouts now scale
  with field size (a 500-entry test field cashed everyone). 17 new tests; 494 passing.
- **T22. Availability / role layer** — 2026-09-17 — `roles` table from nflverse depth charts
  (two formats: weekly `depth_team` through 2024, daily `pos_rank` snapshots from 2025, last
  one before kickoff and never older than 8 days) and weekly injury reports. Roster-mode
  history gains zero rows for dressed-but-silent weeks and role columns (`depth_rank`,
  `eff_rank` = ordinal among dressed teammates, `injury_status`). `RoleAware` (v4): role-bucket
  prior from time-boxed history + own same-role trailing mean, shrunk n/(n+3), then the injury
  listing. **Roster pool 2020–2025: top-12 ρ 0.485 vs 0.438, MAE 4.22 vs 4.90, Spearman 0.709
  vs 0.604, QB bias +0.29 vs +3.42; better in every season; projects the whole pool with
  `--min-games 0`.** Wired as the live default (`project_live_pool`); team-changers are projected
  from their new depth-chart role instead of refused. DEN@KC re-run: Fields 4.3 (was 14.6).
  58 new tests incl. both chart formats and the roster-mode leakage spot-check; 477 passing.
  Subsumes T16 and T20's refusal. H9 answered by measurement.
- **T23. Score the backtest on the full roster** — 2026-09-17 — `rosters` table from nflverse
  weekly rosters (archived per season; status ACT = dressed, and 99.7–100% of stat-recording
  player-weeks in every season carry it). `--pool roster` puts every dressed QB/RB/WR/TE plus
  both DSTs in the pool and scores a missing stat line as 0; `--pool played` reproduces the old
  numbers exactly. Coverage and top-12-per-game metrics added. **Verdict changes:** on the
  honest pool the baseline's bias goes +0.17 → +0.97 (QB +3.42), 28% of evaluated players score
  0 while projected 4.2, and **v3 is worse than the baseline** (top-12 ρ 0.405 vs 0.438, MAE
  5.13 vs 4.90) because its intercepts lift low projections — see H9. 25 new tests, 439 passing.
- **T21. Ingest the current season, and keep it current** — 2026-09-17 — `seasons.end: 2026`;
  2026 week 1 loaded (+389 player-weeks, full 272-game schedule with week-2 lines). Found and
  fixed a staleness bug on the way: the raw cache keyed on a bare filename, so a season in
  progress (and `games.csv`, which changes all year) would have been frozen at first download.
  `src/nflverse.py` now archives live data as dated daily snapshots, keeps every earlier one, and
  raises rather than falling back to a stale file. Snap counts now go through the same archived
  per-season fetcher (previously one unpublished year would have NULLed every season's snaps).
  `scripts/live_showdown.py` parses season/week from the export filename and runs the ingest
  first, with `ingest_status()` flagging any completed-but-unpublished week. Re-run is
  idempotent (identical counts, zero duplicate keys). Coverage on the three archived exports,
  history before 2026 w2: v3 on 11/10/10 of the top-14 (from 9/8/8); every remaining non-v3
  player is a backup QB, a kicker, or a <3-game role change — T22's list exactly. 26 new tests.
- **T13. Real score simulator** — 2026-09-11 — `src/scoremodel.py`: Gaussian copula over lognormal
  marginals, every parameter measured on 2020-2025 (sd = a_pos + b_pos*proj, R^2 0.96; QB-top target
  +0.327, opposing QBs +0.185, DST vs the QB it faces -0.251; else 0). Validated: sd of a real QB+top
  pair 14.29 vs 14.13 simulated (independence would give 12.50); control pair independent in both.
  Replaces `independent_normal_scores`, which had none of this. 22 tests.
- **Phase 1 milestone 6: backtest harness + leakage test** — 2026-09-11 — `src/backtest/` replays
  every historical week (spec §7): `history.as_of()` is strictly-before-W by tuple ordering,
  `history.slate()` carries only pre-lock facts and never scores, so a model cannot look ahead by
  construction. The leakage test CLAUDE.md's DoD has required since T1 now exists — the spec's
  spot-check (delete week W and later, output unchanged) is a real test, plus a spy asserting the
  harness never hands a model week W's scores. `src/projection.py` holds `PriorAverage` (what DK's
  AvgPointsPerGame is) and `ShrunkVegas` (v1). First result on 34,936 player-weeks: **v1 loses to the
  baseline** — MAE 5.11 vs 5.00, Spearman 0.583 vs 0.606. Not shipped. 24 tests.
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
