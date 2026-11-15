"""Windows のデスクトップ通知（トースト通知）。

外部ライブラリ（winotify など）は使わず、Windows 標準の PowerShell 5.1 から
Windows の通知 API を呼び出す。

安全のための工夫:
    通知の文面には他人の動画のタイトル（外部から来る文字列）が入る。
    winotify などは文面をそのまま PowerShell のスクリプトに埋め込むため、
    タイトルに " や $( ) が含まれると任意のコマンドが実行されうる。
    ここでは文面を XML として正しくエスケープしたうえで Base64 に変換して埋め込むので、
    スクリプトに入る外部の文字列は英数字と + / = だけになり、コードとして解釈されることはない。

Windows 以外（テスト環境など）や通知に失敗したときは、コンソールとログに出力する。
"""
from __future__ import annotations

import base64
import locale
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from xml.sax.saxutils import escape, quoteattr

logger = logging.getLogger(__name__)

APP_DISPLAY_NAME = "YouTube View Monitor"
_SAFE_TAG = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    url: str = ""          # 通知をクリックしたときに開く URL
    tag: str = ""          # 同じタグの通知は置き換わる（動画 ID を使う）
    buttons: tuple[tuple[str, str], ...] = ()  # (ボタンの文字, 開く URL)


def clean_text(text: str, limit: int = 300) -> str:
    """XML に入れられない制御文字を除き、長すぎる文字列を切り詰める。"""
    text = _CONTROL_CHARS.sub("", str(text))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _is_safe_url(url: str) -> bool:
    return url.startswith(("https://", "http://localhost", "http://127.0.0.1"))


def build_toast_xml(message: Message, *, sound: bool = True) -> str:
    """トースト通知の XML を作る（文字列はすべてエスケープする）。"""
    launch = ""
    if message.url and _is_safe_url(message.url):
        launch = f" activationType=\"protocol\" launch={quoteattr(message.url)}"
    actions = []
    for label, url in message.buttons[:5]:
        if _is_safe_url(url):
            actions.append(
                f"<action activationType=\"protocol\" content={quoteattr(clean_text(label, 40))} "
                f"arguments={quoteattr(url)} />"
            )
    audio = '<audio src="ms-winsoundevent:Notification.Default" />' if sound else '<audio silent="true" />'
    return (
        f"<toast{launch} duration=\"short\">"
        "<visual><binding template=\"ToastGeneric\">"
        f"<text>{escape(clean_text(message.title, 120))}</text>"
        f"<text>{escape(clean_text(message.body, 400))}</text>"
        f"<text placement=\"attribution\">{escape(APP_DISPLAY_NAME)}</text>"
        "</binding></visual>"
        + (f"<actions>{''.join(actions)}</actions>" if actions else "")
        + audio
        + "</toast>"
    )


def build_powershell_script(xml: str, app_id: str, tag: str = "") -> str:
    """トーストを表示する PowerShell スクリプト（1 行）。外部の文字列は Base64 でだけ埋め込む。

    標準入力から 1 行のコマンドとして渡すため、文は ; でつなぎ、失敗したら終了コード 1 を返す。
    """
    if "'" in app_id:
        raise ValueError("app_id に ' は使えません")
    xml_b64 = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    statements = [
        "$ErrorActionPreference = 'Stop'",
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null",
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null",
        f"$xmlText = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{xml_b64}'))",
        "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument",
        "$doc.LoadXml($xmlText)",
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc)",
    ]
    if tag and _SAFE_TAG.fullmatch(tag):
        statements.append(f"$toast.Tag = '{tag}'")
    statements += [
        "$toast.Group = 'monitor'",
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_id}').Show($toast)",
    ]
    body = "; ".join(statements)
    return f"try {{ {body} }} catch {{ [Console]::Error.WriteLine($_.Exception.Message); exit 1 }}; exit 0"


class Notifier:
    """通知の送信先の共通インターフェース。"""

    name = "base"

    def send(self, message: Message) -> bool:  # pragma: no cover - インターフェース
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    name = "console"

    def send(self, message: Message) -> bool:
        logger.info("【通知】%s / %s %s", message.title, message.body, message.url)
        return True


class WindowsToastNotifier(Notifier):
    name = "windows-toast"

    def __init__(self, app_id: str = "YouTubeViewMonitor", *, sound: bool = True,
                 timeout: float = 30.0) -> None:
        self.app_id = app_id
        self.sound = sound
        self.timeout = timeout
        self._registered = False

    def _register_app_id(self) -> None:
        """通知元の名前を Windows に登録する（HKEY_CURRENT_USER のみ・管理者権限は不要）。"""
        if self._registered or self.app_id.startswith("{"):
            self._registered = True
            return
        try:
            import winreg

            key_path = rf"Software\Classes\AppUserModelId\{self.app_id}"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_DISPLAY_NAME)
        except OSError as exc:  # 登録できなくても通知自体は試す
            logger.debug("通知元の登録に失敗しました: %s", exc)
        self._registered = True

    def send(self, message: Message) -> bool:
        self._register_app_id()
        script = build_powershell_script(build_toast_xml(message, sound=self.sound),
                                         self.app_id, message.tag)
        # スクリプトは標準入力から渡す（コマンドラインに長い Base64 を載せる
        # -EncodedCommand は、セキュリティソフトに不審な動作と判定されやすいため使わない）
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "-"]
        try:
            proc = subprocess.run(
                cmd, input=(script + "\n").encode("ascii"), capture_output=True,
                timeout=self.timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Windows 通知を表示できませんでした: %s", exc)
            return False
        if proc.returncode != 0:
            err = proc.stderr.decode(locale.getpreferredencoding(False), errors="replace").strip()
            logger.warning("Windows 通知を表示できませんでした（PowerShell のエラー）: %s", err[-500:])
            return False
        return True


class FallbackNotifier(Notifier):
    """先頭の通知手段が失敗したら、次の手段（コンソール）で知らせる。"""

    def __init__(self, primary: Notifier, fallback: Notifier) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = primary.name

    def send(self, message: Message) -> bool:
        if self.primary.send(message):
            ConsoleNotifier().send(message)  # 記録のためログにも残す
            return True
        self.fallback.send(message)
        return False


_WATCH_URL = re.compile(r"^https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}$")


def play_video(url: str) -> bool:
    """既定のブラウザで YouTube の動画を開く（YouTube の動画ページは開くと再生が始まる）。

    安全のため、https://www.youtube.com/watch?v=（11 文字の動画 ID）の形の URL だけを開く。
    ブラウザの自動再生の設定によっては、音声付きで自動再生されないことがある。
    """
    if not _WATCH_URL.fullmatch(url or ""):
        logger.warning("YouTube の動画 URL ではないため開きません: %s", url)
        return False
    import webbrowser

    try:
        opened = webbrowser.open(url, new=2)  # 新しいタブで開く
    except Exception as exc:  # ブラウザが見つからない環境など
        logger.warning("ブラウザで動画を開けませんでした: %s", exc)
        return False
    if not opened:
        logger.warning("ブラウザで動画を開けませんでした（既定のブラウザが見つかりません）")
    return bool(opened)


def create_notifier(app_id: str = "YouTubeViewMonitor", *, sound: bool = True,
                    platform: str | None = None) -> Notifier:
    platform = platform or sys.platform
    if platform == "win32":
        return FallbackNotifier(WindowsToastNotifier(app_id, sound=sound), ConsoleNotifier())
    return ConsoleNotifier()
