# Test fixtures

## `2023-w01_dk_main.csv`

A DraftKings-format salary export used to exercise the crosswalk end to end.

**Salaries, player IDs, and AvgPointsPerGame in this file are fabricated.** It is
not DraftKings data and must not be used as a source of real salaries.

What *is* real: the player names, positions, teams, and matchups, drawn from
nflverse 2023 week 1 rosters. That's the point — the fixture has to reproduce
the ways DK's spellings differ from the stats feed, or it would match trivially
and test nothing:

- suffix differences (`Michael Pittman` → `Michael Pittman Jr.`)
- punctuated initials (`DJ Moore` → `D.J. Moore`)
- defenses spelled as nicknames (`Falcons`) rather than abbreviations (`ATL`)
- three players with no stat row at all, so the join rate can't reach 100% and
  the manual-review queue gets exercised

Regenerate with `scripts/make_fixture.py` if the roster data changes.

## `2026-w01_dk_showdown-ne-sea.csv`

A DraftKings **Showdown** export, used to exercise CPT/FLEX collapsing and
status filtering.

**Salaries and player IDs are fabricated**, same as the main-slate fixture. Real
names, teams and matchup, from the 2026 week 1 NE@SEA slate.

What it has to reproduce:

- every player twice, one `CPT` row and one `FLEX` row
- CPT salary at exactly 1.5x the base, since the parser verifies that ratio
  rather than trusting it
- rows sorted by salary descending, as DK ships them — the ordering that used to
  be load-bearing and no longer is
- `Status` values `OUT`, `IR` and `Q`, so pool filtering keeps the questionable
  player and drops the other two
