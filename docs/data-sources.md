# Data sources: what's free, what's stale, what's blocked

Findings from building Phase 1 milestones 1–3. Everything here was verified by
fetching the real endpoints unless explicitly marked otherwise.

## Summary

| Source | Status | Notes |
| --- | --- | --- |
| nflverse player week stats | ✅ working, 2019–2025 | Not via `nfl_data_py` — see below |
| nflverse team week stats | ✅ working, 2019–2025 | The DST inputs |
| nflverse schedules + closing lines | ✅ working | Free historical spreads/totals |
| `nfl_data_py.import_ids()` | ✅ working | ~12.5k players, 7.5k with both GSIS + PFR |
| `nfl_data_py.import_snap_counts()` | ✅ working | 99.1% joined to our stat rows |
| DK salary export (forward) | ⚠️ manual download | The durable path; no API |
| DK salary history (RotoGuru) | ❓ **unverified** | Blocked by sandbox egress, not confirmed dead |
| The Odds API | ⚠️ not needed in Phase 1 | Backtest uses free nflverse closing lines |

## `nfl_data_py` has two stale URLs

The spec's §5.1 snippet (`nfl.import_weekly_data(range(2019, 2026))`) does not
work as written. Two problems, both routed around in `src/nflverse.py`:

**1. `import_weekly_data` cannot see 2025.** It reads the `player_stats` release
tag, which nflverse froze at 2024. Current data lives under the `stats_player`
tag with a different filename:

```
# stale (library default) — 404 for 2025
.../releases/download/player_stats/player_stats_{year}.parquet     # 2019-2024 only

# current — what we use
.../releases/download/stats_player/stats_player_week_{year}.parquet  # 2019-2025 ✓
```

Verified: both tags return byte-identical schemas (145 columns) for overlapping
years, so switching costs nothing. `src/nflverse.py` tries the current tag first
and falls back to the legacy one.

**2. `import_schedules` single-sources over plain HTTP.** It reads
`http://www.habitatring.com/games.csv`. That host is unreachable from restricted
networks. The identical file is mirrored on GitHub over HTTPS, which we prefer,
keeping habitatring as a fallback:

```
https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv
```

This file is the reason Phase 1 needs **no purchased historical odds** — it
carries `spread_line` and `total_line` (closing) for every game back to 1999.

We still use `nfl_data_py` for `import_ids()` and `import_snap_counts()`, which
both work fine.

## Column name gotchas

- The interceptions-thrown column is `passing_interceptions`, **not**
  `interceptions`. Reading the wrong one silently yields zero for every QB.
- Fumbles lost are split across three columns that must be summed:
  `rushing_fumbles_lost`, `receiving_fumbles_lost`, `sack_fumbles_lost`.
- Two-point conversions are likewise split three ways (passing/rushing/receiving).
- `stats_player_week` carries `game_id` directly — no schedule join needed.

## DST: assembled, not provided

nflverse publishes no DST fantasy rows, so `src/ingest/nfl_stats.py` builds them
from `stats_team_week` (sacks, INTs, fumble recoveries, defensive TDs, safeties,
return TDs) plus points allowed from the game's final score.

Two known limitations, both stated rather than silently absorbed:

1. **Points allowed = opponent's final score.** DK excludes points scored by the
   opposing *defense* against your own offense (a pick-six you threw shouldn't
   count against your DST). Correcting this needs play-by-play attribution.
   Affects a minority of games; it's the most likely source of DST drift.
2. **Blocked kicks are recorded as 0.** nflverse books blocked FGs/punts on the
   kicking team's row, not the blocking defense's. Worth +2 each when it happens.

Spot-checked against real results: Dallas W1 2023 (40-0 shutout, 7 sacks, 2 INT,
1 fumble recovery, 1 defensive TD, 1 return TD) scores 35.0 — correct.

## DK salaries: the one input that isn't free

**Forward (durable):** DK's draft screen has *Export to CSV*. Download it every
week, drop it into `data/raw/salaries/` named `2026-w01_dk_main.csv`.
`src/ingest/dk_salaries.py` parses it. There is no public DK API for this — it's
a manual download, which is exactly why the spec says archiving is the durable
answer.

**Historical: unresolved.** The sandbox this was built in allows egress to
GitHub and package registries only; `rotoguru1.com`, `draftkings.com`, and
`api.the-odds-api.com` all return `Host not in allowlist`. So **RotoGuru was not
evaluated** — it may work perfectly from an unrestricted machine. Do not read
this as "RotoGuru is dead."

A RotoGuru adapter is implemented from its documented semicolon export format
but is **unverified against a live response**. Before trusting any backfill:

```bash
python -m src.ingest.dk_salaries probe --season 2023 --week 1
```

That prints `OK` with a parsed preview, `REACHABLE but UNPARSEABLE` with the raw
response (format changed — fix the adapter), or `UNREACHABLE` (still blocked, or
genuinely gone). Parsing is header-driven, so a column reorder is survivable; a
wholesale format change fails loudly rather than backfilling garbage salaries.

Until historical salaries exist, the backtest (Milestone 6) cannot replay past
slates. The spec's §5.3 option 3 — a naive trailing-average projection — still
exercises the rest of the pipeline.

## Schema additions

Five documented departures from the spec's §3 DDL, all additive:

| Table | Addition | Why |
| --- | --- | --- |
| `games` | `home_score`, `away_score` | Needed to derive DST points allowed; free in the same row |
| `player_week_stats` | `st_tds` | Return TDs are 6 real DK points; `dk_points` is wrong without it |
| `dst_week_stats` | whole table | Team defense stats don't fit the player columns |
| `id_crosswalk` | `match_method` values `dst_map`, `exact_name_pos` | Keeps join provenance auditable |
| `entries` | whole table | The experiment ledger (roadmap v2 Step 1, not in the July spec) |

Scored DST rows are also mirrored into `player_week_stats` under
`player_id = 'DST_<TEAM>'` so the optimizer can treat every DK roster slot
uniformly.

## The ledger's dirty-tree guard depends on `.gitignore`

`src/ledger.py` refuses to log an entry while `git status --porcelain` reports
anything, so that every entered lineup is attributable to a commit. That check
counts **untracked** files too, which makes it quietly dependent on
`.gitignore` staying accurate: the DB (`data/dfs.sqlite`) and its `-wal`/`-shm`
siblings are ignored, so ordinary pipeline runs don't trip it.

If a future step writes a generated file that isn't ignored — a scratch CSV, a
sim cache, a notebook checkpoint — every ledger write starts failing 15 minutes
before lock, which is the worst possible time to debug it. Ignore new generated
artifacts when you add them, not after.

## The optimizer identifies players by name, not by ID

`pydfs-lineup-optimizer`'s DK Showdown mode (`Site.DRAFTKINGS_CAPTAIN_MODE`)
wants **two pool entries per player** — one with position `CPT`, one with
`FLEX` — mirroring the two rows DK's own export gives you. Verified by running
it: it enforces the $50,000 cap, the 1 CPT + 5 FLEX shape, and DK's
both-teams-represented rule without extra constraints.

The trap is how it keeps one person out of both slots: **it matches on the
player's name, not on the ID you pass it.** Two different players who share a
name would be silently collapsed into one, and one of them would never appear
in a lineup. `src/ownership.py` passes the canonical GSIS id as the name for
exactly this reason; the display name is rejoined afterwards. If lineups ever
start coming back short, or a player never appears no matter the projection,
check this first.

Salaries stored in the DB are the FLEX/UTIL base, so `src/ownership.py` applies
the 1.5x captain multiplier to both salary and points itself rather than reading
DK's pre-multiplied CPT row.

## Ownership targets are not exactly reachable in a legal field

Ownership estimates are *marginal* rates — each player's share of lineups
independently. The salary cap makes players compete for the same roster, so
those marginals are not jointly satisfiable: sampling lineups slot by slot and
rejecting illegal ones systematically suppresses expensive players, and the
error plateaus around 9-13 points no matter how large the field gets. It is
bias, not noise, so a bigger field does not fix it.

`src/field.py` closes most of the gap with a repair pass that swaps over-target
players out of lineups for under-target ones, keeping only legal results. That
brings the residual to about 0.03 per slot for fields of 100-5,000. The
remainder is structural.

Two consequences worth remembering when Step 3 gets calibrated against real
standings: a field that matches estimated ownership to within ~3 points is as
good as this method gets, and any comparison of estimated to actual ownership
inherits that floor — do not read a 2-point discrepancy as a modelling error.

## DK Showdown export: verified against a real slate

From the 2026-w01 NE@SEA export (68 players, 136 rows):

- **Two rows per player**, one `CPT` and one `FLEX`, no exceptions.
- **CPT salary is exactly 1.5x the FLEX salary**, for all 68 — confirming the
  convention of storing the FLEX base and applying the multiplier in code.
- **The file is sorted by salary descending**, so a player's CPT row always
  precedes their FLEX row. This is load-bearing by accident: `salaries` is keyed
  `(slate_id, dk_name)`, so loading a Showdown export upserts both rows onto one
  key and the *last* one wins. Today that is FLEX, which is correct. If DK ever
  changes the sort, every Showdown salary silently becomes 1.5x too high. See
  T11.
