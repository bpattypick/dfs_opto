"""DraftKings salary ingestion (spec §5.4).

Two input paths:

* :func:`parse_dk_export` — DK's own "Export to CSV" from the draft screen. This
  is the durable, forward-looking path: download weekly, drop it in
  ``data/raw/salaries/`` using the ``2026-w01_dk_main.csv`` convention, done.
* :func:`parse_rotoguru` — the community historical archive, for backfilling
  past seasons.

  IMPORTANT: the RotoGuru adapter is written from its documented semicolon
  export format but is **unverified** — the sandbox this was built in blocks
  every host except GitHub, so the endpoint could not be contacted. Run
  ``python -m src.ingest.dk_salaries probe --season 2023 --week 1`` from a
  machine with open networking to confirm it still serves data and that the
  columns still line up before trusting a backfill. Parsing is header-driven,
  so a column reorder is survivable; a wholesale format change is not.

Salary rows are joined to canonical player IDs through the crosswalk, and the
join coverage is reported against the spec's 97% threshold.
"""

from __future__ import annotations

import argparse
import io
import logging
import re
from pathlib import Path

import pandas as pd
import requests

from src import config as config_mod
from src import db
from src import teams as teams_mod
from src.ingest import crosswalk

log = logging.getLogger(__name__)

SOURCE = "dk"

# 2026-w01_dk_main.csv -> season 2026, week 1, slate 'main'
# Hyphens are allowed in the slate so a Showdown can name its game
# (2026-w01_dk_showdown-ne-sea.csv). Without that, every Showdown in a week
# collapses to the same slate_id and the second one overwrites the first.
FILENAME_RE = re.compile(
    r"^(?P<season>\d{4})-w(?P<week>\d{1,2})_dk_(?P<slate>[a-z0-9][a-z0-9-]*)\.csv$",
    re.IGNORECASE,
)

ROTOGURU_URL = "http://rotoguru1.com/cgi-bin/fyday.pl"


class SalaryFormatError(ValueError):
    """The file did not look like a DK salary export."""


def parse_slate_filename(path: str | Path) -> dict:
    """Pull season/week/slate metadata out of our own filename convention."""
    name = Path(path).name
    match = FILENAME_RE.match(name)
    if not match:
        raise SalaryFormatError(
            f"{name!r} does not match the required convention "
            f"'<season>-w<week>_dk_<slate>.csv', e.g. '2026-w01_dk_main.csv'"
        )
    season = int(match.group("season"))
    week = int(match.group("week"))
    slate = match.group("slate").lower()
    return {
        "season": season,
        "week": week,
        "slate": slate,
        "slate_id": f"{season}-w{week:02d}-{slate}",
    }


def _opponent_from_game_info(game_info: str | None, team: str | None) -> str | None:
    """DK's Game Info looks like 'BUF@LAR 09/08/2024 08:20PM ET'."""
    if not game_info or not team or pd.isna(game_info):
        return None
    matchup = str(game_info).split()[0]
    if "@" not in matchup:
        return None
    away, home = (teams_mod.normalize_team(t) for t in matchup.split("@", 1))
    team = teams_mod.normalize_team(team)
    if team == home:
        return away
    if team == away:
        return home
    return None


def _find_header_row(text: str) -> int:
    """DK exports sometimes prepend a contest-info block before the real header."""
    for index, line in enumerate(text.splitlines()):
        lowered = line.lower()
        if "salary" in lowered and ("name" in lowered or "position" in lowered):
            return index
    raise SalaryFormatError("no header row containing 'Salary' and 'Name' was found")


