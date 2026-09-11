# Decisions

Written decisions, so future-you can't relitigate them from memory. Roadmap
Step 0 asks for a decision "in the repo, that future-you can't argue with" —
this is that file. Each entry: what was decided, when, and what was knowingly
accepted along with it.

Still open, and where they go: **H3** (late-swap news feed), **H6** (cash/GPP
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

## H1 — Step 0: both, but personal edge first

**Decided 2026-09-09. Owner.**

Both games are in scope, in sequence: **personal edge first, community product
second.** The reasoning is the sequencing argument, not a preference between
them — a rationale-backed picks product has nothing to sell without a track
record behind it, so the ledger evidence is the product's entry ticket rather
than a by-product of it.

That makes the community product **dependent on** the personal-edge phase
succeeding, which is a stronger claim than "both". If the personal-edge phase
produces no postable results, the product does not launch on schedule with
weaker numbers — it does not launch.

**Success metric (season 1):** a postable track record — **at least 50 logged
entries in a single contest type, every one attributable to a commit, showing
positive ROI in that cell.**

Deliberately process-shaped rather than "prove positive ROI", because at a solo
evenings-and-weekends pace outcome proof is not reachable this season. Through
January there are ~17 weeks and roughly 3 primetime/standalone Showdown slates
a week (CONTEST_RULES R1). One entry per slate, split across cash and GPP, is
~25 entries per contest type — half of what the minimum-N rule requires before
any conclusion is allowed. GPP needs hundreds.

**What follows from this, and it is not optional:**

- **Concentrate entries in one contest type rather than splitting them.** At
  this volume, splitting guarantees neither cell reaches N and the season ends
  with no conclusion available in either. This is a direct input to H6.
- **Cash / single-entry is the better target for reaching N.** Lower variance
  means 50 entries there supports a claim; 50 GPP entries does not.
- **Padding volume by entering one slate repeatedly does not work.** Those
  entries are correlated — same slate, same projections, often the same lineup —
  so they inflate the count without inflating the evidence. Distinct slates are
  what the minimum-N rule is counting.

**Revisit when:** the season-1 metric is met, or by January, whichever is first.
The product phase is a separate decision made against real numbers, not this one.

## Plan revision — projections forward, validate against history first

**Decided 2026-09-11. Owner, on a proposal from the measurements.**

Two live slates produced a measured error budget, and it contradicts the
roadmap's ordering. The roadmap put the contest simulator first (Step 3) and
projections last (Step 5, January), on the reasoning that projections are a
solved problem you buy and a solo player's edge is process. The measurements:

- projection bias **-5.1 points** per owned player, -11 to -17 on the top plays
  (regression to the mean in `AvgPointsPerGame`)
- the captain slot multiplies that by 1.5, and the optimizer captains the most
  over-projected player by construction
- no teammate correlation — the double-QB stack anti-correlated exactly as the
  placeholder score model cannot represent
- the field generator, which got most of the build effort, is the *least*
  broken component: real fields are 60-70% distinct with a ~1% top build, and
  it is within ~6 points of real ownership

So the projection layer is the dominant error, and it happens to be the only
layer that validates *offline*: seven seasons of `player_week_stats` are
hundreds of validation slates available in minutes, where every other layer
waits one slate per week.

**Decision:** move the projection work forward from January to now, and
validate each layer against its own ground truth before trusting it. This
supersedes the Phase 2+ ordering in `docs/dfs-roadmap-v2.md`, in the same way
that document superseded the July plan.

**The one-improvement-per-month cadence is kept where it applies.** It was
written for a solo builder's build bandwidth. For layers that backtest against
history, the constraint is validation bandwidth and backtesting makes that fast;
for layers that need standings, the bottleneck is genuinely data and the
cadence stands.

**Twelve-week outline:**

1. *Foundation (to ~Sept 25):* load 2019-2025; backtest harness and the leakage
   test CLAUDE.md already requires; projection v1 (shrinkage + Vegas implied
   totals x usage share), shipped only if it beats `AvgPointsPerGame` on
   held-out seasons; empirical variance and correlation from history; T3 as a
   `calibrate` command.
2. *Calibration (Oct):* minimum-stakes entries on every Showdown reachable —
   Sunday games included — for the standings file, not the ROI. `calibrate`
   after each. Field cluster term and captain confidence land against measured
   targets.
3. *Volume (Nov to mid-Dec):* real stakes in one contest type, one entry per
   slate, many slates a week, every entry from committed code. Target is the
   H1 metric: 50 attributable entries.
4. *Mid-December:* decide on evidence. Positive cell -> scale and start the
   product. Otherwise the ledger names the layer to fix.

**Amends CONTEST_RULES R1** for data entries only: any single-game Showdown
qualifies for a minimum-stakes calibration entry. Real-stakes entries still
follow R1-R5 as written. The two are distinguished in the ledger by contest
type and fee.

**Knowingly accepted:** fifty entries supports a claim about cash, not GPP;
GPP edge is a next-season question whatever gets built. "Speed" here is speed
to *knowing whether the machine is calibrated and cash is positive*, which this
plan reaches by December.