- **`Status` marks OUT / IR / Q** — 22 of 68 players were OUT or IR on this
  slate. A pool built without filtering them will happily build lineups around
  players who never take the field. It is also the free late-swap signal roadmap
  Step 4 asks for: it is already in the export, no news feed required.
- **`AvgPointsPerGame` is populated** and is a usable interim projection, but in
  Week 1 it carries *last season's* average — 16 of the 46 playable players had
  zero. Fine for a duplication-aware baseline, weak as a projection.

Raw archives are gitignored (`data/raw/**`), so an archived export lives only on
the machine that downloaded it. Keep your own copy; a fresh clone will not have
it.

## Duplication can invert a lineup's value

Running T6 over tonight's real slate, the highest-projected build finished
*better* than a lower-projected alternative on every conventional measure —
higher projection, better median rank (954 vs 1,368 of 3,001), higher cash rate
(30% vs 23%) — and was still far worse to enter. It appeared 143 times in a
3,000-entry field against the alternative's 6, so its prizes were split ~143
ways.

The mechanism is tie splitting: identical lineups score identically in every
trial, tie, and share the pooled prize for the ranks they occupy. That is why
`simulate_contest` settles ties the way DK does rather than breaking them
arbitrarily — an arbitrary tiebreak would hide exactly the effect the Showdown
strategy is built to exploit.

The practical lesson for reading sim output: median rank and cash rate can both
favour the lineup that is worse to enter. ROI is the metric (CLAUDE.md principle
5), and in a Showdown it is duplication, not projection, that most often decides
it.

## Duplication needs score variance to invert a ranking

Building T7 turned up a bound on the whole Showdown strategy that is easy to
state wrongly.

Under **deterministic** scores, duplication can never make a worse lineup the
better entry. If your lineup always outscores mine, your k copies occupy the
ranks above me: your split share is the *average* of the top k+1 prizes, and I
take the (k+1)th. In any decreasing payout table that average is never smaller.
Verified across k = 1, 2, 4, 8, 12 — no inversion at any k.

What actually drives the inversion is **variance**. Once scores vary, the
less-duplicated lineup sometimes finishes above the duplicated block, and on
those trials it keeps a prize it would otherwise have split. With a lineup only
marginally behind the chalk (22 vs 20 projected) and 8 copies of the chalk in
the field, the effect appears at a coefficient of variation of about 0.15 and
grows from there.

Two consequences:

- **Duplication does not rescue a bad lineup.** It rewards a lineup that is
  *close* to optimal and rarely built. The leverage play is a near-miss on
  projection, not a contrarian punt.
- **The size of the effect depends entirely on the score model**, which is
  currently `independent_normal_scores` — a placeholder with a flat coefficient
  of variation and no correlation between teammates (T13). It gets the direction
  right, and its magnitudes should not be trusted. Real per-player variance,
  which props would supply (H8), is what makes the dup-adjusted ROI numbers
  quantitative rather than directional.

## `jitter` controls the simulated field, and its default was a guess

The `jitter` parameter on `estimate_ownership` defaulted to 0.15, documented as
"a reasonable light default". It was never calibrated, and it turns out to
govern the most important property of the simulated field. On the 2026-w01
NE@SEA pool (46 playable, 5,000-entry field):

| jitter | top player owned | distinct lineups / 5,000 | chalk build |
| --- | --- | --- | --- |
| 0.15 | 96.8% | 746 | 4.76% |
| 0.30 | 84.2% | 1,943 | 0.82% |
| 0.50 | 71.2% | 3,284 | 0.18% |
| 0.80 | 61.0% | 4,042 | 0.08% |

96% ownership for one player is not a real field, so 0.15 is wrong and every
duplication figure produced at it is inflated. But the signal does **not**
disappear at plausible settings, which an earlier revision of this note claimed:

| jitter | chalk build | differentiated build | ratio |
| --- | --- | --- | --- |
| 0.30 | 0.82% (~41 of 5,000) | 0.14% (~7) | 5.9x |
| 0.50 | 0.18% (~9 of 5,000) | 0.02% (~1) | 9.0x |

So duplication is real and worth modelling; the magnitude at the old default was
roughly 8x too large.

**Two methodology traps, both hit while measuring this.** Duplication is a
rare-event rate, so a field must be large enough to resolve it — a 1,500-entry
test put both builds at ~1 copy and made a genuine 9x difference look like 1.0x.
And the whole curve is a modelling artefact until real standings anchor it:
matching *marginal* ownership is not obviously enough to reproduce duplication,
which is a *joint* property of many entrants converging on one combination.
Whether the marginal sampler is adequate, or the field needs an explicit
chalk-cluster component, is an empirical question (T15) that only real contest
standings answer.

Until then, treat duplication output as directionally useful and quantitatively
unanchored — including T6's dup-adjusted ROI, which inherits it.

## Calibration against real standings: 2026-w01 NE@SEA, 2,373 entries

The first real contest standings, and they settle several open questions.

**`jitter` should be ~0.5, not 0.15.** Mean absolute error against real total
ownership, over all players in the pool:

| jitter | MAE (pts) | JSN | Maye | Stevenson |
| --- | --- | --- | --- | --- |
| real | — | 71.7 | 76.3 | 63.7 |
| 0.15 | 9.4 | 96.8 | 95.5 | 22.5 |
| 0.30 | 6.9 | 84.2 | 86.8 | 38.7 |
| **0.50** | **5.7** | **71.2** | **75.2** | 42.5 |
| 0.80 | 6.1 | 61.0 | 66.5 | 44.0 |

At 0.5 the top two players land within a point of reality. The old default was
25 points high on both.

**The marginal sampler cannot reproduce a real field, and this is now measured
rather than suspected.** Real fields are simultaneously *diverse* and
*clustered*:

| | distinct lineups | most-entered build |
| --- | --- | --- |
| real | 61.5% | 1.10% (26 of 2,373) |
| jitter 0.30 | 38.9% | 0.82% |
| jitter 0.50 | 65.7% | 0.18% |

The jitter that matches diversity understates duplication about 6x, and the one
that gets closer on duplication is far too concentrated. No single value does
both, exactly as T15 predicted — the field needs an explicit chalk-cluster
component (a share of entrants converging on the same few builds) layered on a
diffuse sampler, not a better parameter.

**The dominant error is the projection, not the field.** `AvgPointsPerGame`
rated Jadarian Price at 0.0; the real field rostered him **48.1%** of the time.
He was Seattle's starting back with Charbonnet and Henderson both OUT, which
last season's average cannot know. The model gives him 0% ownership at every
jitter value, so no amount of field tuning recovers him. Elijah Arroyo (2.71
projected) appears in the single most-entered build. Every player whose role
changed since last season is invisible in the same way, and on this slate that
was the highest-owned value play on the board.

**One thing the model got exactly right:** the logged `dup_estimate` of 2 for
the entered lineup matched the standings exactly — 2 identical entries, tied at
rank 365. n=1, so that is encouraging rather than evidence.

## Minimum-priced players break the optimizer's value ratio

The mirror image of the role-change problem. DK prices a player at $200-$300 to
say *he will not play*. `AvgPointsPerGame` sometimes gives such a player a small
non-zero average from garbage-time snaps, and the ratio explodes: on the
2026-w01 SF@LAR slate Xavier Smith read $200 and 2.5 projected — **12.5 points
per $1,000**, five times the best real play on the board. The optimizer reached
for a player at or below $1,000 in 23% of runs and put one in the top-projection
lineup.

So the projection is wrong in both directions, for opposite reasons:

- **role changes make it too low** (Jadarian Price, 0.0 projected, 48.1% owned)
- **minimum pricing makes the value ratio too high** (Xavier Smith, 12.5 pts/$1k
  for a player DK has priced as inactive)

A salary floor around $1,200 removes the second class without touching any real
contributor — the cheapest genuine plays on that slate were a $1,400 fullback
and a $2,600 tight end. It is a blunt instrument and it works because DK's
pricing carries the information the projection lacks: the salary *is* the
market's playing-time estimate, which is exactly what a prior-season average
cannot see. Worth preferring over hand-zeroing individual players, which does
not generalise to next week's slate.

## Second slate: the recommendation lost, and the projection is why

2026-w01 SF@LAR, 2,369 entries. The lineup this pipeline recommended —
CPT Stafford, with Nacua / McCaffrey / Purdy / Higbee / Juszczyk — projected
113.2 and **scored 58.15**, which would have finished about 1,215th of 2,369.
The winner scored 98.05. Worth recording in full, because the failure modes were
the ones already filed as open tasks, and one of them is new.

**Projections are systematically optimistic, and it is regression to the mean.**
Across the fifteen players owned above 15%, MAE was 7.2 points with a bias of
**-5.1** — not scatter, a consistent overshoot. The three highest-projected
players were the three biggest misses:

| player | projected | actual | error |
| --- | --- | --- | --- |
| Puka Nacua | 25.1 | 12.4 | -12.7 |
| Christian McCaffrey | 24.8 | 13.8 | -11.0 |
| Matthew Stafford | 22.1 | 5.1 | -17.0 |

`AvgPointsPerGame` is a per-game average from last season, so the top of the
board is populated by whoever sustained the highest average — precisely the
players a new season regresses downward. The fix is shrinkage toward a positional
mean, weighted by how many games the average rests on (T18), not a better
feed.

