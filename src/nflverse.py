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

``nfl_data_py`` is still used where it works — ``import_ids`` and
``import_snap_counts`` (see :mod:`src.ingest.nfl_stats`).

Every download is archived byte-for-byte under ``data/raw/nflverse/`` before it
is parsed, per the spec §2 rule: parsers have bugs, source data disappears.
"""

from __future__ import annotations

import logging
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
GAMES_URLS = (
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv",
    "http://www.habitatring.com/games.csv",
)

_TIMEOUT = 120


class NflverseUnavailable(RuntimeError):
    """No candidate URL for a dataset could be downloaded."""


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


def _cached(urls: tuple[str, ...], filename: str, refresh: bool, cfg=None) -> Path:
    dest = _archive_dir(cfg) / filename
    if dest.exists() and not refresh:
        log.debug("using archived %s", dest)
        return dest
    return _download(tuple(u.format() if "{" not in u else u for u in urls), dest)


def player_week(season: int, refresh: bool = False, cfg=None) -> pd.DataFrame:
    """Per-player per-week stats for one season (nflverse ``stats_player_week``)."""
    urls = tuple(u.format(season=season) for u in PLAYER_WEEK_URLS)
    path = _cached(urls, f"stats_player_week_{season}.parquet", refresh, cfg)
    return pd.read_parquet(path)


def team_week(season: int, refresh: bool = False, cfg=None) -> pd.DataFrame:
    """Per-team per-week stats for one season — the DST inputs."""
    urls = tuple(u.format(season=season) for u in TEAM_WEEK_URLS)
    path = _cached(urls, f"stats_team_week_{season}.parquet", refresh, cfg)
    return pd.read_parquet(path)


def games(refresh: bool = False, cfg=None) -> pd.DataFrame:
    """The full nflverse schedule file, including closing spreads and totals.

    Spec §5.1: this is why the backtest needs no purchased historical odds.
    """
    path = _cached(GAMES_URLS, "games.csv", refresh, cfg)
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
