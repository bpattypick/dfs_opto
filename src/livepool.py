"""Build a live slate's pool: ingest -> resolve -> v4 project -> overrides.

Shared by every live driver script (scripts/live_showdown.py,
scripts/live_classic.py) so the chain — pool.from_export (T11/T12) ->
src.resolve (T14 wedge) -> src.liveproj (v4 role-aware, T22) -> committed
overrides — cannot drift between contest formats. It is per-player and
per-position, not Showdown- or Classic-shaped (docs/decisions.md H10), so it
needs no format-specific branching at all.
"""

from __future__ import annotations

import pandas as pd

from src import config as config_mod
from src import db
from src.ingest import nfl_stats
from src.ingest.dk_salaries import parse_dk_export, parse_slate_filename
from src.liveproj import project_live_pool
from src.pool import apply_overrides, from_export, load_overrides, questionable
from src.resolve import resolve_pool

SALARY_FLOOR = 1200      # T17: excludes the DK "will not play" pricing tier


def refresh_history(season: int) -> None:
    """T21: fetch the season's latest nflverse files before building anything.

    Dated, archived snapshots (src/nflverse.py) — never a stale cache. Says
    loudly if a completed week is still unpublished, since every trailing
    average below would silently omit it.
    """
    with db.session() as conn:
        db.create_schema(conn)
        nfl_stats.ingest(conn, [season])
        status = nfl_stats.ingest_status(conn, season)
    print(nfl_stats.format_status(status))


def build_pool(export_path: str, season: int, week: int) -> pd.DataFrame:
    parsed = parse_dk_export(export_path)
    unavailable = len(parsed) - len(
        parsed[~parsed["status"].fillna("").str.upper().isin(("OUT", "IR", "IR-R", "SUSP", "NA"))]
    )
    if unavailable:
        print(f"excluding {unavailable} OUT/IR players")
    flagged = questionable(parsed)
    if len(flagged):
        print(f"questionable (kept): {', '.join(sorted(flagged['dk_name']))}")

    pool = from_export(export_path)
    slate_id = parse_slate_filename(export_path)["slate_id"]
    overrides_path = config_mod.load().path("projection_overrides")
    try:
        overrides = load_overrides(overrides_path, slate_id)
    except FileNotFoundError:
        overrides = None

    with db.session() as conn:
        resolved = resolve_pool(conn, pool, season=season, week=week)   # T14: the week's rosters
        resolved_n = resolved["gsis_id"].notna().sum()
        projected = project_live_pool(conn, resolved, season=season, week=week)
    v4_n = (projected["proj_source"] == "v4").sum()
    print(f"resolved {resolved_n}/{len(projected)} to history; "
          f"{v4_n} projected with v4 (role-aware), {len(projected) - v4_n} on AvgPointsPerGame")

    # A committed override is the owner's explicit read and wins over the
    # model -- applied AFTER projection (before T22 it ran first and v3 then
    # overwrote it for anyone it could project), and labelled so it is visible.
    if overrides is not None and len(overrides):
        print(f"applying {len(overrides)} committed projection override(s) over the model")
        projected = apply_overrides(projected, overrides)
        projected.loc[projected["name"].isin(overrides["dk_name"]), "proj_source"] = "override"

    # T22: the role each projection rests on, for the players that decide the
    # slate. A backup QB at rank 2 should read as a small number here; if the
    # depth chart is wrong (it happens), this is where to see it and fix it
    # with a committed override.
    top = projected.sort_values("salary", ascending=False).head(16)
    print("\nrole behind each projection (top 16 by salary):")
    print(f"     {'player':<22}{'team':<5}{'pos':<4}{'depth':>6}{'rank':>5}  {'injury':<13}{'proj':>6}  source")
    for _, r in top.iterrows():
        depth = "-" if pd.isna(r.get("depth_rank")) else f"{int(r['depth_rank'])}"
        rank = "-" if pd.isna(r.get("eff_rank")) else f"{int(r['eff_rank'])}"
        inj = r.get("injury_status") or ""
        flag = "  TEAM CHANGE" if bool(r.get("team_changed", False)) else ""
        print(f"     {r['name']:<22}{r['team']:<5}{r['position']:<4}{depth:>6}{rank:>5}  {inj:<13}"
              f"{r['projection']:>6.1f}  {r['proj_source']}{flag}")
    print()

    floored = projected[projected["salary"] >= SALARY_FLOOR].reset_index(drop=True)
    if len(floored) < len(projected):
        print(f"salary floor ${SALARY_FLOOR}: excluded "
              f"{len(projected) - len(floored)} minimum-priced players (T17)\n")
    return floored
