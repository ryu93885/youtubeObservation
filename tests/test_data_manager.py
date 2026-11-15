"""CSV / JSON の読み書きのテスト。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

import data_manager as D
from instance_lock import AlreadyRunningError, InstanceLock
from tests.conftest import T0, VID_A, VID_B

HEADER = "timestamp,video_id,view_count,like_count,comment_count\n"


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_roundtrip_utc(tmp_path):
    store = D.HistoryStore(tmp_path / "history.csv")
    jst = timezone(timedelta(hours=9))
    store.append_samples(datetime(2026, 11, 15, 9, 0, tzinfo=jst),
                         [{"video_id": VID_A, "view_count": 100, "like_count": 5, "comment_count": None}])
    text = (tmp_path / "history.csv").read_text(encoding="utf-8")
    assert text.splitlines()[1] == f"2026-11-15T00:00:00+00:00,{VID_A},100,5,"  # UTC で保存
    df, report = D.read_history_csv(tmp_path / "history.csv")
    assert df["timestamp"].iloc[0] == pd.Timestamp("2026-11-15T00:00:00Z")
    assert str(df["timestamp"].dtype) == "datetime64[ns, UTC]"
    assert df["view_count"].iloc[0] == 100
    assert pd.isna(df["comment_count"].iloc[0])
    assert report.rows_invalid == 0


def test_naive_datetime_is_rejected(tmp_path):
    store = D.HistoryStore(tmp_path / "history.csv")
    with pytest.raises(ValueError):
        store.append_samples(datetime(2026, 11, 15), [{"video_id": VID_A, "view_count": 1}])


def test_missing_file_gives_empty_table(tmp_path):
    df, report = D.read_history_csv(tmp_path / "nothing.csv")
    assert df.empty and list(df.columns) == D.HISTORY_COLUMNS


def test_partial_last_line_is_ignored(tmp_path):
    p = tmp_path / "history.csv"
    write(p, HEADER + f"2026-11-15T00:00:00+00:00,{VID_A},100,,\n2026-11-15T00:05:00+00:00,{VID_A},12")
    df, report = D.read_history_csv(p)
    assert len(df) == 1 and report.partial_line_skipped


def test_truncated_row_completed_later_is_rejected(tmp_path):
    p = tmp_path / "history.csv"
    write(p, HEADER + f"2026-11-15T00:00:00+00:00,{VID_A},100,,\n2026-11-15T00:05:00+00:00,{VID_A},12")
    store = D.HistoryStore(p)
    store.append_samples(T0 + timedelta(minutes=10), [{"video_id": VID_A, "view_count": 130}])
    df, report = D.read_history_csv(p)
    assert list(df["view_count"]) == [100, 130]  # 途中で切れた「12」は使わない
    assert report.rows_invalid == 1


def test_duplicates_and_invalid_rows(tmp_path):
    p = tmp_path / "history.csv"
    write(p, HEADER
          + f"2026-11-15T00:05:00+00:00,{VID_A},150,,\n"
          + f"2026-11-15T00:00:00+00:00,{VID_A},100,,\n"
          + f"2026-11-15T00:05:00+00:00,{VID_A},155,,\n"     # 重複 → 後の行を残す
          + f"not-a-date,{VID_A},1,,\n"
          + "2026-11-15T00:05:00+00:00,short,1,,\n"
          + f"2026-11-15T00:05:00+00:00,{VID_B},-5,,\n"
          + f"2026-11-15T00:05:00+00:00,{VID_B},abc,,\n"
          + f"2026-11-15T09:10:00+09:00,{VID_B},7,,\n"        # +09:00 も UTC に直す
          + f"2026-11-15 00:15:00,{VID_B},8,,\n")              # タイムゾーンなし → UTC とみなす
    df, report = D.read_history_csv(p)
    a = df[df["video_id"] == VID_A]
    assert list(a["view_count"]) == [100, 155]
    assert report.duplicates_removed == 1
    assert report.rows_invalid == 4
    b = df[df["video_id"] == VID_B]
    assert list(b["timestamp"]) == [pd.Timestamp("2026-11-15T00:10:00Z"), pd.Timestamp("2026-11-15T00:15:00Z")]


def test_header_mismatch_stops_without_touching_file(tmp_path):
    p = tmp_path / "history.csv"
    write(p, "date,id,views\n2026-11-15,x,1\n")
    store = D.HistoryStore(p)
    with pytest.raises(D.DataFormatError):
        store.append_samples(T0, [{"video_id": VID_A, "view_count": 1}])
    assert p.read_text(encoding="utf-8") == "date,id,views\n2026-11-15,x,1\n"
    df, report = D.read_history_csv(p)
    assert df.empty and not report.header_ok


def test_locked_file_keeps_rows_pending(tmp_path, monkeypatch):
    p = tmp_path / "history.csv"
    store = D.HistoryStore(p)
    store.append_samples(T0, [{"video_id": VID_A, "view_count": 1}])
    real_open = Path.open

    def locked_open(self, mode="r", *args, **kwargs):
        if self == p and "a" in mode:
            raise PermissionError(13, "locked by Excel")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", locked_open)
    assert store.append_samples(T0 + timedelta(minutes=5), [{"video_id": VID_A, "view_count": 2}]) is False
    assert store.pending_count == 1
    monkeypatch.setattr(Path, "open", real_open)
    assert store.append_samples(T0 + timedelta(minutes=10), [{"video_id": VID_A, "view_count": 3}]) is True
    assert store.pending_count == 0
    df, _ = D.read_history_csv(p)
    assert list(df["view_count"]) == [1, 2, 3]


def test_load_since(tmp_path):
    store = D.HistoryStore(tmp_path / "history.csv")
    for i in range(5):
        store.append_samples(T0 + timedelta(hours=i), [{"video_id": VID_A, "view_count": i}])
    df, _ = store.load(since=T0 + timedelta(hours=3))
    assert list(df["view_count"]) == [3, 4]


def test_prune_keeps_recent_and_unreadable_rows(tmp_path):
    p = tmp_path / "history.csv"
    write(p, HEADER
          + f"2026-10-01T00:00:00+00:00,{VID_A},1,,\n"
          + f"garbage,{VID_A},2,,\n"
          + f"2026-11-15T00:00:00+00:00,{VID_A},3,,\n")
    removed = D.prune_csv_before(p, datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert removed == 1
    assert p.read_text(encoding="utf-8").splitlines() == [
        HEADER.strip(), f"garbage,{VID_A},2,,", f"2026-11-15T00:00:00+00:00,{VID_A},3,,"]
    assert D.prune_csv_before(p, datetime(2026, 11, 1, tzinfo=timezone.utc)) == 0


def test_notifications_csv_roundtrip(tmp_path):
    store = D.CsvAppender(tmp_path / "notifications.csv", D.NOTIFICATION_COLUMNS)
    store.append([{"timestamp": "2026-11-15T00:00:00+00:00", "video_id": VID_A, "kind": "surge",
                   "window_minutes": 60, "increase": 1200, "threshold": 1000, "view_count": 5000,
                   "milestone": None, "delivered": "true", "message": "タイトル, カンマ入り"}])
    df = D.read_notifications_csv(tmp_path / "notifications.csv")
    assert df["increase"].iloc[0] == 1200 and bool(df["delivered"].iloc[0])
    assert df["message"].iloc[0] == "タイトル, カンマ入り"


def test_json_helpers_and_catalog(tmp_path):
    p = tmp_path / "status.json"
    assert D.read_json(p, {"x": 1}) == {"x": 1}
    assert D.write_json_atomic(p, {"日本語": "OK"})
    assert D.read_json(p) == {"日本語": "OK"}
    catalog = D.VideoCatalog.load(tmp_path / "videos.json")
    assert catalog.update({VID_A: {"title": "t1", "channel_title": ""}}, T0)
    assert not catalog.update({VID_A: {"title": "t1"}}, T0 + timedelta(minutes=5))
    assert catalog.update({VID_A: {"title": "t2"}}, T0 + timedelta(minutes=10))
    catalog.save()
    assert D.VideoCatalog.load(tmp_path / "videos.json").title(VID_A) == "t2"
    assert catalog.prune(T0 + timedelta(days=1)) == 1


def test_instance_lock_blocks_second_monitor(tmp_path):
    first = InstanceLock(tmp_path / "monitor.lock")
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            InstanceLock(tmp_path / "monitor.lock").acquire()
    finally:
        first.release()
    with InstanceLock(tmp_path / "monitor.lock"):
        pass  # 解放後は取り直せる
