"""Generate a realistic DK-format salary fixture from real 2023 W1 rosters.

Player names/teams are real (public fact); SALARIES ARE FABRICATED — this is a
join-logic fixture, not a DK data redistribution.

DK's spellings differ from nflverse's in specific, repeatable ways. We reproduce
those so the fixture actually exercises the crosswalk instead of matching
trivially.
"""
import random
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import db, teams as teams_mod  # noqa: E402

random.seed(7)
conn = db.connect()

q = """
SELECT p.player_id, p.name, p.position, s.team, s.game_id, s.dk_points
FROM player_week_stats s JOIN players p USING(player_id)
WHERE s.season=2023 AND s.week=1
"""
df = pd.read_sql_query(q, conn)
games = pd.read_sql_query(
    "SELECT game_id, home_team, away_team FROM games WHERE season=2023 AND week=1", conn
)
# A "main slate" is the Sunday afternoon games; approximate by taking most games.
keep_games = set(games["game_id"].tolist()[:13])
df = df[df["game_id"].isin(keep_games)]

# Rough DK behaviour: only players with a real role are priced.
off = df[df["position"] != "DST"].copy()
off = off[off["dk_points"].fillna(0) > 0.5]
dst = df[df["position"] == "DST"].copy()

opp = {}
for _, g in games.iterrows():
    opp[(g.game_id, g.home_team)] = g.away_team
    opp[(g.game_id, g.away_team)] = g.home_team

SUFFIXED = {  # players DK spells with a suffix that nflverse may not, and vice versa
    "Michael Pittman": "Michael Pittman Jr.",
    "Odell Beckham": "Odell Beckham Jr.",
    "Kenneth Walker": "Kenneth Walker III",
    "Brian Robinson": "Brian Robinson Jr.",
    "Marvin Mims": "Marvin Mims Jr.",
    "Jeff Wilson": "Jeff Wilson Jr.",
}
INITIALS = re.compile(r"^([A-Z])([A-Z]) ")  # "DJ Moore" -> "D.J. Moore"


def dk_spell(name: str) -> str:
    if name in SUFFIXED:
        return SUFFIXED[name]
    m = INITIALS.match(name)
    if m:
        return INITIALS.sub(rf"{m.group(1)}.{m.group(2)}. ", name)
    return name


rows = []
for _, r in off.iterrows():
    pos = r["position"]
    roster = f"{pos}/FLEX" if pos in ("RB", "WR", "TE") else pos
    salary = int(random.gauss(5200, 1600) // 100 * 100)
    salary = max(3000, min(11000, salary))
    o = opp.get((r["game_id"], r["team"]), "")
    rows.append({
        "Position": pos,
        "Name + ID": f"{dk_spell(r['name'])} (1{random.randint(1000000, 9999999)})",
        "Name": dk_spell(r["name"]),
        "ID": f"1{random.randint(1000000, 9999999)}",
        "Roster Position": roster,
        "Salary": salary,
        "Game Info": f"{r['team']}@{o} 09/10/2023 01:00PM ET",
        "TeamAbbrev": r["team"],
        "AvgPointsPerGame": round(random.uniform(2, 22), 2),
    })

for _, r in dst.iterrows():
    team = r["team"]
    o = opp.get((r["game_id"], team), "")
    rows.append({
        "Position": "DST",
        "Name + ID": f"{teams_mod.BY_ABBR[team].nickname} (1{random.randint(1000000, 9999999)})",
        "Name": teams_mod.BY_ABBR[team].nickname,   # DK spells defenses by nickname
        "ID": f"1{random.randint(1000000, 9999999)}",
        "Roster Position": "DST",
        "Salary": int(random.gauss(2900, 400) // 100 * 100),
        "Game Info": f"{team}@{o} 09/10/2023 01:00PM ET",
        "TeamAbbrev": team,
        "AvgPointsPerGame": round(random.uniform(2, 12), 2),
    })

# Real slates always carry a few players with no stat row at all (inactives,
# deep bench, practice-squad elevations). Without these the fixture would
# flatter the join rate.
for name, team, pos in [
    ("Jake Bobo", "SEA", "WR"),
    ("Deneric Prince", "KC", "RB"),
    ("Xavier Gipson", "NYJ", "WR"),
]:
    rows.append({
        "Position": pos, "Name + ID": f"{name} (19999999)", "Name": name,
        "ID": "19999999", "Roster Position": f"{pos}/FLEX", "Salary": 3000,
        "Game Info": f"{team}@BUF 09/10/2023 01:00PM ET", "TeamAbbrev": team,
        "AvgPointsPerGame": 0.0,
    })

out = pd.DataFrame(rows).sort_values(["Position", "Salary"], ascending=[True, False])
dest = Path(__file__).resolve().parent.parent / "tests/fixtures/2023-w01_dk_main.csv"
out.to_csv(dest, index=False)
print(f"wrote {len(out)} rows to {dest}")
print(out["Position"].value_counts().to_string())
