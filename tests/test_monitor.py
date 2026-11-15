"""通知の判定と監視ループのテスト（偽の API・偽の時計を使う）。"""
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import monitor as M
from config import NotificationRule, VideoConfig
from data_manager import read_history_csv, read_notifications_csv
from tests.conftest import (FAKE_KEY, T0, VID_A, VID_B, FakeClient, RecordingNotifier,
                            make_config, regular, series)
from youtube_api import AuthError, QuotaExceededError, YouTubeAPIError


def video(threshold=100, window=60, cooldown=60, milestone=0, notify=True):
    return VideoConfig(VID_A, VID_A, "推し", notify=notify,
                       rule=NotificationRule(window, threshold, cooldown, milestone))


# ---------------------------------------------------------------- 判定の単体テスト
def test_surge_when_increase_reaches_threshold():
    s = regular(13, step_views=10)  # 60 分で +120
    alert = M.evaluate_surge(s, video(threshold=120), M.NotificationLedger(), interval_seconds=300)
    assert alert is not None and alert.increase == 120 and alert.kind == "surge"
    assert M.evaluate_surge(s, video(threshold=121), M.NotificationLedger(), interval_seconds=300) is None


def test_no_surge_with_insufficient_data_or_decrease():
    assert M.evaluate_surge(regular(6, 1000), video(threshold=1), M.NotificationLedger(),
                            interval_seconds=300) is None
    falling = series([0, 30, 60], [5000, 4000, 3000])
    assert M.evaluate_surge(falling, video(threshold=1), M.NotificationLedger(), interval_seconds=300) is None


def test_notify_false_disables_alerts():
    s = regular(13, step_views=100)
    assert M.evaluate_surge(s, video(threshold=1, notify=False), M.NotificationLedger(),
                            interval_seconds=300) is None


def test_cooldown():
    ledger = M.NotificationLedger()
    s = regular(13, step_views=100)
    alert = M.evaluate_surge(s, video(threshold=100, cooldown=30), ledger, interval_seconds=300)
    ledger.remember(alert)
    s2 = regular(18, step_views=100)  # 25 分後
    assert M.evaluate_surge(s2, video(threshold=100, cooldown=30), ledger, interval_seconds=300) is None
    s3 = regular(19, step_views=100)  # 30 分後
    assert M.evaluate_surge(s3, video(threshold=100, cooldown=30), ledger, interval_seconds=300) is not None


def test_milestone_once_per_value():
    ledger = M.NotificationLedger()
    v = video(milestone=1000)
    s = series([0, 5], [990, 1010])
    alert = M.evaluate_milestone(s, v, ledger)
    assert alert.milestone == 1000
    ledger.remember(alert)
    s2 = series([0, 5, 10, 15], [990, 1010, 995, 1020])  # 減って再び超えても 2 回目は出さない
    assert M.evaluate_milestone(s2, v, ledger) is None
    jump = series([0, 5], [1500, 3200])  # 2 つ飛ばしたら一番大きい節目だけ
    assert M.evaluate_milestone(jump, v, M.NotificationLedger()).milestone == 3000
    assert M.evaluate_milestone(series([0], [5000]), v, M.NotificationLedger()) is None  # 初回は出さない


def test_ledger_restored_from_records():
    records = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-11-15T00:00:00Z", "2026-11-15T01:00:00Z", "2026-11-15T02:00:00Z"]),
        "video_id": [VID_A, VID_A, VID_B], "kind": ["surge", "surge", "milestone"],
        "milestone": [float("nan"), float("nan"), 1000.0],
    })
    ledger = M.NotificationLedger(records)
    assert ledger.last_surge[VID_A] == pd.Timestamp("2026-11-15T01:00:00Z")
    assert ledger.milestones[VID_B] == {1000}


def test_many_alerts_are_summarized():
    ts = pd.Timestamp(T0)
    alerts = [M.Alert("surge", f"video{i:06d}", f"動画{i}", ts, 100, increase=50, window_minutes=60,
                      threshold=10) for i in range(7)]
    assert len(M.plan_messages(alerts[:3], 3)) == 3
    msgs = M.plan_messages(alerts, 3, "http://localhost:8501")
    assert len(msgs) == 1 and "7 件" in msgs[0].title and "ほか 2 件" in msgs[0].body