**The captain slot multiplies that error by 1.5.** Stafford at 22.1 projected
should have returned 33.2 as captain and returned 7.6. Because the optimizer
captains whoever maximises projected points, it systematically captains the most
over-projected player on the board, where the bias above is largest. Captain
selection needs to weight *confidence*, not just level — a lower-projection,
higher-floor captain is worth more than the raw arithmetic says.

**The independent score model failed exactly where predicted (T13).** Purdy
scored 22.1 and Stafford 5.1: one offence worked and the other did not, which is
the single most important structure in a single-game slate and the one thing
`independent_normal_scores` cannot represent. The double-QB stack was a bet on
correlation, and nothing in the simulator could evaluate it.

**T15's target replicates.** Field shape across both slates:

| slate | distinct lineups | top build |
| --- | --- | --- |
| NE@SEA | 61.5% | 1.10% |
| SF@LAR | 68.7% | 1.06% |

Real Showdown fields are 60-70% distinct with a most-entered build near 1%.
That is now a stable calibration target rather than one slate's number.

**`jitter` is not stable across slates.** At 0.5 it nailed NE@SEA's top two
(within a point) and understates SF@LAR's by 13-19 points, with MAE around 6 on
both. So 0.5 is about as good as marginal sampling gets, not a correct value —
consistent with T15's finding that the shape is wrong, not the parameter.

**The salary floor belongs in selection, not in the field model.** T17's $1,200
floor is right for choosing a lineup: Xavier Smith, the 12.5 pts/$1k artefact,
drew 3.4% ownership and scored 0.0. But applying it to the ownership estimate
made calibration *worse* (MAE 8.8 floored vs 6.4 unfloored), because real fields
do roster a few minimum-priced players. Filter the pool the optimizer picks
from; do not filter the pool used to model opponents.

## What a Showdown score model has to reproduce (measured, 2020-2025)

Residuals are actual minus the trailing-average projection, over 34,936
player-weeks. These are the numbers `independent_normal_scores` was standing in
for, and they contradict it in three specific ways.

**Spread grows with projection, but sub-linearly — not a constant cv.**

    resid sd = 3.81 + 0.310 * projection        (R^2 = 0.96 across 10 bins)

| position | a | b | R^2 |
| --- | --- | --- | --- |
| QB | 7.90 | 0.037 | 0.36 |
| RB | 4.31 | 0.283 | 0.97 |
| WR | 3.57 | 0.364 | 0.98 |
| TE | 2.53 | 0.426 | 0.99 |
| DST | 5.10 | 0.057 | 0.88 |

The placeholder's flat cv of 0.55 was about right for a 20-point player
(measured 0.51) and badly wrong for a 2-point one (measured ~2.0). QBs are
noisy at every level; a QB's spread barely depends on how good he is.

**The residual is right-skewed with fat tails, not normal.** Skew +0.93,
excess kurtosis +2.28. A player scores under half his projection **33%** of
the time and over double it **13%**. Fantasy scoring is bounded below at zero
and unbounded above, so the marginal wants a right-skewed non-negative family
(gamma or lognormal) with the mean set to the calibrated projection and the sd
from the table above.

**Correlation is a small block structure; almost everything else is zero.**
Residual correlation between players in the same game, pairs with n >= 500:

| pair | relation | r |
| --- | --- | --- |
| QB - his highest-projected pass-catcher | teammate | **+0.327** |
| QB - WR | teammate | +0.213 |
| QB - TE | teammate | +0.170 |
| QB - QB | opponents | +0.185 |
| DST - QB | opponents | **-0.251** |
| DST - RB | opponents | -0.155 |
| DST - WR | opponents | -0.103 |
| DST - QB | teammate | -0.106 |
| QB - RB | teammate | +0.028 |
| WR - WR | teammate | +0.009 |
| RB - WR | teammate | -0.007 |

Three things worth reading off that. The stack is real: a QB and his top
target move together at r = 0.33, which is the correlation the SF@LAR
double-QB lineup was betting on without any way to price it. Opposing QBs are
positively correlated (+0.185) — shootouts happen to both offences — which is
the game-stack signal. And a DST is negatively correlated with the offence it
faces (-0.25 with the QB), so DST + opposing QB is anti-stacked. Everything
else — two receivers on the same team, a QB and his RB — is within noise of
independent.

**Design that follows (T13):** draw a correlated standard normal per lineup
slot from a matrix built from the block structure above, then map each through
the gamma quantile with that player's calibrated mean and position-fitted sd.
A Gaussian copula with gamma marginals. Every number in it is measured; nothing
is a placeholder.

## Projection backtests: what seven seasons say about the live-slate diagnosis

The first use of the backtest harness, 2020-2025, 34,936 player-weeks over 107
weeks, every model scored on the same players. The baseline is `PriorAverage`,
a trailing 17-game per-game mean — what DK's `AvgPointsPerGame` is.

**v1 (`ShrunkVegas`) lost, and the ablation says why.**

| model | MAE | RMSE | Spearman | bias |
| --- | --- | --- | --- | --- |
| baseline | 5.00 | 6.77 | 0.606 | +0.17 |
| shrink only, k=4 | 5.06 | 6.74 | 0.599 | +0.21 |
| shrink only, k=1 | 5.00 | 6.73 | 0.606 | +0.19 |
| vegas only | 5.08 | 6.92 | 0.597 | +0.31 |
| both (v1) | 5.11 | 6.82 | 0.583 | +0.32 |

Multiplying a whole team by its Vegas implied total is what broke v1: alone it
is worse on every metric. Shrinkage by n/(n+k) is harmless at k=1 and
over-shrinks at k=4. Neither is the fix. Implied totals are not useless — they
predict *team* scoring — but scaling every player on the roster by the same
factor adds more noise than signal; if they help at all it will be through a
usage-share model, not a multiplier.

**The two live slates were right about the direction and wrong about the
size.** Overall bias is +0.17, and +0.81 on week 1 — not the +5.1 those two
low-scoring games showed. But by projection decile the regression-to-the-mean
effect is real and monotonic:

| decile | projected | actual | bias |
| --- | --- | --- | --- |
| 1 | 1.2 | 2.2 | -0.98 |
| 5 | 6.6 | 6.3 | +0.29 |
| 10 | 20.4 | 19.0 | +1.44 |
| top 5% | 22.6 | 20.5 | **+2.03** |
| top 5%, week 1 | 23.2 | 20.7 | **+2.50** |

The bottom projects low, the top projects high, and the overall bias is near
zero only because they cancel. This is why n/(n+k) shrinkage could not help: a
17-game veteran gets weight ~1 — no shrinkage — and veterans are exactly who
sit at the top. The bias depends on projection *level*, not sample size. It
also sizes the captain problem (T19): the slot that pays 1.5x is filled from
the top 5%, where the projection runs +2.0 to +2.5 high.

**v2 (`CalibratedAverage`) — a global linear calibration — is a wash.** Fit
actual on the trailing average within time-boxed history, apply the line. Rank
order is preserved exactly (Spearman 0.606 -> 0.606, as it must be for a
monotone map), overall bias falls to +0.06, RMSE 6.77 -> 6.74, but MAE 5.00 ->
5.03. The per-position rows explain it: the fitted slope over-corrects QBs
(bias -1.11) and under-corrects TE and DST (+0.4). **Regression to the mean
differs by position.** v3 fits one line per position.

**v3 (`calibrated_by_position`) removes the position bias and is otherwise a
wash.**

| model | MAE | RMSE | Spearman | bias | QB bias | TE bias |
| --- | --- | --- | --- | --- | --- | --- |
| baseline | 5.00 | 6.77 | 0.606 | +0.17 | +0.31 | +0.03 |
| v2 global | 5.03 | 6.74 | 0.606 | +0.06 | -1.11 | +0.41 |
| v3 per position | 5.01 | 6.73 | 0.604 | +0.07 | +0.31 | +0.09 |

Every position's bias is now within 0.3 of zero, RMSE is the best of the three,
and MAE has not moved. That is the expected shape of the result, not a
disappointment: **calibration removes bias, and MAE is dominated by variance.**
The residual sd is 5-9 points against a 1-2 point bias, so flattening the bias
barely registers in absolute error. Reducing MAE needs *information* the
trailing average does not have — usage trends, matchup, a proper Vegas model —
not a recalibration of the same average.

What v3 is for is the thing MAE does not measure: the +2.0 systematic error at
the top of the board, which the captain slot multiplies by 1.5 and which feeds
every expected-ROI number. No projection model is wired into the live lineup
path yet — pools still read `AvgPointsPerGame` directly — so nothing ships by
this result alone. When one is wired in, the choice between the baseline and v3
is the owner's, with this table as the evidence. The ship rule in TASKS.md
should distinguish a *calibration* step (criterion: bias by decile near zero,
rank preserved) from a *prediction* step (criterion: MAE and Spearman); v1 and
v3 were held to the wrong one of those.

**v3 by decile — the claim, measured.**

| decile | baseline bias | v3 bias |
| --- | --- | --- |
| 1 (lowest projections) | -0.98 | +0.32 |
| 5 | +0.29 | +0.17 |
| 10 (highest) | +1.44 | -0.31 |
| top 5% | **+2.03** | **-0.19** |
| top 5%, week 1 | **+2.50** | **+0.91** |

