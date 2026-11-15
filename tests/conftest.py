"""テスト共通の道具（YouTube API は使わない）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import config as config_module
from youtube_api import FetchResult, VideoStats

VID_A = "dQw4w9WgXcQ"
VID_B = "abcdefghijk"
FAKE_KEY = "AIza" + "x" * 35
T0 = datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc)


def series(minutes: list[float], views: list[int], start: datetime = T0) -> pd.DataFrame:
    """経過分と視聴回数から 1 本の動画の時系列を作る。"""
    ts = [pd.Timestamp(start) + pd.Timedelta(minutes=m) for m in minutes]
    return pd.DataFrame({"timestamp": pd.to_datetime(ts, utc=True).as_unit("ns"),
                         "view_count": pd.Series(views, dtype="int64")})


def regular(n: int, step_views: int = 10, start_views: int = 1000, every: int = 5) -> pd.DataFrame:
    return series([i * every for i in range(n)], [start_views + step_views * i for i in range(n)])


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


@dataclass
class FakeClient:
    """呼ばれるたびに視聴回数を増やして返す偽の YouTube クライアント。"""

    clock: Clock
    views: dict[str, int]
    step: dict[str, int] = field(default_factory=dict)
    missing: set[str] = field(default_factory=set)
    error: Exception | None = None
    calls: int = 0

    def fetch_videos(self, ids):
        self.calls += 1
        if self.error is not None:
            raise self.error
        stats = {}
        for vid in ids:
            if vid in self.missing or vid not in self.views:
                continue
            self.views[vid] += self.step.get(vid, 0)
            stats[vid] = VideoStats(vid, self.views[vid], 10, 2, title=f"title {vid}", channel_title="ch")
        return FetchResult(fetched_at=self.clock(), stats=stats,
                           missing=[v for v in ids if v not in stats], quota_used=1)


class RecordingNotifier:
    name = "recording"

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent = []

    def send(self, message) -> bool:
        self.sent.append(message)
        return self.ok


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make_config(tmp_path: Path, **overrides) -> config_module.AppConfig:
    data = {
        "api_key": FAKE_KEY,
        "videos": [{"url": f"https://youtu.be/{VID_A}", "name": "動画A"},
                   {"url": f"https://www.youtube.com/watch?v={VID_B}", "name": "動画B"}],
        "notification": {"threshold": 500},
    }
    data.update(overrides)
    return config_module.parse_config(data, base_dir=tmp_path, environ={})
