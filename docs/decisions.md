# Decisions

Written decisions, so future-you can't relitigate them from memory. Roadmap
Step 0 asks for a decision "in the repo, that future-you can't argue with" —
this is that file. Each entry: what was decided, when, and what was knowingly
accepted along with it.

Still open, and where they go: **H3** (late-swap news feed), **H6** (cash/GPP
allocation and stake sizing). See the human-only queue in `TASKS.md`.

---

## H10 — Expand scope to DK Classic main-slate GPPs

**Decided 2026-09-20. Owner: yes, undoubtedly — expand to Classic. Showdown
stays in scope alongside it, not replaced by it** ("more showdown
opportunities" than expected, so both games are worth playing).

Asked for a lineup on the day's 1pm/4pm ET Classic window (11 games, DK
Classic roster). Declined in the moment per the scope line in `CLAUDE.md` at
the time ("NFL Showdown slates ... only. Not Sunday main-slate large-field
GPPs"), and the question was put back to the owner rather than guessed at.
Confirmed: expand.

**This supersedes the "Scope through January" line in
`docs/dfs-roadmap-v2.md`** ("NFL Showdown slates + soft-field contests
only"), the same way the Sept 11 plan revision superseded that document's
Phase 2+ ordering — the roadmap document itself is left as the historical
record; this entry is the current instruction.

**What transfers from the Showdown build, and what does not.** The
projection layer (T16/T20/T21/T22/T23/T24 — ingest, roles, `RoleAware`, the
empirical score marginals) is per-player and per-position, not
Showdown-shaped, and carries over close to as-is. Everything downstream does
not and needs real, separate work, each piece on the order of what its
Showdown counterpart took:

- **Roster/optimizer.** `src/showdown.py` and the optimizer mode in
  `src/ownership.py` hardcode 1 CPT + 5 FLEX, one cap, two teams. Classic is
  QB/RB/RB/WR/WR/WR/TE/FLEX/DST across as many teams as have a game in the
  window. New module, new optimizer mode, not a parameter change.
- **Correlation.** `src/scoremodel.build_correlation` is built on "a
  Showdown slate is one game" — every player pair in the pool is scored
  against every other. Across 11 independent games that structure is wrong;
  it needs to be block-diagonal by `game_id`, correlated within a game and
  ~0 across games (unless a real cross-game effect is measured, e.g. weather
  or pace-of-play — not assumed, checked). Genuinely new work, not a
  reparameterization.
- **Field, ownership, duplication.** The jitter=0.5 calibration and the T15
  chalk cluster (share, jitter) were fitted against exactly two real
  single-game Showdown standings files. A Classic GPP field is a different
  animal — far larger, spread across many games, different concentration
  dynamics — and needs its own real standings to calibrate against from
  scratch. No Classic standings are archived yet; a Classic H2 equivalent
  (download every entered contest's standings) is the prerequisite, same as
  it was for Showdown.
- **Captain confidence (T19).** Does not port as a concept — there is no
  captain slot. The underlying idea (choose by floor/ceiling, not mean) still
  applies to which players anchor a Classic build, but the mechanism needs
  redesigning around stack construction instead.

**Knowingly accepted:** none of this ships fast. Showdown took from Sept 9 to
Sept 20 to go from nothing to a calibrated, validated pipeline, working
evenings-and-weekends. Classic is comparable in size. **No lineup was built
for the slate that prompted this decision** — building one against zero
validated infrastructure would repeat exactly the mistake the ledger and the
leakage guarantee exist to prevent, on the same day the decision was made.

**Sequencing is still open** — whether Classic runs as a second monthly-
cadence track alongside Showdown or takes over the cadence until it reaches
Showdown's current maturity is an owner pacing call, not decided here. See
the Classic backlog items added to `TASKS.md`.

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
