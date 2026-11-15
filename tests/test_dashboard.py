"""ダッシュボードの起動確認（ブラウザなしで Streamlit の画面を実行する）。"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

import monitor as M  # noqa: E402
from config import load_config  # noqa: E402
from demo_data import demo_bundle, simulate_notifications  # noqa: E402
from tests.conftest import FAKE_KEY, VID_A, VID_B, Clock, FakeClient, RecordingNotifier  # noqa: E402

DASHBOARD = str(Path(__file__).resolve().parent.parent / "dashboard.py")


def run_app(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["dashboard.py", *args])
    at = AppTest.from_file(DASHBOARD, default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def labels(at):
    return [m.label for m in at.metric]


def choose(at, key, value):
    """ボタン群（segmented_control）を操作する。古い Streamlit のテスト機能にはないので省く。"""
    if not hasattr(at, "segmented_control"):
        return at
    at.segmented_control(key=key).set_value(value).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_demo_mode_renders_all_tabs(monkeypatch):
    at = run_app(monkeypatch, "--demo")
    assert "現在の視聴回数" in labels(at) and "過去24時間の増加数" in labels(at)
    assert len(at.tabs) == 4
    # 表示期間・時間帯の区切り・比較方法を切り替えても落ちない
    for period in ["6時間", "全期間", "7日"]:
        choose(at, "period", period)
    choose(at, "bucket", "15分")
    choose(at, "compare_mode", "1時間あたりの増加数")
    at.selectbox(key="detail_video").select_index(1).run()
    assert not at.exception


def make_real_data(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "api_key": FAKE_KEY,
        "videos": [{"url": VID_A, "name": "推しA"}, {"url": VID_B, "name": "推しB", "threshold": 50}],
        "notification": {"threshold": 500},
    }, ensure_ascii=False), encoding="utf-8")
    # 「今」の 3 時間前から取得を始めた想定（表示期間の絞り込みも本物と同じ条件で確かめる）
    clock = Clock(datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3))
    client = FakeClient(clock, {VID_A: 10_000, VID_B: 500}, step={VID_A: 60, VID_B: 2})
    svc = M.MonitorService(load_config(cfg_path), client=client, notifier=RecordingNotifier(),
                           clock=clock, sleep=lambda s: None)
    svc.startup()
    for _ in range(36):
        svc.run_cycle()
        clock.advance(minutes=5)
    return cfg_path


def test_real_data_mode(monkeypatch, tmp_path):
    cfg_path = make_real_data(tmp_path)
    at = run_app(monkeypatch, "--config", str(cfg_path))
    values = {m.label: m.value for m in at.metric}
    assert values["過去1時間の増加数"] == "+720回"
    assert values["現在の視聴回数"] == "12,160回"
    assert values["過去24時間の増加数"] == "データ不足"  # 3 時間分しかないので数字を出さない
    choose(at, "period", "全期間")


def test_without_any_data(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"videos": [VID_A]}), encoding="utf-8")
    at = run_app(monkeypatch, "--config", str(cfg_path))
    assert any("まだ視聴回数のデータがありません" in i.value for i in at.info)


def test_demo_bundle_uses_shared_logic():
    bundle = demo_bundle()
    assert set(bundle.history["video_id"]) == {"demoVideo01", "demoVideo02", "demoVideo03"}
    notes = simulate_notifications(bundle.history)
    assert not notes.empty and (notes["increase"] >= notes["threshold"]).all()


def test_exported_demo_files_open_with_data_dir(monkeypatch, tmp_path):
    from demo_data import write_demo_files

    write_demo_files(tmp_path / "demo")
    at = run_app(monkeypatch, "--data-dir", str(tmp_path / "demo"), "--config", str(tmp_path / "none.json"))
    assert "現在の視聴回数" in labels(at)