The baseline's bias is a monotone ramp across deciles; v3's stays inside +/-0.32
at every level. That is what "calibration removes bias" looks like in the
place it matters. One honest residual: week 1 is only two-thirds corrected. The
prior season's average is at its stalest there and a calibration pooled over
all weeks under-corrects it. If week-1 slates keep mattering — and under the
Showdown-only scope they do — week 1 wants its own line, or a stronger shrink.
Filed under T18's remaining work rather than solved here.

**Method note.** Each of these is a claim that was cheap to test and would
have gone into a lineup untested a week ago. v1's hypothesis came from two
slates; seven seasons refuted it as stated in about four minutes. That is the
harness earning its place, and it is the reason the projection layer is being
built before the field layer is touched again.

## T13 validated: the correlated score model reproduces a real stack

`src/scoremodel.py` — a Gaussian copula over lognormal marginals with the
fitted variance function and the measured correlation blocks — checked against
the realized covariance of QB + top-target pairs across all 107 weeks of
2020-2025 (2,594 pairs), with two same-team receivers as the independent
control (2,587 pairs):

| | realized | simulated |
| --- | --- | --- |
| QB - top target residual correlation | +0.349 | +0.282 |
| WR - WR (control) residual correlation | -0.019 | +0.014 |
| sd of the QB + top target summed score | **14.29** | **14.13** |
| ... if the two were independent | — | 12.50 |

The number that matters for a lineup is the last pair of rows. A stacked pair's
score is 1.8 points more volatile than independence would predict, and the
simulator reproduces that within 1%. The placeholder gave 12.50 — it could not
see the stack at all, which is why the SF@LAR double-QB lineup was entered as a
bet nothing in the pipeline could price. The simulated pairwise correlation
sits a little under the realized one because Pearson on a lognormal runs below
the copula's underlying value; the covariance in points, which is what
placement depends on, comes out right.

Stated limits: the correlation table is pooled across 2020-2025 and every game
state; it is not conditioned on spread or total, and the fit and the check use
the same seasons. A held-out check waits for the 2026 archive to grow.

## The team-change blind spot: a stale ROLE, not a stale name (T20)

Caught by the owner on the first live run of v3, not by any test: the
recommended DEN@KC lineup rostered **Justin Fields as cheap value at $8,600**,
v3-projected 14.6 points, resolved cleanly to real NFL history. The owner's
objection was immediate and correct — Fields will not play for KC unless
Mahomes is hurt, and a confident 14.6 for a player realistically getting zero
snaps is not a number to enter money on.

Checked directly: Fields' entire trailing window is **1.0 snap_pct at NYJ
through week 11 of 2025** — a full-time starter, at a different team, in a
role he no longer holds. A second player in the same pool had the identical
defect: Sam Ehlinger resolved to a single full-snap start at IND **in 2022**
and got 7.9 points from it, three-plus years and two teams removed from
today's bench role at DEN.

The mechanism: a trailing average is a snapshot of the role a player held
*when the history was recorded*, not a property of their name. An offseason
team change invalidates that snapshot completely, and this project's database
has no way to know one happened — the 2026 season is not ingested at all
(`config.yaml` seasons end at 2025), so a trade or a free-agent signing since
the last loaded season is **structurally invisible** until games are actually
played and ingested. Falling back to `AvgPointsPerGame` does not fix this: DK's
own average is built from the identical stale history, so it carries the exact
same blind spot as v3.

This is the mirror case of T16 (Jadarian Price, projected 0 despite starting)
in mechanism only — both are "the model has no notion of current depth-chart
role" — but opposite in shape and arguably more dangerous. T16 produces an
obviously-wrong 0 that invites scrutiny. This produces a plausible,
GSIS-resolved, confidently-labeled "v3" number sitting right next to real
starters, which is precisely what makes it easy to miss.

**Fix, shipped:** `src.resolve.resolve_pool` now flags `team_changed` —
comparing a resolved player's most recent known team against the export's
team. `src.liveproj.project_live_pool` refuses v3 for anyone flagged, whether
or not they clear `min_games`, and tags them `proj_source="team_changed"` — a
distinct value from plain `"avg_points"` so they cannot blend in with an
ordinary unresolved player. The CLI (`scripts/live_showdown.py`) prints every
flagged player as a loud warning before showing any recommendation, naming the
stale number and pointing at the override mechanism.

**What the fix does not do:** guess the player's real role. It can only say
"do not trust this number," not supply a better one. That still requires a
human's own knowledge of the depth chart, recorded as a committed override —
exactly the mechanism built for Stribling, now used again for Fields and
Ehlinger. A cheap general improvement worth doing later: weight DK's own
salary as a role signal for flagged players specifically, since a genuine
backup is rarely priced at $8,600+ captain-eligible the way a timeshare or
new starter would be — but salary reflects name recognition too (see the
Xavier Smith case, T17), so it is a hint, not a substitute for review.

## Third live result: DEN@KC, and T19 confirmed a second time

Projected 94.7, actual **56.5** (computed via `src.scoring.score_offense` from the
real box score, not a fantasy-site total). No cash, finished roughly rank 2000
of ~3000. Standings expired before they could be downloaded — the first missed
calibration opportunity this season; see the H2 note below.

**Real result: DEN 10, KC 31.** A 21-point blowout, nothing like the
competitive game the field/ownership model assumed. Four of six roster spots
were on the losing offense, including the captain.

**T19 (captain confidence) is now confirmed twice, not once.** SF@LAR
captained Stafford at the top of a range the backtest later showed running
+2.0 to +2.5 high; here the optimizer captained J.K. Dobbins — highest mean
among cap-feasible options — who returned 3.6 points on 8 carries and **zero
targets**, the single worst game script a non-receiving back can draw. The
mechanism is the same both times: the optimizer selects a captain purely on
projected mean, with no notion that a receiving-dependent back in a bad game
script, or an over-projected top-of-board player, carries more downside than
its point estimate shows. Two for two on the first two live pipeline
lineups is enough to stop calling this theoretical.

**The other failure was the tail landing, not a bug.** Troy Franklin — flagged
in this doc as the standout value pick, 3.51 pts/$1k, the best on the board —
returned a hard zero (1 target, 0 catches). The measured residual distribution
already said a player finishes under half his projection about a third of the
time; a cheap, low-target-share player going to literal zero in a blowout is
squarely inside that, not a surprise the model should be expected to have
ruled out. Worth remembering when presenting a "best value" pick going
forward: cheap and high point-per-dollar also means high variance, and that
should be said in the same breath as the recommendation, not after the fact.

## Where the projection's error actually lives (measured, held-out 2024–2025)

Asked after DEN@KC: what is the projection model missing? Three experiments,
all on `player_week_stats` 2020–2025 (QB/RB/WR/TE), ridge regression fit per
position on 2020–2023, evaluated on 2024–2025 (11,232 player-weeks), players
with ≥3 prior games. Every feature is computed from rows strictly before the
row's `(season, week)`, same rule as the harness. Scripts were one-off
(scratchpad); the numbers are reproducible from the DB.

**1. What the trailing history can still add — about 4%.**

| features (on top of trailing-17 mean)             | MAE  | Spearman | top-12-in-game ρ |
|---------------------------------------------------|------|----------|------------------|
| trailing-17 `dk_points` only (= v3's input)        | 4.83 | 0.663    | 0.488            |
| + recency (trailing-3, EWM half-life 4)            | 4.74 | 0.680    | 0.499            |
| + trailing-17 usage (targets, carries, snaps, …)   | 4.82 | 0.665    | 0.484            |
| + usage trailing-3 + last-week snap%               | 4.66 | 0.695    | 0.510            |
| + TD / non-TD split                                | 4.82 | 0.665    | 0.491            |
| + Vegas (implied total, spread, total)             | 4.83 | 0.660    | 0.489            |
| everything above                                   | 4.63 | 0.699    | 0.520            |

Recency and *recent* usage are the only parts that matter; long-window usage
is redundant with long-window points. Vegas at the player level adds nothing —
that is the third time it has been measured (v1 multiplier, ablation, here),
so stop treating it as a projection lever; it stays useful for correlations,
DST and game environment only. This is the "v4 features" work and it is
real, but it is a 4% improvement, not the missing piece.

**2. What *role knowledge* is worth — 2.5× that.** A deliberate cheat:
add this week's actual snap% as a feature, as an upper bound on what depth
charts, injury reports and beat-writer role news could supply.

| model                                  | MAE  | Spearman | top-12 ρ |
|----------------------------------------|------|----------|----------|
| trailing-17 only                       | 4.83 | 0.663    | 0.488    |
| all history features                   | 4.63 | 0.699    | 0.520    |
| trailing-17 **+ this-week snap%**      | 4.32 | 0.766    | 0.548    |
| all history **+ this-week snap%**      | 4.20 | 0.779    | 0.565    |

Knowing the role alone is worth −0.51 MAE and +0.10 Spearman — more than
every history feature combined, and it stacks with them. Among the twelve
highest-projected players per game, **18% played under 80% of their previous
week's snaps**, and the ones under 50% were projected **+7.0 points too high**
on average. That bucket is Price (0 → started), Fields (starter history →
third string), Dobbins (feature back → 8 carries, 0 targets in a blowout).
The pipeline currently has *no data source at all* for it: no depth charts,
no injury reports, no snap trend, and no current-season stats.

**3. The live pool is 40% invisible to the model.** Re-running
`project_live_pool` on the three archived 2026 exports:

| slate   | pool | unresolved | team_changed | avg_points fallback | v3 | top-14 by salary on v3 |
|---------|------|------------|--------------|---------------------|----|------------------------|
| NE@SEA  | 46   | 13         | 9            | 14                  | 23 | 8 of 14                |
| SF@LAR  | 49   | 18         | 5            | 19                  | 25 | 8 of 14                |
| DEN@KC  | 51   | 17         | 7            | 18                  | 26 | 9 of 14                |

Only 8–9 of the fourteen players who decide a Showdown slate got a model
number; the rest were DK's own `AvgPointsPerGame` (which carries every
weakness above plus a week-1 zero for anyone new) or a refusal. Two causes,
both mechanical: **2026 is not ingested** (`config.yaml` seasons end at 2025,
so "trailing" means "last year, possibly on another team"), and the crosswalk
cannot resolve a forward-looking slate (T14). Neither is a modelling problem.

