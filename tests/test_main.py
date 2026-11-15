"""main.py（監視プログラムの起動）のテスト。"""
import json
import logging

import pytest

import main as main_module
import monitor
from data_manager import read_history_csv
from instance_lock import InstanceLock
from tests.conftest import FAKE_KEY, VID_A, Clock, FakeClient
from youtube_api import AuthError


@pytest.fixture(autouse=True)
def restore_logging():
    yield
    logging.getLogger().handlers.clear()


def write_config(tmp_path, **overrides):
    data = {"api_key": FAKE_KEY, "videos": [{"url": f"https://youtu.be/{VID_A}", "name": "推し"}]}
    data.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_first_run_creates_config_from_example(tmp_path, capsys):
    path = tmp_path / "config.json"
    assert main_module.main(["--config", str(path)]) == 1
    assert path.exists()
    assert "作成しました" in capsys.readouterr().out


def test_invalid_config_is_explained(tmp_path, capsys):
    path = write_config(tmp_path, api_key="")
    assert main_module.main(["--config", str(path)]) == 1
    assert "API キー" in capsys.readouterr().out


def test_once_fetches_and_saves(tmp_path, monkeypatch):
    clock = Clock()
    fake = FakeClient(clock, {VID_A: 100}, step={VID_A: 5})
    monkeypatch.setattr(monitor, "YouTubeClient", lambda key: fake)
    path = write_config(tmp_path)
    assert main_module.main(["--config", str(path), "--once"]) == 0
    df, _ = read_history_csv(tmp_path / "data" / "history.csv")
    assert list(df["view_count"]) == [105]
    status = json.loads((tmp_path / "data" / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "stopped"
    assert (tmp_path / "logs" / "monitor.log").exists()


def test_second_instance_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "YouTubeClient", lambda key: pytest.fail("API を呼んではいけない"))
    path = write_config(tmp_path)
    with InstanceLock(tmp_path / "data" / "monitor.lock"):
        assert main_module.main(["--config", str(path), "--once"]) == 1


def test_check_reports_videos(tmp_path, monkeypatch, capsys):
    clock = Clock()
    fake = FakeClient(clock, {VID_A: 1234})

    class FakeYouTube:
        def __init__(self, key, **kwargs):
            pass

        def fetch_videos(self, ids):
            return fake.fetch_videos(ids)

    monkeypatch.setattr(main_module, "YouTubeClient", FakeYouTube)
    path = write_config(tmp_path)
    assert main_module.main(["--config", str(path), "--check"]) == 0
    out = capsys.readouterr().out
    assert "OK 推し: 1,234回" in out and FAKE_KEY not in out


def test_check_explains_bad_key(tmp_path, monkeypatch, capsys):
    class BadKey:
        def __init__(self, key, **kwargs):
            pass

        def fetch_videos(self, ids):
            raise AuthError("bad", status=400, reason="API_KEY_INVALID")

    monkeypatch.setattr(main_module, "YouTubeClient", BadKey)
    path = write_config(tmp_path)
    assert main_module.main(["--config", str(path), "--check"]) == 1
    assert "API キーが正しくありません" in capsys.readouterr().out


def test_test_notify_works_without_api_key(tmp_path):
    path = write_config(tmp_path, api_key="", videos=[])
    assert main_module.main(["--config", str(path), "--test-notify"]) == 0


def test_log_formatter_hides_api_key():
    fmt = main_module.RedactingFormatter("%(message)s", [FAKE_KEY])
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, f"url?key={FAKE_KEY}", None, None)
    assert FAKE_KEY not in fmt.format(record) and "***" in fmt.format(record)


def test_config_error_shows_file_path_and_txt_twin(tmp_path, capsys):
    path = write_config(tmp_path, videos=[{"url": "https://youtu.be/YYYYYYYYYYY", "name": "仮"}])
    (tmp_path / "config.json.txt").write_text("{}", encoding="utf-8")
    (tmp_path / "config.example.json").write_text("{}", encoding="utf-8")
    assert main_module.main(["--config", str(path)]) == 1
    out = capsys.readouterr().out
    assert "https://youtu.be/YYYYYYYYYYY" in out          # どの URL が残っているか
    assert f"読み込んだ設定ファイル: {path}" in out          # どのファイルを読んだか
    assert "config.json.txt" in out                      # メモ帳の .txt 付き保存の可能性
    assert "config.example.json（ひな形）を書き換えても読み込まれません" in out