def parse_dk_export(path: str | Path) -> pd.DataFrame:
    """Parse DK's native salary CSV into the ``salaries`` schema."""
    meta = parse_slate_filename(path)
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    header_row = _find_header_row(text)
    raw = pd.read_csv(io.StringIO(text), skiprows=header_row)
    raw.columns = [str(c).strip() for c in raw.columns]

    def column(*candidates: str) -> pd.Series:
        for candidate in candidates:
            for col in raw.columns:
                if col.lower() == candidate.lower():
                    return raw[col]
        return pd.Series([None] * len(raw), index=raw.index)

    salary = pd.to_numeric(
        column("Salary").astype(str).str.replace(r"[$,]", "", regex=True),
        errors="coerce",
    )

    out = pd.DataFrame(index=raw.index)
    out["slate_id"] = meta["slate_id"]
    out["season"] = meta["season"]
    out["week"] = meta["week"]
    out["dk_name"] = column("Name").astype(str).str.strip()
    out["dk_salary"] = salary
    out["roster_position"] = column("Roster Position", "Position").astype(str).str.strip()
    out["position"] = column("Position", "Roster Position").astype(str).str.strip()
    out["team"] = column("TeamAbbrev", "Team").map(teams_mod.normalize_team)
    game_info = column("Game Info", "GameInfo")
    out["opponent"] = [
        _opponent_from_game_info(g, t) for g, t in zip(game_info, out["team"])
    ]
    out["source_id"] = column("ID").astype(str).str.strip()

    out = out[out["dk_name"].notna() & (out["dk_name"] != "") & out["dk_salary"].notna()]
    if out.empty:
        raise SalaryFormatError(f"{Path(path).name}: parsed zero usable salary rows")
    out["dk_salary"] = out["dk_salary"].astype(int)
    return out.reset_index(drop=True)