**4. The noise floor, so expectations are honest.** For projected-top-12
players whose role did *not* change (80–120% of last week's snaps, n=3,915),
MAE is still 6.2 and the sd of actual is 9.0. Within the 15–20 projection
band the sd of actual is 9.3; in 20–30 it is 10.3. That is the irreducible
part: a single Showdown lineup is six draws from distributions that wide, and
three live results are three draws. The game's actual top scorer is the
projected #1 only **23%** of the time, in the projected top-3 54%, top-5
72% — and the snap oracle barely moves that (26 / 58 / 78%). The captain
slot cannot be won on mean projection; it is a ceiling-and-ownership
decision (T19), and the sim already has the variance to make it.

**5. The backtest cannot see the failure mode that lost live.**
`player_week_stats` holds ~11 offensive players per team-game — only those
who recorded a stat. A DK Showdown pool has ~25 per team. So the harness
never asks "does this player play at all?", which is exactly the question
Price, Fields and Franklin turned on. Backtest MAE ≈ 4.8–5.0 therefore
understates live error by construction; a harness that scores the full
roster (nflverse weekly rosters as the pool, 0 points for anyone absent
from the stat line) is needed before an availability model can be
validated without leaking.

**What this changes.** The ranked list, by measured size: (1) ingest the
current season and fix pool resolution — no model can beat data it isn't
given; (2) an availability/role layer from free nflverse depth charts,
injury reports and snap counts, gated so a listed backup projects near
zero and a listed starter with no history gets a role-based prior instead
of 0.0; (3) score the backtest on the full roster so (2) can be validated;
(4) captain by ceiling (T19); (5) recent-usage and recency features (v4)
for the last 4%. Player props (H8) are the market's *already-role-aware*
estimate and would cover (2) for the top of the board in one step — the
measurement above raises H8's priority rather than lowering it.

**Source check for (2), from this sandbox.** `nfl_data_py.import_depth_charts`
loads (2025: 554k rows) and is keyed by a `dt` timestamp, not a week — each
row is a dated snapshot with `gsis_id`, `pos_abb`, `pos_rank`, which is the
right shape for a no-look-ahead join (take the last snapshot before kickoff).
`import_injuries` loads (2025: 6,068 rows) with `season, week, team, gsis_id,
report_status, practice_status`. `import_weekly_rosters` returned **HTTP 403**
here — check it against the stale-URL workaround at the top of this doc before
assuming it is unavailable. Snap counts are already in `player_week_stats`.

**A rounding bug of my own, caught while logging this.** The live report
showed this lineup at "0.00%" duplicated — the actual figure was 0.0600%
(3 of 5,000 in the simulated field), which rounds to "0.00%" at the table's
two decimal places. Scaled to the real ~3,000-entry field, that is an
estimated 1.8 duplicates, not the clean zero I represented it as. Small in
this case, but the same display-precision issue that caused the earlier
win%/solo% confusion — a pattern worth fixing at the source (more decimal
places, or show raw counts) rather than catching by hand each time.

## The raw cache was a staleness bug for anything still changing (T21)

`src/nflverse.py` archived each download under a bare filename and reused it
forever unless `--refresh`. Correct for a closed season, whose parquet never
changes. Wrong for two things that were being treated the same way:

- **A season in progress.** `stats_player_week_2026.parquet` is republished
  every week. Once week 1 was archived, every later ingest would have read
  the week-1 file and every trailing average would have quietly stopped at
  week 1 for the rest of the year. `--refresh` would have fixed the data by
  overwriting the archive — the opposite of "archive raw, always."
- **`games.csv`.** One file for every season, and it changes all year: scores
  as games finish, lines as they are posted, next season's schedule in
  spring. It had been cached once on 2026-09-11.

Fix: live data is fetched at most once per day into a *dated* snapshot
(`stats_player_week_2026_2026-09-17.parquet`, `games_2026-09-17.csv`), earlier
snapshots are kept, and a failed download **raises** naming the newest
snapshot rather than silently using it. A season counts as live until March 1
of the following year — nflverse last touched the 2025 snap counts on
2026-02-09, so "the regular season is over" is not the same as "the file is
final." The bare-named 2019–2025 archives stay as they are.

**Snap counts were never archived, and were all-or-nothing.** `_attach_snaps`
called `nfl_data_py.import_snap_counts(seasons)` for every season in one go:
no raw copy on disk, and one unpublished year (which a current season is,
early on) would have failed the whole call and — because the ingest is
`INSERT OR REPLACE` — **NULLed every season's snaps on that re-run.** Now
fetched per season through the same archived fetcher; a miss leaves only
that season's snaps NULL, with a warning. The release URL is the one the
library uses (`snap_counts/snap_counts_{season}.parquet`); 2026's file was
published 2026-09-15 for week 1.

**Publishing lag, observed.** Week 1 ended Monday 2026-09-14. By Thursday
the 17th, player and team stats were current (file dated 14:19 UTC that
day, so nflverse re-publishes continuously, not once), snap counts were
dated Tuesday the 15th. `ingest_status()` compares the last ingested week
to the last *completed* week (last kickoff + 4h, from `games.kickoff_utc`)
and the live script prints it before doing anything, so the Thursday-slate
case — Sunday's stats not yet in — is said out loud instead of being a
silently shorter window.

**A fragility in `_transform_dst`, exposed by the ingest test fixture.**
`frame.get("fumble_recovery_opp")` returns a scalar `None` when the column
is absent, and `pd.to_numeric(None).fillna(0)` then fails on a float. The
real team-week file has every column so it never fired, but a column
dropped upstream would have produced an `AttributeError` two calls away
from the cause. Optional DST columns now zero-fill through one helper.

**Result: 2026 week 1 in, and the live pool is mostly visible now.**
44,072 → 44,461 player-weeks (+389), 1,960 → 2,232 games (the full 2026
schedule, so week-2 lines are already queryable), 1,381 → 1,425 players.
Snap join 99.1%. Second run: identical counts, zero duplicate keys, no
downloads (today's snapshots reused). Re-measuring the three archived
exports with history strictly before 2026 week 2 — what the model sees
for *this* week's slates:

| slate  | unresolved (was) | team_changed (was) | v3 in top-14 (was) | still not v3 in the top-14                          |
|--------|------------------|--------------------|--------------------|-----------------------------------------------------|
| DEN@KC | 13 (17)          | 4 (7)              | **11** (9)         | Fields, Ehlinger (bench QBs), Nussmeier (rookie QB) |
| NE@SEA | 9 (13)           | 5 (9)              | **10** (8)         | DeVito, Morton (bench QBs), Price (<3 games), Myers (K) |
| SF@LAR | 16 (18)          | 2 (5)              | **10** (8)         | Bennett, Simpson, Rourke (bench QBs), Stribling (<3 games) |

Everyone left is a backup quarterback, a kicker, or a player with fewer
than three games in his new role — precisely the availability/role class
T22 is for, and nothing a longer history window would fix. Fields has no
2026 row (did not play) so he is still flagged `team_changed`, which is
the right answer. Kenneth Walker III resolves cleanly now: his 2026 week-1
row is at KC (37.1 points on 23 carries, 6 targets), so the team-change
flag clears and v3 sees him — the first live slate had to override him by
hand.

**Operational rule from here:** `python -m src.ingest.nfl_stats` before every
slate, or let `scripts/live_showdown.py` do it (it now ingests the slate's
season first and prints the currency check). `config.yaml` `seasons.end`
must be the season in progress; bump it each September.

## The backtest scored on the full roster (T23): the verdict on v3 reverses

Until now the harness evaluated players *with a stat line* in week W. That
conditions every number on having played — the one thing a pre-lock
projection cannot know, and the thing all three live losses turned on
(Price 0 → started; Fields starter's history → third string; Dobbins
feature back → 8 carries). The played pool cannot even represent "dressed,
did not play."

**Pool source: nflverse weekly rosters.** `nfl_data_py.import_weekly_rosters`
returns HTTP 403 from here; the release asset
(`weekly_rosters/roster_weekly_{season}.parquet`) downloads fine and is now
archived per season like everything else. `status` is the game-day roster
state and **`ACT` is the dressed 46**: 99.7–100% of stat-recording
player-weeks in every season 2019–2026 carry it (the rest have no roster
row at all — id gaps, not a different status), and no `INA` player ever
records a stat. So the roster pool is "every ACT QB/RB/WR/TE on a team with
a game, plus both DSTs" — ~14 skill players per team, the pool a Showdown
entrant faces at lock, inactives already announced. The upcoming week's
roster is published before its games (the 2026 file carried week 2 on the
Thursday of week 2), so it is pre-lock information. A dressed player with no
stat line scored 0, and the model is scored on that.

**The class the old pool could not see is a fifth of the pool, every year.**
2020–2025, 44,908 dressed skill player-weeks; **10,025 (22.3%) recorded no
stat line**, and 7,062 of those had ≥3 games of history — the model
projected them their trailing average and got 0. By position: QB 39.7%
of dressed never record a stat (backup quarterbacks), TE 28.9%, RB 20.0%,
WR 13.1%.

**Backtest, 2020–2025, same 107 weeks, both pools** (`--pool played` is the
pre-T23 behaviour and reproduces the earlier runs exactly):

| model                  | pool   | n      | MAE  | Spearman | bias  | top-12/game MAE | top-12 ρ | top-12 bias | zeros | zeros' mean proj |
|------------------------|--------|--------|------|----------|-------|-----------------|----------|-------------|-------|------------------|
| prior_average          | played | 34,936 | 5.00 | 0.606    | +0.17 | 6.23            | 0.480    | +0.64       | 11.5% | 3.66             |
| calibrated_by_position | played | 34,936 | 5.01 | 0.604    | +0.07 | 6.14            | 0.480    | −0.13       | 11.5% | 4.42             |
| prior_average          | roster | 42,991 | 4.90 | 0.604    | +0.97 | 6.81            | 0.438    | +1.59       | 28.1% | 4.21             |
| calibrated_by_position | roster | 42,991 | 5.13 | 0.576    | +1.11 | 6.85            | 0.405    | +1.04       | 28.1% | 5.22             |

("top-12/game" = the twelve highest-projected players per game — what a
Showdown lineup is chosen from. "zeros" = evaluated players who scored 0.)

Three things the honest pool says that the old one could not:

1. **The projection is biased high, and it is the quarterbacks.** Overall
   bias +0.17 → +0.97; QB bias **+3.42** (MAE 7.60, from 6.85). 36.9% of
   dressed QBs score 0 and the baseline hands them **8.9 points** on
   average. In the projected top-12 per game, **28.6% of the QB slots are a
   zero-scorer projected 12.0** — a backup with a starter's history ranked
   as a top play. Across all positions **11.0% of projected top-12 slots
   (2,134 of 19,380) go to a zero-scorer, handed 10.9 points; 79.6% of games
   have at least one.** That is Justin Fields at 14.6, reproduced sixteen
   hundred times over six seasons. Nothing in the trailing average can see
   it; T22 (depth chart + injury report) is built for exactly this.

2. **v3 is worse than the baseline on this pool** — top-12 ρ 0.405 vs 0.438,
   MAE 5.13 vs 4.90, Spearman 0.576 vs 0.604, and it hands zero-scorers
   5.2 vs 4.2. The mechanism is its own fix: the per-position intercept
   *lifts* low projections, which was right on the played pool (the bottom
   decile ran ~1 point low there) and is exactly wrong when a quarter of
   the pool scores 0. v3 still has the better top-12 *bias* (+1.04 vs
   +1.59), because it also pulls the top down. So T18's "a wash on MAE, a
   bias fix" was a verdict from a flattering pool; on the pool live slates
   are played on, it is a small regression in rank quality. **v3 is the
   live model today** (`project_live_pool`). Which to run until T22 lands is
   an owner call — H9 in TASKS.md — and `--pool roster` is the ship
   criterion from here on.

3. **A play/no-play oracle alone is worth most of the Showdown gap.**
   Dropping only the zero-scorers from the roster pool (a cheat, to bound
   what "will he play?" knowledge is worth): top-12 MAE 6.81 → **6.11**,
   top-12 bias +1.59 → **+0.33**, top-12 ρ 0.438 → 0.470. Overall Spearman
   *falls* (0.604 → 0.553) because zeros are easy to rank below everyone;
   the top-12 numbers are the ones that matter. Combined with the snap
   oracle measured earlier, availability and role are the whole story.

**What the old pool got right.** MAE is *lower* on the roster pool (4.90 vs
5.00) — the zeros are cheap misses for low-projected depth players — so a
single MAE headline would have hidden all of the above. The coverage block
and the top-12 slice now print on every run so that cannot happen again.
The excluded-for-no-history share is 3.1% of actual points on both pools:
rookies with no games are a small leak, not the problem.

**Caveats, stated (T23).** (a) The roster pool treats gameday inactives as known,
which is true for a Showdown lock (kickoff) and not for a main-slate early
game — right for the current scope, to be revisited if Classic ever comes
back in. (b) `min_games` still gates who is scored, so a T22 model that
supplies a prior for no-history players will need `--min-games 0` to be
scored on them; the coverage block shows what is being left out either
way. (c) 2026 week 2 rosters are in the table but not evaluated (no stats
yet); the harness keys on stat weeks.

## The role layer (T22): the first model that beats the baseline on the honest pool

T23 said availability and role were the whole gap. T22 builds them from two
free nflverse feeds, both archived per season like everything else:

**Depth charts come in two formats, and the numbers mean different things.**
Through 2024: one row per (week, player, slot) with `depth_team` as the
rank, and starters *tie* — a team lists two WRs at depth 1, two at 2.
From 2025: **daily** dated snapshots (`dt`, ~07:15 UTC, 219 days for KC in
2025) with `pos_rank`, an **ordinal within the position group across
slots** (KC 2025-11-01: Rice 1, Worthy 2, Brown 3, JuJu 4 …), so a player
in several slots takes his minimum. A week uses the last snapshot with
`dt` strictly before that team's kickoff — leakage-safe by construction —
and never one older than eight days, otherwise the live season's newest
snapshot gets pinned to every remaining week on the schedule (it did,
until that rule went in). The stored `depth_rank` is the feed's own number;
the comparable quantity is computed downstream.

**Injury reports** are weekly (`report_status` Out / Doubtful / Questionable;
most severe listing wins if a player appears twice). Only the 2019–2024
files carry `date_modified`; 2025+ do not, so the report cannot be
timestamp-checked against kickoff. It is the week's final report by the
feed's construction, and that is stated rather than verified.

**Effective rank, and zero rows.** The number the model uses is
`eff_rank`: the player's ordinal among *dressed* teammates at the position,
ordered by depth rank, then prior snap share (strictly-before, zeros
counted), then id. That resolves the old format's ties with what was
knowable, makes both formats comparable, and — because it is computed over
the dressed — puts a rookie RB at rank 1 when the two ahead of him are
inactive (Price). Roster-mode history also carries a **zero row for every
week a player dressed without a stat line**, so a backup's trailing average
is what he produced when dressed. Played-mode history is byte-for-byte
unchanged. Coverage: 91–96% of dressed skill players have a depth rank in
2019–2024, 99% from 2025 (the daily feed is more complete).

**The model (v4, `RoleAware`).** A prior per role bucket (QB1/QB2,
RB1–3, WR1–4, TE1/2, DST1) estimated fresh each week from time-boxed
history, zeros included; the player's own trailing mean over history rows
*in the bucket he holds this week*; shrink one toward the other by
n / (n + k), k = 3; then the week's injury listing (Out → 0, Doubtful ×
0.25, Questionable × 1.0 — the last two are untuned defaults). A demoted
starter has no same-role rows and projects as a backup; a rookie thrust
into RB1 gets the RB1 prior instead of DK's 0.0; a career backup stays a
backup because his same-role rows say so.

**Backtest, roster pool, 2020–2025, 107 weeks** (baseline row as recorded
under T23; v4 with the same `min_games 3`, and with `0` — everyone):

| model              | n      | MAE  | Spearman | bias  | top-12 MAE | top-12 ρ  | top-12 bias | QB MAE | QB bias | zeros → promised |
|--------------------|--------|------|----------|-------|------------|-----------|-------------|--------|---------|------------------|
| prior_average      | 42,991 | 4.90 | 0.604    | +0.97 | 6.81       | 0.438     | +1.59       | 7.60   | +3.42   | 28.1% → 4.21     |
| role_aware, mg 3   | 45,728 | 4.22 | 0.709    | +0.29 | 6.26       | **0.485** | +0.63       | 4.45   | +0.29   | 31.4% → 2.42     |
| role_aware, mg 0   | 48,138 | 4.16 | 0.707    | +0.31 | 6.28       | 0.480     | +0.63       | 4.39   | +0.30   | 32.6% → 2.42     |

Every acceptance number is cleared, and every season individually is
better (Spearman 0.70–0.73 vs 0.60–0.63). The QB line is the DEN@KC lesson
closed: bias +3.42 → +0.29, MAE 7.60 → 4.45. Against the play/no-play
oracle measured under T23 (top-12 MAE 6.11, bias +0.33, ρ 0.470), v4
reaches most of the MAE, half the bias, and *exceeds* the oracle on rank —
the role prior also improves ordering among players who do play. With
`min_games 0` it projects the entire pool with nothing excluded and still
beats the baseline that excluded 5,147 players. What v4 does **not** fix:
DST (ρ 0.13 either way — a trailing mean is not a DST model), and the
noise floor (top-12 MAE 6.3 on players who score with sd ≈ 9).

**Shipped live on the projection layer's criterion, stated.** CLAUDE.md
says a model ships on contest-sim ROI. There are no historical DK salaries
to simulate contests over, so — per the owner-approved plan, "validate each
layer against its own ground truth" — the projection layer ships on the
roster-pool backtest and ROI accrues in the ledger. `project_live_pool` now
defaults to v4; H9 (v3 or baseline until T22) is answered by this
measurement: neither. v3's refusal on a team change (T20) is no longer
needed — the depth chart supplies the role — so a team-changer is projected
and flagged rather than refused.

**Live smoke, the slate that lost.** The DEN@KC export re-run with history
before 2026 week 2 and the 2026-09-17 depth chart: Justin Fields (depth 2,
team change) **4.3** — v3 gave him 14.6; Stidham 0.2, Ehlinger 0.2;
Walker 13.5 and Waddle 11.6 with no hand override; Mahomes 21.5, Nix 18.9,
Rice 17.1, Dobbins 12.7 (RB1), Harvey 5.6 (RB2). 38 of 51 pool players are
model-backed; the 13 on `AvgPointsPerGame` are kickers and names the
crosswalk cannot reach (Nussmeier, T14). The optimizer's top build no
longer contains a quarterback who will not play. The CLI now prints the
role behind each top-16 projection so a wrong depth chart is visible before
it is trusted. Committed overrides now apply *after* the model and are
labelled `override` — before this they ran first and the model overwrote
them for anyone it could project, so an owner's read was silently lost for
exactly the players it was written for.

**Caveats, stated (T22).** (a) Injury multipliers are defaults, not fitted.
(b) The bucket caps (QB 2, RB 3, WR 4, TE 2) were chosen, not searched.
(c) `eff_rank` for the live slate is computed among the DK pool minus
OUT/IR — the dressed roster is not known until 90 minutes before kickoff,
so a surprise inactive shifts ranks after the projection is made; the
committed-override mechanism remains the late fix. (d) The depth chart is
the team's published one; teams are sometimes slow to update it, which the
dressed-only ranking mitigates but cannot remove.

## Captain by floor and ceiling (T19): built, and the floor is honest about itself

Twice live the optimizer captained the highest mean per dollar and lost on
it (Stafford SF@LAR, Dobbins DEN@KC). The mechanism was upstream of the
contest sim: candidates came from a mean-maximising optimizer, so every
candidate captained the same player and the sim never got to *compare
captains*. `src/captain.py` fixes the generation step with what already
existed — the score model's per-position variance (T13) and the correlated
simulator — and adds no new parameters:

1. `player_quantiles`: each player's floor / median / ceiling, closed-form
   from the lognormal the simulator draws from.
2. `captain_candidates`: one lineup per plausible captain (top by the
   objective's quantile, union top by mean so the old answer is always on
   the board), each with the **exact** best five-FLEX complement under the
   cap and both-teams rule (brute force over the top 30 by projection plus
   the cheapest five; verified against exhaustive search in tests).
3. `rank_lineups`: lineup-level mean and quantiles from one correlated draw,
   captain at 1.5×, every lineup on the same draws. `--objective cash`
   shortlists and orders on the lineup's 25th percentile and cash rate,
   `gpp` on the 90th and top-1% rate. Cash is the default (R5).

The lineup-level number is the point: with Mahomes as captain and his own
pass-catchers in the FLEX, the lineup is *wider* than the same FLEX behind
Dobbins, whatever the two marginals say alone (+0.33 QB–top target,
+0.21 QB–WR). A test pins that. The board on the DEN@KC slate, v4
projections, cash objective:

| captain   | pos | proj | sd  | own p10 | own p90 | lineup mean | p10  | p25  | p90   |
|-----------|-----|------|-----|---------|---------|-------------|------|------|-------|
| Mahomes   | QB  | 21.5 | 8.7 | 12.1    | 32.8    | 99.0        | 69.4 | 80.6 | 133.2 |
| Dobbins   | RB  | 12.7 | 7.9 | **5.2** | 22.4    | 95.9        | 67.5 | 79.1 | 128.7 |
| Nix       | QB  | 18.9 | 8.6 | 9.9     | 30.0    | 95.6        | 68.6 | 78.8 | 125.6 |
| Rice      | WR  | 17.1 | 9.8 | 7.5     | 29.4    | 96.3        | 66.9 | 78.0 | 128.1 |

Dobbins' own tenth percentile is 5.2 — the 3.6 he returned was inside the
model's range, not a surprise — and the board says so before lock instead
of after.

**Are the floors and ceilings real? Measured, roster pool 2020–2025, v4
means, the projected top-12 per game (n = 19,380):** share of actuals at or
below each model quantile, ideal = the quantile.

| position | ≤p10  | ≤p25  | ≤p50  | ≤p75  | ≤p90  | ≤p95  |
|----------|-------|-------|-------|-------|-------|-------|
| QB       | 0.184 | 0.283 | 0.469 | 0.716 | 0.903 | 0.968 |
| RB       | 0.213 | 0.330 | 0.516 | 0.726 | 0.883 | 0.952 |
| WR       | 0.236 | 0.347 | 0.530 | 0.728 | 0.879 | 0.947 |
| TE       | 0.208 | 0.313 | 0.474 | 0.711 | 0.883 | 0.948 |
| DST      | 0.203 | 0.320 | 0.499 | 0.715 | 0.893 | 0.953 |
| **ALL**  | **0.216** | **0.326** | 0.507 | 0.722 | 0.886 | 0.952 |

The median and the ceiling are calibrated (p50 → 0.507, p90 → 0.886, p95 →
0.952). **The floor is optimistic**: a fifth of top-12 actuals land below the
model's tenth percentile and a third below its 25th. A lognormal has no mass
at zero and its left tail is thin; the real one has both (3–7.5% of top-12
players score exactly 0; blowouts and in-game injuries do the rest). WR
floors are the most overstated, QB floors the least — so a true-floor
correction would push the cash choice *further* toward the steady captain,
not away from it. The ordering the board gives is directionally right; the
floor *numbers* should be read as "about ten percentile points too kind."
Fixing that is a score-model change (zero mass and a fatter left tail per
position) and is queued as T24 rather than folded in here.

**Ceiling as a captain predictor, measured on the same slice:** the game's
actual top scorer is the projected #1 by mean 23.9% of the time, by model
p90 **26.3%**, by p95 25.7%, by p10 22.6% (top-3: 53.5 / 53.9 / 54.1 /
52.7%). Ordering the captain slot on ceiling is a real if modest gain for
GPP, and ordering it on floor is worse for that question — which is the
whole reason the objective is a switch and not a constant.

**Also fixed on the way:** the live script's synthetic payout table paid 600
places regardless of field size, so any test field under 600 entries cashed
every lineup and the cash ordering was degenerate; the tiers now scale with
the field. The shortlist handed to the contest sim is now the top candidates
by the objective's lineup quantile, not by mean, so a steadier or
higher-ceiling captain is not cut before the sim sees it.

## The score model's floor, fixed (T24): an empirical marginal behind the same copula

T19 measured the lognormal's lower tail as optimistic on the honest pool.
The cause was structural — a lognormal has no mass at zero and a thin left
tail — so the fix is a different marginal, not a re-fit: `EmpiricalMarginals`
in `src/scoremodel.py` is the quantile curve of **actual / projection**, per
position and projection band (five quantile bands; a player's curve is
interpolated between the two nearest band centres), fitted on the v4
roster-pool replay with zeros included. It sits behind the unchanged
Gaussian copula, so the measured correlation blocks still apply, and the
lognormal path is byte-for-byte intact when no marginal is passed.

**Two choices, stated.** (1) The curves are *not* mean-normalised: they are
the calibrated distribution of what happened given the projection, so the
simulator's mean is projection × the band's mean ratio — a few percent under
the projection at the top of the board (v4 runs ~3–5% high there), over it
at the bottom; T18's regression to the mean is now in the simulator as well
as the projection. `normalise_mean=True` exists for a caller who wants the
projection kept as the mean at the cost of calibration. (2) The shape is
tied to the projection model that produced the residuals; when v4 changes,
`scripts/fit_score_marginals.py` refits it and prints this table again.

**Held out: fit on 2020–2023, checked on 2024–2025.** Share of actuals at or
below each model quantile on the projected top-12 per game (n = 6,528;
ideal = the quantile):

| marginal  | ≤p10      | ≤p25      | ≤p50  | ≤p75  | ≤p90  | ≤p95  |
|-----------|-----------|-----------|-------|-------|-------|-------|
| lognormal | 0.210     | 0.318     | 0.502 | 0.721 | 0.886 | 0.951 |
| empirical | **0.094** | **0.232** | 0.487 | 0.745 | 0.906 | 0.955 |

Every position lands within ~0.05 of ideal at every quantile (QB ≤p10
0.102, WR 0.113, DST 0.096; RB 0.066 and TE 0.075 now read a touch
*conservative* at the floor). The shipped file, refit on all six seasons,
reads 0.105 / 0.240 / 0.492 / 0.748 / 0.904 / 0.955 in-sample on the same
slice. On the full dressed pool the ≤p10 share still reads 0.33 — not a
miss: below about five projected points the zero atom is 45–75% of the
distribution, so "P(actual ≤ the 10th percentile)" is the atom itself.
Coverage is only a meaningful check at quantiles outside the atom, which
the top-12 slice is.

The fitted zero shares are the availability story in one line: QB bands at
projection 0.8 / 2.0 / 14.2 / 17.4 / 21.1 have zero shares 0.76 / 0.73 /
0.11 / 0.02 / 0.01; RB 0.75 → 0.015; TE 0.75 → 0.05; WR 0.68 → 0.025.

**What changed on the board.** DEN@KC, same slate as under T19, cash
objective: Mahomes' own tenth percentile 12.1 → **9.5**, Dobbins 5.2 →
**2.7**, Kelce 4.1 → 2.3; lineup means ~4% lower (the bias, now carried);
Mahomes still leads on lineup p25 (74.1), Nix second, Dobbins third. The
live script prints which marginal it is running and, if the JSON is
missing, that floors read ten points too kind.

**T13's stack check, re-run under the new marginal.** The copula is
unchanged but the marginal is not, so the summed QB + top-target spread —
the number a stacked lineup's placement depends on — was re-measured on
the v4 pairs (3,219 team-weeks, QB projected ≥ 8, top target ≥ 5):

| | realised | lognormal | empirical |
|---|---|---|---|
| sd of the QB + top-target summed score | **15.63** | 15.13 | 16.26 |
| … if the two were independent | — | 13.23 | 13.84 |
| mean of the sum | 31.25 | 32.55 | 31.03 |

Both marginals reproduce the stack (+2.4 points of spread over
independence); the empirical one runs 4% over on spread where the lognormal
ran 3% under, and its mean is the one that matches (the lognormal's is the
projection sum, 1.3 points high). Same seasons for fit and check, as
before; a held-out version waits on the 2026 archive.

## The crosswalk resolves a slate before it is played (T14)

`crosswalk.build_reference` used to be "everyone with a stat row that
season/week": right for a backtest, impossible for a live export, whose week
has no stat rows until the games are over. `src/resolve.py` covered the live
path with a name + most-recent-team lookup against `players`, which is why
13–18 players per pool went unresolved (rookies, anyone with no stat line
yet, kickers) and why a team-changer had to be *refused* (T20).

**The reference is now three sources, unioned.** The week's **rosters** —
every status, because DK prices the whole 53 plus elevations, and the weekly
roster is published before the games (the 2026 file carried week 2 on the
Thursday of week 2); the week's stat rows, as before; and a DST for every
team on the schedule that week. Name-to-id mapping only, so nothing here can
leak an outcome — the "validated against the roster as it actually was"
guarantee is, if anything, stronger: the roster *is* the roster. A week with
neither rosters nor stats yet falls back to the most recent earlier roster
week and says so (`ref.attrs["reference_week"]`, logged by the salary
loader), rather than raising.

**Kickers stay in `rosters` now.** The stats feed has nothing on them, so the
roster ingest dropped them and the crosswalk could never resolve one; they
are kept (`ROSTER_POSITIONS`) and the backtest pool filters to skill
positions instead. `salaries.player_id` is therefore populated for kickers
too, which T3's ownership table will want.

**The live path uses the real waterfall.** `resolve_pool(conn, pool, season,
week)` runs the pool through `crosswalk.resolve` against that week's
reference — manual overrides, DST map, the persisted DK-id map, exact
name+team+position, name+position, fuzzy within team and position — and
persists what it matched, so a DK id resolved once resolves by id from then
on whatever DK does to the spelling. Without a week it is the old stop-gap,
unchanged. `team_changed` is still reported (most recent stat team ≠ export
team); v4 projects from the new team's depth chart, so it is information,
not a refusal.

**Measured on the three archived 2026 exports.**

| slate  | pool | stop-gap | crosswalk | still unresolved                                |
|--------|------|----------|-----------|-------------------------------------------------|
| NE@SEA | 46   | 37       | **46**    | —                                               |
| SF@LAR | 49   | 33       | **49**    | —                                               |
| DEN@KC | 51   | 38       | **49**    | two long snappers DK lists as TE (position LS)  |

`python -m src.ingest.dk_salaries load` now stores all three slates — the
first forward-looking loads ever — at 98.5% / 98.1% / 96.4% join; DEN@KC
sits under the 97% threshold by exactly those two long snappers, and the
warning names them in `data/unmatched_review.csv`, which is the threshold
doing its job. On a second load 49 / 62 / 49 rows resolved by the persisted
DK id. `id_crosswalk` holds 167 DK rows.

**What this unblocks.** Garrett Nussmeier (rookie QB, no stat line anywhere)
now resolves, gets a QB3 role prior from v4 instead of DK's 0.0, and stops
being an `avg_points` row on the board. The `salaries` table can carry a
live slate, which T3 (standings → ownership by player_id) and the December
ownership model both need.

## The chalk cluster (T15): the field is diverse and clustered at once, and now the sampler is too

Two real standings files said the same thing: a Showdown field is 61–69%
distinct lineups *and* has a most-entered build at ~1.1%, and a marginal
sampler cannot produce both (the jitter that matches diversity understates
the top build ~6×). Real entrants do not draw players independently; a
share of them run an optimizer on roughly the same projections and land on
the same few builds. `generate_field` now takes that literally: a
**cluster** — the optimizer's near-optimal builds at low jitter, weighted
by how often it lands on them (`field.chalk_builds`) — and a **share** of
the field drawn from it, with the rest sampled diffusely against the
*residual* ownership and the cluster lineups frozen through the repair
pass. `src/standings.py` reads a standings CSV as lineups so the
calibration has ground truth; T3 owns persistence.

**Fitted on NE@SEA (2,369 entries mapped of 2,373; the four dropped
rostered OUT players).** Generator fed the *real* ownership, to isolate the
joint-structure question:

| cluster jitter | share | distinct | top build | top-5 | own MAE |
|----------------|-------|----------|-----------|-------|---------|
| —              | 0     | 87.0%    | 0.25%     | 0.9%  | 0.7     |
| 0.15           | 0.2   | 67.0%    | 2.32%     | 8.9%  | 0.4     |
| 0.30           | 0.2   | 74.8%    | 0.76%     | 2.7%  | 0.7     |
| **0.30**       | **0.3** | **63.5%** | **0.97%** | **4.0%** | 0.5 |
| 0.30           | 0.4   | 54.4%    | 1.39%     | 5.2%  | 0.8     |
| *real*         |       | *61.5%*  | *1.10%*   | *3.4%* |        |

Two things worth stating. First, with **correct marginals and no cluster**
the sampler is at 87% distinct against a real 61.5%: the joint structure is
the whole story, not a residual. Second, the cluster's own concentration
matters as much as the share — at jitter 0.15 the top build is 12% of the
cluster and a 20% share already overshoots the real 1.1% twice over; at
0.30 it is 4% and the share does the work. Ownership error does not move
with the cluster (0.4–0.8 points either way), so the marginals are not
being paid for. Fed the *estimated* ownership instead (the live path), the
same setting reads 62.4% / 1.14% / 4.5% with the estimate's own 5.1-point
MAE, unchanged.

**Checked on SF@LAR, untouched (2,369 entries, real 68.7% / 1.06% / 4.2%):**

| setting                | ownership in | distinct | top build | top-5 |
|------------------------|--------------|----------|-----------|-------|
| no cluster             | real         | 88.7%    | 0.25%     | 1.1%  |
| jitter 0.30, share 0.3 | real         | **73.5%** | **0.63%** | 2.1% |
| jitter 0.30, share 0.3 | estimated    | 72.9%    | 0.72%     | 2.3%  |

The structure transfers — the cluster closes most of the distance on both
axes — but the top build lands at 0.63% against 1.06%. The reason is
visible in the table: on SF@LAR our optimizer's landscape at jitter 0.30
was flatter (the most common build was 1% of the cluster, not 4%), so the
same share produces less concentration, while the real field concentrated
just as hard as it had the week before. Real entrants' consensus is
sharper than our projections' optimizer says it should be on that slate. A
share of ~0.45 or a jitter of ~0.2 would have matched SF@LAR, and would
have overshot NE@SEA; one number cannot be fitted to both without fitting
to the check, so the NE@SEA fit ships (`CHALK_SHARE = 0.30`,
`CHALK_JITTER = 0.30`) and the SF@LAR gap is the stated error bar: the
sampler now carries about **60–90% of the real top-build concentration**
where it carried 25% before, and is within 2–5 points on distinct share
where it was 25 points off. **Every duplication figure and every
dup-adjusted ROI is anchored to a real field for the first time**, with
that error bar attached.

**Limits, stated.** (a) Two slates of ground truth; the third (DEN@KC) was
lost to H2. Re-check the share on each new standings file — the live
script prints the generated field's shape next to the real reference
numbers on every run so drift is visible. (b) The cluster is built from
*our* projections; the real field's cluster is built from the field's.
Where those differ (SF@LAR), so does the concentration. A cluster built
from a projection *consensus* — vendor or market — would fix that, and is
one more reason H4/H8 matter. (c) Field size is the real contest's; a
5,000-entry default field with a 2,369-entry calibration is an
extrapolation the shape metrics do not depend on strongly (shares, not
counts), but the most-entered *count* does.

