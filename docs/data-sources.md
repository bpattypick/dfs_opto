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