def parse_rotoguru(text: str, season: int, week: int, slate: str = "main") -> pd.DataFrame:
    """Parse RotoGuru's semicolon export into the ``salaries`` schema.

    UNVERIFIED — see the module docstring. Header-driven so that a column
    reorder does not silently shift data into the wrong fields.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header_index = next(
        (i for i, ln in enumerate(lines) if ";" in ln and "salary" in ln.lower()), None
    )
    if header_index is None:
        raise SalaryFormatError(
            "no semicolon-delimited header containing 'salary' found; "
            "RotoGuru's format has probably changed"
        )
    frame = pd.read_csv(
        io.StringIO("\n".join(lines[header_index:])), sep=";", engine="python"
    )
    frame.columns = [str(c).strip().lower() for c in frame.columns]

    def column(*candidates: str) -> pd.Series:
        for candidate in candidates:
            if candidate in frame.columns:
                return frame[candidate]
        return pd.Series([None] * len(frame), index=frame.index)

    salary = pd.to_numeric(
        column("dk salary", "salary").astype(str).str.replace(r"[$,]", "", regex=True),
        errors="coerce",
    )

    out = pd.DataFrame(index=frame.index)
    out["slate_id"] = f"{season}-w{week:02d}-{slate}"
    out["season"] = season
    out["week"] = week
    # RotoGuru writes names "Last, First"; DK and our reference use "First Last".
    names = column("name").astype(str).str.strip()
    out["dk_name"] = [
        f"{n.split(',', 1)[1].strip()} {n.split(',', 1)[0].strip()}" if "," in n else n
        for n in names
    ]
    out["dk_salary"] = salary
    out["position"] = column("pos", "position").astype(str).str.strip().str.upper()
    out["roster_position"] = out["position"]
    out["team"] = column("team").map(teams_mod.normalize_team)
    out["opponent"] = column("oppt", "opp").map(teams_mod.normalize_team)
    out["source_id"] = ""

    out = out[out["dk_salary"].notna()]
    if out.empty:
        raise SalaryFormatError("RotoGuru response contained no priced rows")
    out["dk_salary"] = out["dk_salary"].astype(int)
    return out.reset_index(drop=True)


def fetch_rotoguru(season: int, week: int, timeout: int = 60) -> str:
    """Download one week of RotoGuru DK salaries. UNVERIFIED — see module docstring."""
    resp = requests.get(
        ROTOGURU_URL,
        params={"week": week, "year": season, "game": "dk", "scsv": 1},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.text


# --- crosswalk + persistence --------------------------------------------------


def resolve_and_store(conn, salaries: pd.DataFrame, cfg=None) -> crosswalk.MatchResult:
    """Join salary rows to canonical IDs, persist, and report join coverage."""
    cfg = cfg or config_mod.load()
    season = int(salaries["season"].iloc[0])
    week = int(salaries["week"].iloc[0])

    reference = crosswalk.build_reference(conn, season=season, week=week)
    if reference.empty:
        raise RuntimeError(
            f"no player_week_stats for {season} week {week}; "
            "run `python -m src.ingest.nfl_stats` first"
        )

    source_rows = salaries.rename(columns={"dk_name": "source_name"})
    result = crosswalk.resolve(
        source_rows,
        reference,
        source=SOURCE,
        manual=crosswalk.load_manual_overrides(cfg=cfg),
        fuzzy_threshold=int(cfg.get("crosswalk.fuzzy_threshold", 90)),
    )

    crosswalk.persist(conn, SOURCE, result.matched)

    merged = pd.concat([result.matched, result.unmatched], ignore_index=True)
    merged["dk_name"] = merged["source_name"]
    db.upsert_df(conn, "salaries", merged)

    if len(result.unmatched):
        path = crosswalk.write_unmatched(
            result.unmatched.assign(season=season, week=week), cfg=cfg
        )
        log.info("%d unmatched rows appended to %s", len(result.unmatched), path)

    threshold = float(cfg.get("crosswalk.min_join_coverage", 0.97))
    log.info("dk salaries %s w%s: %s", season, week, result.summary())
    if result.coverage < threshold:
        log.warning(
            "JOIN COVERAGE %.1f%% is below the %.0f%% threshold — resolve "
            "data/unmatched_review.csv into data/manual_overrides.csv before "
            "trusting this slate",
            100 * result.coverage, 100 * threshold,
        )
    return result


def load_file(conn, path: str | Path, cfg=None) -> crosswalk.MatchResult:
    salaries = parse_dk_export(path)
    log.info("parsed %d salary rows from %s", len(salaries), Path(path).name)
    return resolve_and_store(conn, salaries, cfg=cfg)


def load_archive(conn, cfg=None) -> dict[str, crosswalk.MatchResult]:
    """Load every DK salary file sitting in ``data/raw/salaries/``."""
    cfg = cfg or config_mod.load()
    results: dict[str, crosswalk.MatchResult] = {}
    for path in sorted(cfg.path("raw_salaries").glob("*.csv")):
        try:
            results[path.name] = load_file(conn, path, cfg=cfg)
        except SalaryFormatError as exc:
            log.error("skipping %s: %s", path.name, exc)
    if not results:
        log.warning(
            "no salary files found in %s — DK exports are the one input that "
            "cannot be fetched for free; see docs/data-sources.md",
            cfg.path("raw_salaries"),
        )
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Load DK salary files.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load", help="load one file, or the whole archive")
    p_load.add_argument("path", nargs="?", help="defaults to every file in data/raw/salaries/")

    p_probe = sub.add_parser("probe", help="test whether RotoGuru still serves DK salaries")
    p_probe.add_argument("--season", type=int, required=True)
    p_probe.add_argument("--week", type=int, required=True)
    p_probe.add_argument("--save", action="store_true", help="archive the response if it parses")

    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    cfg = config_mod.load()

    if args.command == "probe":
        try:
            text = fetch_rotoguru(args.season, args.week)
        except Exception as exc:  # noqa: BLE001
            print(f"UNREACHABLE: {type(exc).__name__}: {exc}")
            return 1
        try:
            frame = parse_rotoguru(text, args.season, args.week)
        except SalaryFormatError as exc:
            print(f"REACHABLE but UNPARSEABLE: {exc}")
            print("first 400 chars of response:\n" + text[:400])
            return 1
        print(f"OK: parsed {len(frame)} rows for {args.season} week {args.week}")
        print(frame.head(10).to_string(index=False))
        if args.save:
            dest = cfg.path("raw_salaries") / f"{args.season}-w{args.week:02d}_dk_main.csv"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
            print(f"archived raw response to {dest}")
        return 0

    with db.session() as conn:
        db.create_schema(conn)
        if args.path:
            load_file(conn, args.path, cfg=cfg)
        else:
            load_archive(conn, cfg=cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
