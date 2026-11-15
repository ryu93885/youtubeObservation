"""通知（Windows トースト）のテスト。PowerShell は実行せず、渡す内容を検証する。"""
import base64
import re
import subprocess
import xml.etree.ElementTree as ET

import pytest

import notifier as N

EVIL_TITLE = "推し\"; Remove-Item C:\\ -Recurse; \"$(Stop-Computer) `whoami` ]]> <b>&amp;"


def test_toast_xml_escapes_everything():
    msg = N.Message(title=EVIL_TITLE, body="1行目\n2行目 <script>", url="https://www.youtube.com/watch?v=a&b=1",
                    buttons=(("動画を開く", "https://www.youtube.com/watch?v=x"), ("危険", "javascript:alert(1)")))
    xml = N.build_toast_xml(msg)
    root = ET.fromstring(xml)  # 正しい XML として読める
    texts = [t.text for t in root.iter("text")]
    assert texts[0] == EVIL_TITLE
    assert "<script>" in texts[1]
    assert root.get("launch") == "https://www.youtube.com/watch?v=a&b=1"
    actions = list(root.iter("action"))
    assert len(actions) == 1  # javascript: の URL は入れない


def test_script_contains_user_text_only_as_base64():
    xml = N.build_toast_xml(N.Message(title=EVIL_TITLE, body="本文"))
    script = N.build_powershell_script(xml, "YouTubeViewMonitor", tag="dQw4w9WgXcQ")
    assert "Remove-Item" not in script and "Stop-Computer" not in script and "推し" not in script
    encoded = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script).group(1)
    assert base64.b64decode(encoded).decode("utf-8") == xml
    assert "$toast.Tag = 'dQw4w9WgXcQ'" in script
    assert script.isascii() and "\n" not in script  # 標準入力から 1 行で渡せる


def test_unsafe_tag_and_app_id():
    script = N.build_powershell_script("<toast/>", "App", tag="x'; evil")
    assert "evil" not in script
    with pytest.raises(ValueError):
        N.build_powershell_script("<toast/>", "bad'id")


def test_long_and_control_text_is_cleaned():
    assert N.clean_text("a\x00b\x1fc") == "abc"
    assert len(N.clean_text("あ" * 1000, limit=50)) == 50


def test_create_notifier_by_platform():
    assert isinstance(N.create_notifier(platform="linux"), N.ConsoleNotifier)
    win = N.create_notifier("App", platform="win32")
    assert isinstance(win, N.FallbackNotifier) and isinstance(win.primary, N.WindowsToastNotifier)


def test_windows_toast_invocation(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(N.subprocess, "run", fake_run)
    toast = N.WindowsToastNotifier("App")
    toast._registered = True  # レジストリには触れない
    assert toast.send(N.Message(title=EVIL_TITLE, body="b", tag="dQw4w9WgXcQ"))
    cmd, kwargs = calls[0]
    assert cmd == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "-"]
    assert "-EncodedCommand" not in cmd
    assert b"Remove-Item" not in kwargs["input"]


def test_fallback_to_console_when_toast_fails(monkeypatch, caplog):
    monkeypatch.setattr(N.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"error"))
    toast = N.WindowsToastNotifier("App")
    toast._registered = True
    notifier = N.FallbackNotifier(toast, N.ConsoleNotifier())
    with caplog.at_level("INFO"):
        assert notifier.send(N.Message(title="T", body="B")) is False
    assert "【通知】T" in caplog.text


def test_missing_powershell_is_handled(monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError("powershell.exe")

    monkeypatch.setattr(N.subprocess, "run", boom)
    toast = N.WindowsToastNotifier("App")
    toast._registered = True
    assert toast.send(N.Message(title="T", body="B")) is False
