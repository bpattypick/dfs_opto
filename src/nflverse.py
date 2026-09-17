"""Direct nflverse data access, with raw archiving.

Why this exists instead of just calling ``nfl_data_py`` (see docs/data-sources.md):

* ``import_weekly_data`` reads the frozen ``player_stats`` release tag, which
  stops at 2024. Current seasons live under the ``stats_player`` tag. This one
  is a genuine staleness bug in the library — 2025 is simply unreachable
  through it.
* ``import_schedules`` reads ``http://www.habitatring.com/games.csv`` over plain
  HTTP and single-sources it. That host is unreachable from restricted networks
  (it was blocked in the environment this was built in), so we prefer the GitHub
  mirror of the same file and keep habitatring as a fallback.
* ``import_snap_counts`` concatenates every requested season in one call, so a
  single unpublished year fails all of them — and archives nothing.

``nfl_data_py`` is still used where it works (``import_ids``).

Every download is archived byte-for-byte under ``data/raw/nflverse/`` before it
is parsed, per the spec §2 rule: parsers have bugs, source data disappears.

**Live data is snapshotted, not cached.** A closed season's file never changes,
so it is archived once under its bare name and reused. A season still in
progress is republished every week (and corrected for weeks after), and
``games.csv`` changes all year — scores as games finish, lines as they are
posted, next season's schedule in spring. Those are fetched at most once per
day into a *dated* snapshot (``stats_player_week_2026_2026-09-17.parquet``)
and earlier snapshots are kept, so the archive is both current and complete.
Before T21 the live season would have been cached forever on first download
and every later ingest would have silently used the stale file.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from src import config as config_mod

log = logging.getLogger(__name__)

_RELEASE = "https://github.com/nflverse/nflverse-data/releases/download"

# Tried in order; the first URL that downloads wins.
PLAYER_WEEK_URLS = (
    _RELEASE + "/stats_player/stats_player_week_{season}.parquet",
    _RELEASE + "/player_stats/player_stats_{season}.parquet",
)
TEAM_WEEK_URLS = (
    _RELEASE + "/stats_team/stats_team_week_{season}.parquet",
)
SNAP_COUNT_URLS = (
    _RELEASE + "/snap_counts/snap_counts_{season}.parquet",
)
GAMES_URLS = (
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
    "http://www.habitatring.com/games.csv",
)

_TIMEOUT = 120

# A season's files keep changing until well after its last game: weekly
# additions through the regular season, then stat corrections into February
# (nflverse last touched the 2025 snap counts on 2026-02-09). From March 1 of
# the following year the season is treated as closed and its archive as final.
SEASON_CLOSES = (3, 1)  # (month, day) of season + 1

_SNAPSHOT_DATE = r"_(\d{4}-\d{2}-\d{2})"


class NflverseUnavailable(RuntimeError):
    """No candidate URL for a dataset could be downloaded."""


def is_live_season(season: int, today: date | None = None) -> bool:
    """True while ``season``'s published files may still change."""
    today = today or date.today()
    return today < date(season + 1, *SEASON_CLOSES)


def _archive_dir(cfg: config_mod.Config | None = None) -> Path:
    cfg = cfg or config_mod.load()
    path = cfg.path("raw_nflverse")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _download(urls: tuple[str, ...], dest: Path) -> Path:
    """Download the first working URL to ``dest``, writing atomically."""
    errors: list[str] = []
    for url in urls:
        try:
            resp = requests.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - report every candidate
            errors.append(f"{url} -> {type(exc).__name__}: {exc}")
            log.debug("nflverse candidate failed: %s", errors[-1])
            continue
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(resp.content)
        tmp.replace(dest)
        log.info("archived %s (%.1f KB) from %s", dest.name, len(resp.content) / 1024, url)
        return dest

    raise NflverseUnavailable(
        "could not download "
        + dest.name
        + "; tried:\n  "
        + "\n  ".join(errors)
    )


def _snapshot_path(directory: Path, filename: str, today: date) -> Path:
    p = Path(filename)
    return directory / f"{p.stem}_{today:%Y-%m-%d}{p.suffix}"


