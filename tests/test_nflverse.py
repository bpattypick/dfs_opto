"""Raw nflverse archiving: closed seasons cached once, live data snapshotted daily.

T21. Before this, a season in progress was archived under a bare filename on
first download and reused forever — every later ingest would have used the
stale week-1 file — and ``games.csv`` (scores, lines) was cached the same way.
No network in these tests: ``requests.get`` is replaced with a fake.
"""

from __future__ import annotations

import io
from datetime import date

import pandas as pd
import pytest
import requests

from src import nflverse

TODAY = date(2026, 9, 17)
TOMORROW = date(2026, 9, 18)


class FakeResponse:
    def __init__(self, content: bytes, status: int = 200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(nflverse, "_archive_dir", lambda cfg=None: tmp_path)
    return tmp_path


@pytest.fixture
def remote(monkeypatch):
    """A fake remote: ``remote['content']`` is served; ``remote['down']`` fails."""
    state = {"content": b"v1", "down": False, "calls": []}

    def get(url, timeout):
        state["calls"].append(url)
        if state["down"]:
            raise requests.ConnectionError("network down")
        return FakeResponse(state["content"])

    monkeypatch.setattr(nflverse.requests, "get", get)
    return state


def parquet_bytes(frame: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    frame.to_parquet(buf)
    return buf.getvalue()


class TestLiveSeason:
    def test_the_season_in_progress_is_live(self):
        assert nflverse.is_live_season(2026, TODAY)

    def test_last_season_stays_live_until_march(self):
        # Stat corrections land into February; 2025's snap counts were last
        # touched 2026-02-09.
        assert nflverse.is_live_season(2025, date(2026, 2, 28))
        assert not nflverse.is_live_season(2025, date(2026, 3, 1))

    def test_old_seasons_are_closed(self):
        assert not nflverse.is_live_season(2019, TODAY)


class TestClosedSeasonArchive:
    URLS = ("https://example.test/a.parquet",)

    def test_downloads_once_then_reuses_the_bare_file(self, archive, remote):
        first = nflverse._cached(self.URLS, "a.parquet", refresh=False, live=False)
        second = nflverse._cached(self.URLS, "a.parquet", refresh=False, live=False)
        assert first == second == archive / "a.parquet"
        assert len(remote["calls"]) == 1

    def test_refresh_redownloads(self, archive, remote):
        nflverse._cached(self.URLS, "a.parquet", refresh=False, live=False)
        remote["content"] = b"v2"
        nflverse._cached(self.URLS, "a.parquet", refresh=True, live=False)
        assert (archive / "a.parquet").read_bytes() == b"v2"
        assert len(remote["calls"]) == 2

    def test_falls_through_to_the_next_url(self, archive, monkeypatch):
        calls = []

        def get(url, timeout):
            calls.append(url)
            return FakeResponse(b"", status=404) if "first" in url else FakeResponse(b"ok")

        monkeypatch.setattr(nflverse.requests, "get", get)
        path = nflverse._cached(("https://x/first", "https://x/second"), "a.parquet",
                                refresh=False, live=False)
        assert path.read_bytes() == b"ok"
        assert calls == ["https://x/first", "https://x/second"]

    def test_all_urls_failing_raises(self, archive, remote):
        remote["down"] = True
        with pytest.raises(nflverse.NflverseUnavailable):
            nflverse._cached(self.URLS, "a.parquet", refresh=False, live=False)
        assert not (archive / "a.parquet").exists()


class TestLiveSnapshots:
    URLS = ("https://example.test/live.parquet",)

    def cached(self, today, refresh=False):
        return nflverse._cached(self.URLS, "live.parquet", refresh=refresh,
                                live=True, today=today)

    def test_snapshot_is_dated_not_bare(self, archive, remote):
        path = self.cached(TODAY)
        assert path == archive / "live_2026-09-17.parquet"
        assert not (archive / "live.parquet").exists()

    def test_same_day_reuses_todays_snapshot(self, archive, remote):
        self.cached(TODAY)
        self.cached(TODAY)
        assert len(remote["calls"]) == 1

    def test_a_new_day_fetches_again_and_keeps_the_old_snapshot(self, archive, remote):
        self.cached(TODAY)
        remote["content"] = b"v2"
        new = self.cached(TOMORROW)
        assert new == archive / "live_2026-09-18.parquet"
        assert (archive / "live_2026-09-17.parquet").read_bytes() == b"v1"
        assert new.read_bytes() == b"v2"

    def test_refresh_redownloads_todays_snapshot(self, archive, remote):
        self.cached(TODAY)
        remote["content"] = b"v2"
        self.cached(TODAY, refresh=True)
        assert (archive / "live_2026-09-17.parquet").read_bytes() == b"v2"

    def test_failed_download_raises_and_never_falls_back_to_stale(self, archive, remote):
        self.cached(TODAY)
        remote["down"] = True
        with pytest.raises(nflverse.NflverseUnavailable) as err:
            self.cached(TOMORROW)
        # The old snapshot is untouched and named in the error, but not used.
        assert (archive / "live_2026-09-17.parquet").read_bytes() == b"v1"
        assert "live_2026-09-17.parquet" in str(err.value)
        assert not (archive / "live_2026-09-18.parquet").exists()

    def test_snapshots_lists_oldest_first_and_ignores_other_files(self, archive, remote):
        self.cached(TOMORROW)
        self.cached(TODAY)
        (archive / "live.parquet").write_bytes(b"bare")
        (archive / "other_2026-09-17.parquet").write_bytes(b"x")
        names = [p.name for p in nflverse.snapshots("live.parquet")]
        assert names == ["live_2026-09-17.parquet", "live_2026-09-18.parquet"]


class TestDatasets:
    def test_closed_season_player_week_uses_bare_name(self, archive, remote):
        remote["content"] = parquet_bytes(pd.DataFrame({"season": [2019], "week": [1]}))
        frame = nflverse.player_week(2019, today=TODAY)
        assert (archive / "stats_player_week_2019.parquet").exists()
        assert list(frame.week) == [1]
        assert remote["calls"] == [nflverse.PLAYER_WEEK_URLS[0].format(season=2019)]

    def test_live_season_player_week_is_a_dated_snapshot(self, archive, remote):
        remote["content"] = parquet_bytes(pd.DataFrame({"season": [2026], "week": [1]}))
        nflverse.player_week(2026, today=TODAY)
        assert (archive / "stats_player_week_2026_2026-09-17.parquet").exists()
        assert not (archive / "stats_player_week_2026.parquet").exists()

    def test_team_week_snap_counts_and_rosters_follow_the_same_rule(self, archive, remote):
        remote["content"] = parquet_bytes(pd.DataFrame({"season": [2026], "week": [1]}))
        nflverse.team_week(2026, today=TODAY)
        nflverse.snap_counts(2026, today=TODAY)
        nflverse.snap_counts(2019, today=TODAY)
        nflverse.weekly_rosters(2026, today=TODAY)
        nflverse.weekly_rosters(2019, today=TODAY)
        assert (archive / "stats_team_week_2026_2026-09-17.parquet").exists()
        assert (archive / "snap_counts_2026_2026-09-17.parquet").exists()
        assert (archive / "snap_counts_2019.parquet").exists()
        assert (archive / "roster_weekly_2026_2026-09-17.parquet").exists()
        assert (archive / "roster_weekly_2019.parquet").exists()
        assert nflverse.SNAP_COUNT_URLS[0].format(season=2026) in remote["calls"]
        assert nflverse.WEEKLY_ROSTER_URLS[0].format(season=2019) in remote["calls"]

    def test_games_is_always_a_dated_snapshot(self, archive, remote):
        remote["content"] = b"game_id,season,week\n2026_01_NE_SEA,2026,1\n"
        frame = nflverse.games(today=TODAY)
        assert (archive / "games_2026-09-17.csv").exists()
        assert not (archive / "games.csv").exists()
        assert list(frame.game_id) == ["2026_01_NE_SEA"]