def test_alert_message_mentions_measured_span():
    a = M.Alert("surge", VID_A, "推し", pd.Timestamp(T0), 5000, increase=1200, window_minutes=60,
                threshold=1000, span_minutes=50)
    msg = a.message()
    assert "+1,200回" in msg.body and "実測50分" in msg.body and msg.tag == VID_A


def test_schedule_helpers():
    t = datetime(2026, 11, 15, 12, 4, 50, tzinfo=timezone.utc)
    assert M.next_aligned_time(t, 300) == datetime(2026, 11, 15, 12, 10, tzinfo=timezone.utc)
    t = datetime(2026, 11, 15, 12, 0, 3, tzinfo=timezone.utc)
    assert M.next_aligned_time(t, 300) == datetime(2026, 11, 15, 12, 5, tzinfo=timezone.utc)
    # 太平洋時間 0:05（11 月は標準時 UTC-8）
    assert M.next_quota_reset(t) == datetime(2026, 11, 16, 8, 5, tzinfo=timezone.utc)


# ---------------------------------------------------------------- 監視ループ
def make_service(tmp_path, clock, client=None, notifier=None, **config_overrides):
    cfg = make_config(tmp_path, **config_overrides)
    client = client or FakeClient(clock, {VID_A: 10_000, VID_B: 500}, step={VID_A: 60, VID_B: 1})
    notifier = notifier or RecordingNotifier()
    svc = M.MonitorService(cfg, client=client, notifier=notifier, clock=clock, sleep=lambda s: None)
    svc.startup()
    return svc, client, notifier


def run(svc, clock, n):
    results = []
    for _ in range(n):
        results.append(svc.run_cycle())
        clock.advance(minutes=5)
    return results


def test_cycle_writes_history_and_notifies(tmp_path, clock):
    svc, client, notifier = make_service(tmp_path, clock)
    results = run(svc, clock, 14)
    df, _ = read_history_csv(svc.paths.history)
    assert len(df) == 28
    surge = [a for r in results for a in r.alerts]
    assert [a.video_id for a in surge] == [VID_A]  # +600/60 分 ≥ 500。B は +12 で通知しない
    assert surge[0].increase == 600
    assert len(notifier.sent) == 1
    notes = read_notifications_csv(svc.paths.notifications)
    assert list(notes["kind"]) == ["surge"]
    status = json.loads(svc.paths.status.read_text(encoding="utf-8"))
    assert status["state"] == "running" and status["quota"]["used"] == 14
    catalog = json.loads(svc.paths.videos.read_text(encoding="utf-8"))
    assert catalog[VID_A]["title"] == f"title {VID_A}"


def test_restart_keeps_history_and_cooldown(tmp_path, clock):
    svc, client, notifier = make_service(tmp_path, clock)
    run(svc, clock, 14)
    assert len(notifier.sent) == 1
    # 再起動（同じ data フォルダー）: 履歴と通知済みの記録を引き継ぐので、すぐには再通知しない
    notifier2 = RecordingNotifier()
    svc2, _, _ = make_service(tmp_path, clock, client=client, notifier=notifier2)
    assert len(svc2.history) > 0
    run(svc2, clock, 6)
    assert notifier2.sent == []
    run(svc2, clock, 8)  # クールダウン 60 分が過ぎたら再び通知
    assert len(notifier2.sent) == 1


def test_missing_video_is_not_judged_with_old_data(tmp_path, clock):
    svc, client, notifier = make_service(tmp_path, clock)
    run(svc, clock, 12)
    client.missing.add(VID_A)
    result = svc.run_cycle()
    assert result.missing == [VID_A] and result.alerts == []
    status = json.loads(svc.paths.status.read_text(encoding="utf-8"))
    assert status["missing_videos"] == [VID_A]


@pytest.mark.parametrize("error, state", [
    (QuotaExceededError("quota", status=403), "waiting_quota"),
    (AuthError("bad key", status=400, reason="API_KEY_INVALID"), "error"),
    (YouTubeAPIError("network", retryable=True), "error"),
])
def test_api_errors_do_not_stop_the_monitor(tmp_path, clock, error, state):
    svc, client, notifier = make_service(tmp_path, clock)
    client.error = error
    result = svc.run_cycle()
    assert result.error
    assert json.loads(svc.paths.status.read_text(encoding="utf-8"))["state"] == state
    client.error = None
    assert not svc.run_cycle().error  # 次の回には回復する


