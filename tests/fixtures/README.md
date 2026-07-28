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
