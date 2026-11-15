"""設定ファイルの読み込み・検証のテスト。"""
import json

import pytest

import config as C
from tests.conftest import FAKE_KEY, VID_A, VID_B


def parse(data, tmp_path, **kwargs):
    return C.parse_config(data, base_dir=tmp_path, environ=kwargs.pop("environ", {}), **kwargs)


def base(**overrides):
    data = {"api_key": FAKE_KEY, "videos": [{"url": f"https://youtu.be/{VID_A}", "name": "推し"}]}
    data.update(overrides)
    return data


def test_example_config_has_no_key_and_uses_documented_defaults():
    data = json.loads(C.EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
    assert data["api_key"] == ""  # 誤って API キー入りのひな形をコミットしないため
    assert data["interval_seconds"] == 300
    assert data["notification"]["window_minutes"] == 60
    assert data["notification"]["threshold"] == 1000


def test_example_config_placeholders_are_reported(tmp_path):
    data = json.loads(C.EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
    data["api_key"] = FAKE_KEY
    with pytest.raises(C.ConfigError) as exc:
        parse(data, tmp_path)
    assert any("ひな形" in p for p in exc.value.problems)


def test_defaults(tmp_path):
    cfg = parse(base(), tmp_path)
    assert cfg.interval_seconds == 300
    assert cfg.notification.window_minutes == 60
    assert cfg.notification.threshold == 1000
    assert cfg.notification.cooldown_minutes == 60
    assert cfg.retention_days == 30
    assert cfg.timezone == "Asia/Tokyo"
    assert cfg.data_dir == (tmp_path / "data").resolve()
    v = cfg.videos[0]
    assert (v.video_id, v.name, v.rule.threshold) == (VID_A, "推し", 1000)
    assert cfg.estimated_daily_quota() == 288


def test_env_var_takes_precedence(tmp_path):
    env_key = "AIza" + "y" * 35
    cfg = parse(base(), tmp_path, environ={C.API_KEY_ENV: env_key})
    assert cfg.api_key == env_key and "環境変数" in cfg.api_key_source
    cfg = parse(base(api_key=""), tmp_path, environ={C.API_KEY_ENV: env_key})
    assert cfg.api_key == env_key


def test_missing_key_is_error_only_when_required(tmp_path):
    with pytest.raises(C.ConfigError) as exc:
        parse(base(api_key="YOUR_API_KEY"), tmp_path)
    assert "API キー" in str(exc.value)
    cfg = parse(base(api_key=""), tmp_path, require_api_key=False)
    assert cfg.api_key == ""


def test_per_video_overrides(tmp_path):
    data = base(notification={"threshold": 2000, "milestone_step": 100000},
                videos=[f"https://youtu.be/{VID_A}",
                        {"url": VID_B, "name": "B", "threshold": 300, "window_minutes": 30,
                         "notify": False, "enabled": True}])
    cfg = parse(data, tmp_path)
    a, b = cfg.videos
    assert a.rule.threshold == 2000 and a.rule.milestone_step == 100000 and a.name == ""
    assert a.display_name() == VID_A
    assert (b.rule.threshold, b.rule.window_minutes, b.notify) == (300, 30, False)


def test_errors_are_collected_and_explained(tmp_path):
    data = base(interval_seconds="300", notification={"threshold": True, "window_minutes": 1.5},
                timezone="Mars/Base",
                videos=[{"url": "https://www.youtube.com/@channel"}, {"url": "abc"}])
    with pytest.raises(C.ConfigError) as exc:
        parse(data, tmp_path)
    text = str(exc.value)
    assert "interval_seconds" in text and "\" \" で囲まないで" in text
    assert "notification.threshold" in text and "notification.window_minutes" in text
    assert "Mars/Base" in text
    assert "チャンネル" in text
    assert len(exc.value.problems) >= 6


def test_interval_minimum(tmp_path):
    with pytest.raises(C.ConfigError):
        parse(base(interval_seconds=10), tmp_path)


def test_warnings_for_unknown_keys_duplicates_and_quota(tmp_path):
    data = base(treshold=5, _memo="無視される",
                videos=[VID_A, f"https://www.youtube.com/watch?v={VID_A}"],
                interval_seconds=60, daily_quota=1000)
    cfg = parse(data, tmp_path)
    joined = "\n".join(cfg.warnings)
    assert "treshold" in joined and "_memo" not in joined
    assert "同じ動画" in joined
    assert "上限" in joined
    assert len(cfg.videos) == 1


def test_no_videos(tmp_path):
    with pytest.raises(C.ConfigError):
        parse(base(videos=[]), tmp_path)
    assert parse(base(videos=[]), tmp_path, require_videos=False).videos == ()


def test_powershell_app_id_alias(tmp_path):
    cfg = parse(base(notification={"app_id": "powershell"}), tmp_path)
    assert cfg.notification.app_id == C.POWERSHELL_APP_ID


def test_load_config_file_with_bom_and_relative_paths(tmp_path):
    p = tmp_path / "sub" / "config.json"
    p.parent.mkdir()
    p.write_text(json.dumps(base(data_dir="mydata")), encoding="utf-8-sig")
    cfg = C.load_config(p)
    assert cfg.data_dir == (p.parent / "mydata").resolve()
    assert cfg.path == p


def test_json_syntax_error_points_to_line(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{\n  "api_key": "x"\n  "videos": []\n}', encoding="utf-8")
    with pytest.raises(C.ConfigError) as exc:
        C.load_config(p)
    assert "3 行目" in str(exc.value) and ", の付け忘れ" in str(exc.value)


def test_ensure_config_file_copies_example(tmp_path):
    p = tmp_path / "config.json"
    assert C.ensure_config_file(p) is True
    assert json.loads(p.read_text(encoding="utf-8"))["api_key"] == ""
    assert C.ensure_config_file(p) is False


def test_dashboard_loader_never_fails(tmp_path):
    cfg, messages = C.load_config_for_dashboard(tmp_path / "none.json")
    assert cfg.interval_seconds == 300 and messages
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    cfg, messages = C.load_config_for_dashboard(bad)
    assert "誤り" in messages[0]
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"videos": [VID_A]}), encoding="utf-8")
    cfg, messages = C.load_config_for_dashboard(good)
    assert cfg.videos[0].video_id == VID_A and not messages
