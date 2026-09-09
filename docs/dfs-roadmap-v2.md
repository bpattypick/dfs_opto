# DFS Pipeline — Revised Roadmap (Post Pressure-Test)

**Written:** September 9, 2026 (Week 1). Supersedes the Phase 2+ ordering in the July roadmap. Phase 1 (data foundation, backtest harness) stands unchanged.

**What changed and why:** the July plan sequenced projection ensemble → ownership model → simulator → LLM agent. The pressure test showed that ordering builds a worse copy of what vendors already sell. Edge for a solo builder lives in contest selection, late-swap speed, and duplication-aware Showdown builds — none of which need a projection ensemble. So the model work gets reordered, the scope gets narrowed to a lane where a solo builder can actually win, and a measurement layer gets added so "edge" is a number instead of a feeling.

**Scope through January:** NFL Showdown slates + soft-field contests only. One modeling improvement per month. Everything else is parked.

---

## Step 0 — Decide which game you're playing (this week)

This gates everything downstream and takes one evening, not a sprint.

1. **Resolve the Wisconsin question in the app.** Try to enter a paid Showdown contest. If geolocation blocks it, real-money personal edge is off the table from home and this project is a *product* project. If it's allowed, it can be both. Write the answer down.
2. **Pick the primary game.**
   - *Personal edge:* success = ROI over N entries. Requires bankroll discipline and hundreds of entries before conclusions mean anything.
   - *Community product:* success = subscribers who value rationale-backed picks. Your projections do not need to beat vendors'; your packaging and explanation do. Lower variance, different economics.
   - Both are legitimate. Choosing neither is what kills the project.
3. **Set the one-line success metric** for the primary game and put it at the top of the experiment ledger (Step 1).

**Exit:** a written decision, in the repo, that future-you can't argue with.

---

## Step 1 — Experiment ledger (Sprint A: Sept 10–16)

A few hours of work that retroactively validates everything else. Without it, process improvements and variance are indistinguishable.

**Schema (`entries` table or CSV, one row per contest entry):**

```
entry_id, date, slate_id, contest_id, contest_type (cash/gpp/single-entry/showdown),
field_size, entry_fee, payout_structure_id, lineup (json), model_version,
sim_mean, sim_ceiling, chalk_score, dup_estimate,
actual_score, finish_rank, payout, roi
```

**Rules:**
- Every entry gets a row, including free-to-play ones. Free entries still produce finish-rank data.
- `model_version` is a git commit hash. If the code that built a lineup isn't committed, the entry isn't logged. This is the whole point.
- `payout_structure_id` links to a saved copy of that contest's payout table — needed for the contest sim later.
- A `results.md` notebook or view that reports ROI by contest_type × model_version, with entry counts, so small-N claims are visibly small-N.

**Minimum-N rule:** no conclusion about any model change until it has ≥50 entries in the same contest type. GPP ROI needs far more than that to be distinguishable from noise; treat anything under a few hundred GPP entries as "directional at best." Write this rule into the notebook header so it's a permanent reminder.

**Exit:** tonight's and Sunday's entries are already logged with a commit hash.

---

## Step 2 — Contest selection rules (Sprint A, same week)

Zero modeling. Highest edge-per-effort on the board. Codify it so it isn't a per-week judgment call.

- **Default to soft fields:** smaller contests, single-entry or 3-max, non-marquee slates. Stay out of the marquee large-field GPP (the "Milly Maker" tier) until the ledger says the process is positive elsewhere.
- **Showdown first:** single-game slates have shallower, more predictable fields and a mechanic (duplication) that casual players don't model. Primetime and standalone games only through October.
- **Rake awareness:** note each contest's effective rake in the ledger. Prefer contests with lower rake or overlay (guaranteed prize pool that doesn't fill).
- **Cash vs GPP split:** a written allocation (e.g., majority cash/single-entry while validating the process; small GPP allocation for ceiling). Adjust only on ledger evidence, not on how last week felt.

**Exit:** a `CONTEST_RULES.md` in the repo; each week's entries conform to it or the ledger notes why not.

---

## Step 3 — Contest simulator (Sprint B: Sept 17 – Oct 14) ← the core build

This replaces "projection ensemble" as the first modeling project because it's the piece that makes every other component measurable. The current sim produces your lineup's *point distribution*; this produces its *expected finish and ROI*.

**Three parts, built in order:**

**3a. Field generator.** Given ownership estimates (start with the tier system + the optimal-rate baseline: run your optimizer N times over lightly randomized projections and count how often each player appears), sample thousands of plausible opponent lineups that respect the salary cap, roster rules, and ownership rates. Output: a synthetic field the size of your target contest.