def test_run_forever_waits_until_quota_reset(tmp_path, clock):
    waited = []

    def fake_sleep(seconds):
        waited.append(seconds)
        clock.advance(seconds=seconds)

    svc, client, _ = make_service(tmp_path, clock)
    svc.sleep = fake_sleep
    client.error = QuotaExceededError("quota", status=403)
    svc.run_forever(max_cycles=2)
    assert sum(waited) > 3600 * 8  # 太平洋時間 0 時まで待つ
    assert json.loads(svc.paths.status.read_text(encoding="utf-8"))["state"] == "stopped"


def test_notification_disabled(tmp_path, clock):
    svc, _, notifier = make_service(tmp_path, clock, notification={"enabled": False, "threshold": 1})
    run(svc, clock, 14)
    assert notifier.sent == []


def test_failed_toast_is_recorded(tmp_path, clock):
    svc, _, notifier = make_service(tmp_path, clock, notifier=RecordingNotifier(ok=False))
    run(svc, clock, 14)
    notes = read_notifications_csv(svc.paths.notifications)
    assert list(notes["delivered"]) == [False]


def test_locked_history_still_evaluates_from_memory(tmp_path, clock, monkeypatch):
    svc, _, notifier = make_service(tmp_path, clock)
    monkeypatch.setattr(svc.history_store, "flush", lambda: False)  # Excel で開いている状態
    run(svc, clock, 14)
    assert len(notifier.sent) == 1


def test_config_changes_are_reloaded(tmp_path, clock):
    cfg_path = tmp_path / "config.json"
    data = {"api_key": FAKE_KEY, "videos": [VID_A], "notification": {"threshold": 100000}}
    cfg_path.write_text(json.dumps(data), encoding="utf-8")
    from config import load_config

    cfg = load_config(cfg_path)
    client = FakeClient(clock, {VID_A: 0}, step={VID_A: 100})
    notifier = RecordingNotifier()
    svc = M.MonitorService(cfg, client=client, notifier=notifier, clock=clock, sleep=lambda s: None)
    svc.startup()
    run(svc, clock, 13)
    assert notifier.sent == []
    data["notification"]["threshold"] = 1000
    cfg_path.write_text(json.dumps(data), encoding="utf-8")
    import os

    os.utime(cfg_path, (1, 1))  # 更新時刻を確実に変える
    run(svc, clock, 1)
    assert svc.config.notification.threshold == 1000
    assert len(notifier.sent) == 1
    cfg_path.write_text("{ broken", encoding="utf-8")
    os.utime(cfg_path, (2, 2))
    run(svc, clock, 1)  # 壊れた設定は無視して続ける
    assert svc.config.notification.threshold == 1000


def test_retention_prunes_old_rows_on_startup(tmp_path, clock):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    old = (T0 - timedelta(days=40)).isoformat()
    new = (T0 - timedelta(days=1)).isoformat()
    (data_dir / "history.csv").write_text(
        "timestamp,video_id,view_count,like_count,comment_count\n"
        f"{old},{VID_A},1,,\n{new},{VID_A},2,,\n", encoding="utf-8")
    svc, _, _ = make_service(tmp_path, clock)
    df, _ = read_history_csv(svc.paths.history)
    assert list(df["view_count"]) == [2]
    svc2, _, _ = make_service(tmp_path / "keep", clock, retention_days=0)
    assert svc2.config.retention_days == 0


def test_status_heartbeat_while_waiting(tmp_path, clock):
    def fake_sleep(seconds):
        clock.advance(seconds=seconds)

    svc, _, _ = make_service(tmp_path, clock)
    svc.sleep = fake_sleep
    svc._sleep_until(clock() + timedelta(hours=2))
    status = json.loads(svc.paths.status.read_text(encoding="utf-8"))
    assert pd.Timestamp(status["updated_at"]) >= pd.Timestamp(clock()) - pd.Timedelta(minutes=2)


def test_unexpected_error_does_not_stop_the_loop(tmp_path, clock):
    def fake_sleep(seconds):
        clock.advance(seconds=seconds)

    svc, client, _ = make_service(tmp_path, clock)
    svc.sleep = fake_sleep
    client.error = RuntimeError("disk full?")
    svc.run_forever(max_cycles=3)
    assert client.calls == 3  # 例外が起きても次の回に進む
    assert json.loads(svc.paths.status.read_text(encoding="utf-8"))["state"] == "stopped"
