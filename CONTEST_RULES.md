# CONTEST_RULES.md — which contests to enter

Derived from `docs/dfs-roadmap-v2.md` Step 2. Zero modeling: these are pre-entry
checks, applied to a candidate contest before money goes in. The point is that
contest selection stops being a per-week judgment call.

Scope through January 2027 is NFL Showdown slates and soft-field contests only
(CLAUDE.md, "Current scope"). Rules change on ledger evidence, never on how last
week felt.

## How to use

Walk the checks in order against the contest you are considering. **Any hard
check that fails means you do not enter it.** The whole pass should take a
minute; if it takes longer, the contest is probably not a clean fit.

Everything marked **Record** goes into the ledger row:

```
python -m src.ledger add --slate ... --contest ... --type ... --fee ... --lineup ...
```

---

## R1. It is a single-game Showdown slate

**Pass if:** the contest is on a Showdown (single-game) slate.
**Through October, additionally:** the game is a primetime or standalone game.

**Why:** single-game fields are shallower and more predictable, and duplication —
the one mechanic casual players don't model, and the edge Step 3c is being built
around — only bites in a small roster space. Full-slate contests are in the
roadmap parking lot.

**Record:** `--slate` as `{season}-w{week:02d}-showdown-{away}-{home}`, e.g.
`2026-w01-showdown-sea-ne`, matching the `slate_id` convention used by `salaries`.

## R2. The field is soft

**Pass if all three hold:**
- max entries per user is **≤ 3** (single-entry preferred over 3-max)
- it is **not** the headline tournament on that slate
- field size is at the **small end** of what's offered for that game

**Why:** multi-entry contests are where bankrolled players run hundreds of
correlated lineups. A 3-max cap is the cheapest available filter against that.

**Record:** `--field-size`, and `--type` as `showdown_cash` / `showdown_gpp` /
`single_entry`.

## R3. It is not the marquee large-field GPP

**Pass if:** the contest is not the slate's "Milly Maker"-tier headline GPP.

**Why:** the marquee tier is the sharpest, most heavily multi-entered field on
the board. It is the last place to validate a process, not the first.

**Unlock condition:** this check stays closed until the ledger shows *positive
ROI in another contest type* at n ≥ 50 (`src/ledger.py: MIN_N`). For GPP
specifically, treat even a passing cell as directional until entry counts are in
the hundreds — that is the minimum-N rule, and it is what stops a hot fortnight
from reopening this door.

## R4. Effective rake is computed and recorded

**Pass if:** you computed the effective rake and, among otherwise-equivalent
candidates, picked the lowest.

```
effective_rake = 1 - (advertised_prize_pool / (entry_fee x field_size))
```

**Overlay** is the case worth hunting: a guaranteed prize pool that doesn't fill
gives `effective_rake < 0`, meaning the house is adding money to the pool.
Overlay beats a marginally better lineup edge, and it is visible before lock.

**Why:** rake is the one cost that applies to every entry regardless of how good
the lineup is. A process that is +3% before rake and -7% after is a losing one,
and only the ledger will tell you which you have.

**Record:** `--payout-structure` (id of the saved payout table — the same table
Step 3b needs for placement ROI). Save the payout table itself; it disappears
from the site after the contest.

**Note:** there is no hard rake ceiling yet — see H6.

## R5. It fits the written allocation

**Pass if:** entering keeps the week's entries inside the allocation recorded in
`docs/decisions.md`.

The roadmap fixes the *shape* of that allocation — **majority cash /
single-entry while the process is being validated, a minority GPP allocation for
ceiling** — but the exact split and the per-entry stake are money decisions for
the owner, not for this document. Until H6 is answered, this check reads:

> Is this entry consistent with "mostly cash, a little GPP", and is the stake one
> you would repeat 50 times without flinching? The minimum-N rule means you will
> need to.

**Why:** allocation is what converts a positive process into a surviving
bankroll. It also makes the ledger interpretable — ROI by contest type only means
something if the mix was deliberate.

## R6. A human enters it

**Pass if:** a human reviewed the lineup and uploaded it.

**Why:** CLAUDE.md principle 6, no exceptions. Code may recommend lineups and
prepare CSVs; it never enters contests. This is real money.

## R7. It is logged before lock

**Pass if:** `python -m src.ledger add ...` succeeded for this entry.

A successful `add` is itself the proof: the ledger refuses to write from a dirty
working tree, so a row exists only if the code that built the lineup is
committed and the entry is attributable to a commit hash.

**Every entry gets a row, including free ones** — free entries still produce
finish-rank data.

---

## Recording a deviation

Roadmap Step 2's exit criterion is that each week's entries conform to these
rules *or the ledger notes why not*. The `entries` table has no free-text note
column today, so until it does, record deviations in `docs/decisions.md` under a
"Contest rule deviations" heading, one line each:

```
2026-09-14 | contest 12345 | entered 20-max GPP (R2 fail) | reason: $8 overlay, ~40% of pool
```

A deviation is not a failure — the rules are a default, not a cage. An
*unrecorded* deviation is the failure, because it silently poisons the ROI
attribution the ledger exists to produce.

## Not entered, under any circumstances, this season

Straight from the roadmap parking lot:

- Sunday main-slate large-field GPPs
- Any sport other than NFL
- Multi-entry contests above 3-max
- The marquee GPP tier, until R3's unlock condition is met

## Open questions

- **H6** (TASKS.md): the exact cash/GPP split, per-entry stake sizing, and
  whether R4 should carry a hard rake ceiling rather than "prefer the lowest".
  R5 is deliberately soft until this is answered.