def snapshots(filename: str, cfg=None) -> list[Path]:
    """Every archived dated snapshot of ``filename``, oldest first."""
    p = Path(filename)
    pattern = re.compile(re.escape(p.stem) + _SNAPSHOT_DATE + re.escape(p.suffix) + "$")
    directory = _archive_dir(cfg)
    return sorted(f for f in directory.iterdir() if pattern.match(f.name))


def _cached(
    urls: tuple[str, ...],
    filename: str,
    refresh: bool,
    cfg=None,
    live: bool = False,
    today: date | None = None,
) -> Path:
    """Return an archived copy of ``filename``, downloading if needed.

    Closed data (``live=False``): archived once under the bare name, reused
    forever unless ``refresh``.

    Live data (``live=True``): one dated snapshot per day. Today's snapshot is
    reused if present (``refresh`` re-downloads it); a new day gets a new
    file and earlier snapshots are never touched. A failed download raises —
    it does **not** fall back to an older snapshot, because "silently used
    last week's file" is exactly the failure this exists to prevent. The
    error names the newest snapshot so a human can decide.
    """
    directory = _archive_dir(cfg)
    if not live:
        dest = directory / filename
        if dest.exists() and not refresh:
            log.debug("using archived %s", dest)
            return dest
        return _download(urls, dest)

    today = today or date.today()
    dest = _snapshot_path(directory, filename, today)
    if dest.exists() and not refresh:
        log.debug("using today's snapshot %s", dest)
        return dest
    try:
        return _download(urls, dest)
    except NflverseUnavailable as exc:
        earlier = snapshots(filename, cfg)
        hint = (
            f"; newest archived snapshot is {earlier[-1].name} "
            "(NOT used automatically: it may be stale)"
            if earlier
            else "; no archived snapshot exists"
        )
        raise NflverseUnavailable(str(exc) + hint) from exc


def player_week(season: int, refresh: bool = False, cfg=None, today: date | None = None) -> pd.DataFrame:
    """Per-player per-week stats for one season (nflverse ``stats_player_week``)."""
    urls = tuple(u.format(season=season) for u in PLAYER_WEEK_URLS)
    path = _cached(urls, f"stats_player_week_{season}.parquet", refresh, cfg,
                   live=is_live_season(season, today), today=today)
    return pd.read_parquet(path)


def team_week(season: int, refresh: bool = False, cfg=None, today: date | None = None) -> pd.DataFrame:
    """Per-team per-week stats for one season — the DST inputs."""
    urls = tuple(u.format(season=season) for u in TEAM_WEEK_URLS)
    path = _cached(urls, f"stats_team_week_{season}.parquet", refresh, cfg,
                   live=is_live_season(season, today), today=today)
    return pd.read_parquet(path)


def snap_counts(season: int, refresh: bool = False, cfg=None, today: date | None = None) -> pd.DataFrame:
    """Per-player per-week snap counts for one season (PFR-sourced, keyed by name)."""
    urls = tuple(u.format(season=season) for u in SNAP_COUNT_URLS)
    path = _cached(urls, f"snap_counts_{season}.parquet", refresh, cfg,
                   live=is_live_season(season, today), today=today)
    return pd.read_parquet(path)


def games(refresh: bool = False, cfg=None, today: date | None = None) -> pd.DataFrame:
    """The full nflverse schedule file, including closing spreads and totals.

    Spec §5.1: this is why the backtest needs no purchased historical odds.
    One file covers every season and it changes all year, so it is always
    fetched as a dated snapshot.
    """
    path = _cached(GAMES_URLS, "games.csv", refresh, cfg, live=True, today=today)
    return pd.read_csv(path, low_memory=False)


def available_seasons(candidates: list[int], cfg=None) -> list[int]:
    """Subset of ``candidates`` whose player-week file can actually be fetched."""
    ok: list[int] = []
    for season in candidates:
        try:
            player_week(season, cfg=cfg)
            ok.append(season)
        except NflverseUnavailable:
            log.warning("no player-week data published for %s", season)
    return ok