**3b. Outcome + placement.** For each Monte Carlo trial, simulate player scores (existing sim), score every field lineup and yours, and record your finish rank. Combine with the saved payout structure → payout per trial → expected ROI, plus the distribution (% of trials cashing, % top-1%, etc.).

**3c. Duplication model (Showdown-specific).** Estimate, for any candidate lineup, how many field entries are *identical* to it. In a small Showdown pool the "optimal" builds get duplicated heavily, and a duplicated first place splits the prize. Approach: from the field generator, count exact-match frequency of each candidate; report `dup_estimate` alongside ROI and let the optimizer trade a little projected points for a lot less duplication. This is the single most concrete Showdown edge available and almost nobody casual models it.

**Data prerequisite:** actual ownership from DK contest standings (download after every contest, starting tonight). It's the calibration target for the field generator and the training data for the eventual ownership model.

**Exit:** the sim ranks candidate lineups by expected ROI and dup-adjusted ROI for a Showdown slate; the ledger records `dup_estimate` for every entry; a one-week calibration check compares simulated ownership vs. actual standings ownership.

---

## Step 4 — Late-swap / news pipeline (Sprint C: Oct 15 – Nov 11)

The structural, repeatable edge: reacting inside the 90 minutes before lock faster than the field.

**This is a data-access problem first.** Decide the feed before writing any LLM code:
- Options: news-site RSS/APIs, DK's own player status field (already in the salary export — poll it), team injury-report pages, a paid news aggregator. X/Twitter API is the richest source and the most expensive; price it before assuming it.
- Poll cadence: every 5–10 minutes from 90 min before lock; every minute in the last 15.

**Then the agent:** LLM parses each new item into `{player, status, expected_snap_change, confidence, source, timestamp}`. Rule-based triggers (a starter → OUT) fire an automatic re-projection and re-sim; the pipeline surfaces a swap recommendation, and you approve it in one click. Human-in-the-loop for money decisions; automation for the speed.

**Exit:** a dry run on a Sunday where an inactive is detected, re-sim completes, and a swap is recommended before lock; timestamps logged.

---

## Step 5 — Ownership model, then projection ensemble (Nov 12 → season end)

Now they earn their place, in that order, and only because Steps 3–4 give them something to be measured against.

- **Ownership model:** LightGBM on salary, value rank, projection rank, Vegas features, recency, name-recognition proxy; target = actual ownership from the standings archive built since Week 1. Success metric: beats the optimal-rate baseline on held-out slates. Plug into the field generator.
- **Projection ensemble:** bias-corrected blend of vendor feeds + own features + props. Success metric: the contest sim shows higher expected ROI with it than without — not lower MAE. MAE is a proxy; ROI is the point.

**Exit:** each component ships only if the contest sim says it improves ROI on the ledger's contest types.

---

## Monthly cadence (the one-improvement rule)

| Month | Ships |
|---|---|
| September | Ledger, contest rules, standings archiving, optimal-rate baseline |
| October | Contest simulator (3a–3c) |
| November | Late-swap pipeline |
| December | Ownership model |
| January | Projection ensemble (if the ledger still argues for it) |

Anything not on this list waits. If a month slips, the next month's item slips — nothing gets stacked.

## Weekly rhythm (updated)

| When | What | Time |
|---|---|---|
| Thu evening / Sun AM | Capture: projections, salaries, odds, DK status field | 10 min |
| Fri/Sat | Crosswalk triage; check contest rules for the week's entries | 15 min |
| Pre-lock | Run sim → pick entries per contest rules → log with commit hash | 15 min |
| Post-lock | Download contest standings (actual ownership) — every contest entered | 5 min |
| Mon | Ingest results; ledger rows get `actual_score`, `finish_rank`, `roi` | 10 min |

## Parking lot (explicitly not this season)

- Sunday main-slate large-field GPPs
- Full-slate contest simulation (Showdown only for now)
- Play-by-play-derived correlation structure
- DST points-allowed attribution fix
- Any sport other than NFL
- The community product's *build* (its *decision* is Step 0; the build waits until the sim and ledger exist, because the product is the sim's output packaged)

## Risks (revised)

1. **Mistaking variance for edge** — mitigated by the ledger and the minimum-N rule. This is the risk that actually ends bankrolls.
2. **Scope creep back toward the ensemble** — mitigated by the monthly cadence and the "ships only if ROI improves" gate.
3. **News-feed cost/access** stalls Step 4 — mitigated by deciding the feed before writing the agent.
4. **Standings archive gaps** — every contest entered gets its standings downloaded, no exceptions; it's the calibration data for everything in Step 3+.
5. **Legal ambiguity** — resolved at Step 0, not assumed.
