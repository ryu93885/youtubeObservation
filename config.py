"""config.json の読み込みと検証。

- API キーは環境変数 ``YOUTUBE_API_KEY`` を優先し、なければ config.json の ``api_key`` を使う
- 誤りはまとめて日本語で報告する（どの項目をどう直せばよいかが分かるように）
- 未知の項目（つづりの誤りなど）は警告する。``_`` で始まる項目はメモとして無視する
- 相対パス（data_dir など）は config.json のあるフォルダーを基準に解決する
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from youtube_api import InvalidVideoReference, extract_video_id

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.json"
EXAMPLE_CONFIG_PATH = PROJECT_DIR / "config.example.json"
API_KEY_ENV = "YOUTUBE_API_KEY"
CONFIG_PATH_ENV = "YTMONITOR_CONFIG"
MIN_INTERVAL_SECONDS = 60
POWERSHELL_APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"

_PLACEHOLDER_KEYS = {"", "YOUR_API_KEY", "ここにAPIキー", "ここに API キー", "<API_KEY>"}
_PLACEHOLDER_VIDEO_IDS = {"XXXXXXXXXXX", "YYYYYYYYYYY", "ZZZZZZZZZZZ"}


class ConfigError(Exception):
    """設定ファイルの誤り（複数の誤りを改行区切りで持つ）。"""

    def __init__(self, problems: list[str] | str) -> None:
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("\n".join(self.problems))


@dataclass(frozen=True)
class NotificationRule:
    """1 本の動画に適用される通知条件。"""

    window_minutes: int = 60
    threshold: int = 1000
    cooldown_minutes: int = 60
    milestone_step: int = 0  # 0 なら節目通知なし（例: 100000 なら 10 万回ごと）


@dataclass(frozen=True)
class NotificationSettings:
    enabled: bool = True
    window_minutes: int = 60
    threshold: int = 1000
    cooldown_minutes: int = 60
    milestone_step: int = 0
    max_per_cycle: int = 3  # 1 回の取得でこれを超える件数の通知が出るときは 1 件にまとめる
    sound: bool = True
    app_id: str = "YouTubeViewMonitor"

    def default_rule(self) -> NotificationRule:
        return NotificationRule(self.window_minutes, self.threshold,
                                self.cooldown_minutes, self.milestone_step)


@dataclass(frozen=True)
class DashboardSettings:
    refresh_seconds: int = 60
    default_period_hours: int = 24
    url: str = "http://localhost:8501"


@dataclass(frozen=True)
class VideoConfig:
    video_id: str
    url: str
    name: str = ""
    enabled: bool = True
    notify: bool = True
    rule: NotificationRule = field(default_factory=NotificationRule)

    def display_name(self) -> str:
        return self.name or self.video_id


@dataclass(frozen=True)
class AppConfig:
    api_key: str = ""
    api_key_source: str = ""
    interval_seconds: int = 300
    timezone: str = "Asia/Tokyo"
    retention_days: int = 30
    daily_quota: int = 10000
    data_dir: Path = PROJECT_DIR / "data"
    log_dir: Path = PROJECT_DIR / "logs"
    notification: NotificationSettings = field(default_factory=NotificationSettings)
    dashboard: DashboardSettings = field(default_factory=DashboardSettings)
    videos: tuple[VideoConfig, ...] = ()
    warnings: tuple[str, ...] = ()
    path: Path | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def active_videos(self) -> tuple[VideoConfig, ...]:
        return tuple(v for v in self.videos if v.enabled)

    def video(self, video_id: str) -> VideoConfig | None:
        for v in self.videos:
            if v.video_id == video_id:
                return v
        return None

    def estimated_daily_quota(self) -> int:
        """監視間隔と動画数から 1 日の推定消費ユニット数を返す。"""
        requests_per_cycle = -(-len(self.active_videos) // 50) if self.active_videos else 0
        cycles_per_day = 86400 / self.interval_seconds
        return int(round(requests_per_cycle * cycles_per_day))


# ---------------------------------------------------------------------- 検証の部品
class _Checker:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def integer(self, data: dict, key: str, default: int, label: str, *, minimum: int = 0,
                path: str = "") -> int:
        name = f"{path}{key}"
        if key not in data:
            return default
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (
            isinstance(value, float) and not value.is_integer()
        ):
            hint = "（数字を \" \" で囲まないでください）" if isinstance(value, str) else ""
            self.errors.append(f"{name}（{label}）は整数で指定してください。現在の値: {value!r}{hint}")
            return default
        value = int(value)
        if value < minimum:
            self.errors.append(f"{name}（{label}）は {minimum} 以上にしてください。現在の値: {value}")
            return default
        return value

    def boolean(self, data: dict, key: str, default: bool, label: str, *, path: str = "") -> bool:
        if key not in data:
            return default
        value = data[key]
        if not isinstance(value, bool):
            self.errors.append(f"{path}{key}（{label}）は true か false で指定してください。現在の値: {value!r}")
            return default
        return value

    def string(self, data: dict, key: str, default: str, label: str, *, path: str = "") -> str:
        if key not in data:
            return default
        value = data[key]
        if value is None:
            return default
        if not isinstance(value, str):
            self.errors.append(f"{path}{key}（{label}）は文字列で指定してください。現在の値: {value!r}")
            return default
        return value.strip()

    def unknown(self, data: dict, allowed: set[str], path: str = "") -> None:
        for key in data:
            if key.startswith("_") or key in allowed:
                continue
            self.warnings.append(f"未知の設定項目「{path}{key}」は無視しました（つづりを確認してください）")


_TOP_KEYS = {"api_key", "interval_seconds", "timezone", "retention_days", "daily_quota",
             "data_dir", "log_dir", "notification", "dashboard", "videos"}
_NOTIFY_KEYS = {"enabled", "window_minutes", "threshold", "cooldown_minutes", "milestone_step",
                "max_per_cycle", "sound", "app_id"}
_DASH_KEYS = {"refresh_seconds", "default_period_hours", "url"}
_VIDEO_KEYS = {"url", "id", "name", "enabled", "notify", "window_minutes", "threshold",
               "cooldown_minutes", "milestone_step"}


def _resolve_path(value: str, base: Path) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(value)))
    return p if p.is_absolute() else (base / p).resolve()


def parse_config(data: Any, *, base_dir: Path = PROJECT_DIR, path: Path | None = None,
                 require_api_key: bool = True, require_videos: bool = True,
                 environ: dict[str, str] | None = None) -> AppConfig:
    """辞書（config.json の中身）を検証して AppConfig にする。"""
    env = os.environ if environ is None else environ
    c = _Checker()
    if not isinstance(data, dict):
        raise ConfigError("config.json の一番外側は { } で囲んだ形式にしてください")
    c.unknown(data, _TOP_KEYS)

    # --- API キー（環境変数を優先）
    env_key = (env.get(API_KEY_ENV) or "").strip()
    file_key = c.string(data, "api_key", "", "YouTube API キー")
    if env_key:
        api_key, source = env_key, f"環境変数 {API_KEY_ENV}"
    elif file_key and file_key not in _PLACEHOLDER_KEYS:
        api_key, source = file_key, "config.json"
    else:
        api_key, source = "", ""
    if require_api_key and not api_key:
        c.errors.append(
            "YouTube API キーが設定されていません。config.json の \"api_key\" に貼り付けるか、"
            f"環境変数 {API_KEY_ENV} に設定してください（取得方法は README を参照）"
        )
    elif api_key and not (api_key.startswith("AIza") and len(api_key) == 39):
        c.warnings.append("API キーの形式がふつうと異なります（通常は AIza で始まる 39 文字）。貼り付け間違いがないか確認してください")

    interval = c.integer(data, "interval_seconds", 300, "監視間隔（秒）", minimum=MIN_INTERVAL_SECONDS)
    tz_name = c.string(data, "timezone", "Asia/Tokyo", "表示に使うタイムゾーン") or "Asia/Tokyo"
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        c.errors.append(
            f"timezone「{tz_name}」が見つかりません。例: \"Asia/Tokyo\"。"
            "Windows では tzdata パッケージが必要です（pip install -r requirements.txt）"
        )
        tz_name = "UTC"
    retention = c.integer(data, "retention_days", 30, "履歴の保存日数（0 で無期限）", minimum=0)
    daily_quota = c.integer(data, "daily_quota", 10000, "1 日のクォータ上限", minimum=1)
    data_dir = _resolve_path(c.string(data, "data_dir", "data", "データの保存先") or "data", base_dir)
    log_dir = _resolve_path(c.string(data, "log_dir", "logs", "ログの保存先") or "logs", base_dir)

    # --- 通知
    raw_notify = data.get("notification", {})
    if not isinstance(raw_notify, dict):
        c.errors.append("notification は { } で囲んだ形式にしてください")
        raw_notify = {}
    c.unknown(raw_notify, _NOTIFY_KEYS, "notification.")
    np_ = "notification."
    app_id = c.string(raw_notify, "app_id", "YouTubeViewMonitor", "通知のアプリ ID", path=np_)
    if app_id.lower() == "powershell":
        app_id = POWERSHELL_APP_ID
    if "'" in app_id or not app_id:
        c.errors.append("notification.app_id に ' は使えません")
        app_id = "YouTubeViewMonitor"
    notification = NotificationSettings(
        enabled=c.boolean(raw_notify, "enabled", True, "通知を出すか", path=np_),
        window_minutes=c.integer(raw_notify, "window_minutes", 60, "判定対象期間（分）", minimum=5, path=np_),
        threshold=c.integer(raw_notify, "threshold", 1000, "通知閾値（回）", minimum=1, path=np_),
        cooldown_minutes=c.integer(raw_notify, "cooldown_minutes", 60, "通知クールダウン（分）", minimum=0, path=np_),
        milestone_step=c.integer(raw_notify, "milestone_step", 0, "節目通知の間隔（回）", minimum=0, path=np_),
        max_per_cycle=c.integer(raw_notify, "max_per_cycle", 3, "1 回にまとめずに出す通知の上限", minimum=1, path=np_),
        sound=c.boolean(raw_notify, "sound", True, "通知音", path=np_),
        app_id=app_id,
    )
    if notification.window_minutes * 60 < interval * 2:
        c.warnings.append(
            f"判定対象期間（{notification.window_minutes} 分）が監視間隔の 2 倍より短いため、増加数を計算できないことがあります"
        )

    # --- ダッシュボード
    raw_dash = data.get("dashboard", {})
    if not isinstance(raw_dash, dict):
        c.errors.append("dashboard は { } で囲んだ形式にしてください")
        raw_dash = {}
    c.unknown(raw_dash, _DASH_KEYS, "dashboard.")
    dashboard = DashboardSettings(
        refresh_seconds=c.integer(raw_dash, "refresh_seconds", 60, "自動更新の間隔（秒）", minimum=10, path="dashboard."),
        default_period_hours=c.integer(raw_dash, "default_period_hours", 24, "初期表示期間（時間）", minimum=1, path="dashboard."),
        url=c.string(raw_dash, "url", "http://localhost:8501", "ダッシュボードの URL", path="dashboard.") or "http://localhost:8501",
    )

    # --- 動画
    videos: list[VideoConfig] = []
    raw_videos = data.get("videos", [])
    if not isinstance(raw_videos, list):
        c.errors.append("videos は [ ] で囲んだ一覧にしてください")
        raw_videos = []
    seen: dict[str, int] = {}
    default_rule = notification.default_rule()
    for index, raw in enumerate(raw_videos, start=1):
        vp = f"videos[{index}]."
        if isinstance(raw, str):
            raw = {"url": raw}
        if not isinstance(raw, dict):
            c.errors.append(f"videos の {index} 番目は {{\"url\": \"...\", \"name\": \"...\"}} の形式にしてください")
            continue
        c.unknown(raw, _VIDEO_KEYS, vp)
        ref = raw.get("url", raw.get("id", ""))
        try:
            video_id = extract_video_id(ref)
        except InvalidVideoReference as exc:
            c.errors.append(f"videos の {index} 番目: {exc}")
            continue
        if video_id in _PLACEHOLDER_VIDEO_IDS:
            c.errors.append(
                f"videos の {index} 番目はひな形の仮の URL（{str(ref).strip()}）のままです。"
                "監視したい動画の URL に書き換えて、上書き保存してください"
            )
            continue
        if video_id in seen:
            c.warnings.append(f"videos の {index} 番目は {seen[video_id]} 番目と同じ動画（{video_id}）のため無視しました")
            continue
        seen[video_id] = index
        rule = NotificationRule(
            window_minutes=c.integer(raw, "window_minutes", default_rule.window_minutes, "判定対象期間（分）", minimum=5, path=vp),
            threshold=c.integer(raw, "threshold", default_rule.threshold, "通知閾値（回）", minimum=1, path=vp),
            cooldown_minutes=c.integer(raw, "cooldown_minutes", default_rule.cooldown_minutes, "通知クールダウン（分）", minimum=0, path=vp),
            milestone_step=c.integer(raw, "milestone_step", default_rule.milestone_step, "節目通知の間隔（回）", minimum=0, path=vp),
        )
        videos.append(VideoConfig(
            video_id=video_id,
            url=str(ref).strip(),
            name=c.string(raw, "name", "", "表示名", path=vp),
            enabled=c.boolean(raw, "enabled", True, "監視するか", path=vp),
            notify=c.boolean(raw, "notify", True, "通知するか", path=vp),
            rule=rule,
        ))
    video_errors = any(e.startswith("videos") for e in c.errors)
    if require_videos and not any(v.enabled for v in videos) and not video_errors:
        c.errors.append("監視する動画がありません。config.json の \"videos\" に動画の URL を追加してください")

    if c.errors:
        raise ConfigError(c.errors)

    config = AppConfig(
        api_key=api_key, api_key_source=source, interval_seconds=interval, timezone=tz_name,
        retention_days=retention, daily_quota=daily_quota, data_dir=data_dir, log_dir=log_dir,
        notification=notification, dashboard=dashboard, videos=tuple(videos),
        warnings=tuple(c.warnings), path=path,
    )
    if config.estimated_daily_quota() > daily_quota * 0.8:
        config = replace(config, warnings=config.warnings + (
            f"この設定では 1 日に約 {config.estimated_daily_quota():,} ユニットを使い、上限 {daily_quota:,} に近づきます。監視間隔を長くしてください",
        ))
    return config


def resolve_config_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    env_path = os.environ.get(CONFIG_PATH_ENV)
    if env_path:
        return Path(env_path).expanduser().resolve()
    return DEFAULT_CONFIG_PATH


def read_config_file(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig")  # メモ帳で保存した BOM 付き UTF-8 にも対応
    except FileNotFoundError:
        raise ConfigError(f"設定ファイルが見つかりません: {path}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"{path.name} を UTF-8 で読めません。メモ帳などで「UTF-8」を選んで保存し直してください") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        hint = ""
        if "Expecting ',' delimiter" in exc.msg or "Expecting property name" in exc.msg:
            hint = "（項目の間の , の付け忘れ・最後の項目の後ろの余計な , がないか確認してください）"
        raise ConfigError(
            f"{path.name} の {exc.lineno} 行目 {exc.colno} 文字目付近の書き方に誤りがあります: {exc.msg}{hint}"
        ) from None


def load_config(path: str | Path | None = None, *, require_api_key: bool = True,
                require_videos: bool = True) -> AppConfig:
    config_path = resolve_config_path(path)
    data = read_config_file(config_path)
    return parse_config(data, base_dir=config_path.parent, path=config_path,
                        require_api_key=require_api_key, require_videos=require_videos)


def load_config_for_dashboard(path: str | Path | None = None) -> tuple[AppConfig, list[str]]:
    """ダッシュボード用。API キーや動画がなくても、読めた範囲で設定を返す（失敗時は既定値）。"""
    config_path = resolve_config_path(path)
    messages: list[str] = []
    if not config_path.exists():
        messages.append(f"{config_path.name} がないため既定の設定で表示しています")
        return AppConfig(path=None), messages
    try:
        config = load_config(config_path, require_api_key=False, require_videos=False)
    except ConfigError as exc:
        messages.append("設定ファイルに誤りがあるため既定の設定で表示しています:\n" + str(exc))
        return AppConfig(data_dir=config_path.parent / "data", path=config_path), messages
    return config, messages


def ensure_config_file(path: Path) -> bool:
    """config.json がなければひな形からコピーする。コピーしたら True を返す。"""
    if path.exists():
        return False
    if not EXAMPLE_CONFIG_PATH.exists():
        raise ConfigError(f"設定ファイルのひな形 {EXAMPLE_CONFIG_PATH.name} が見つかりません")
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLE_CONFIG_PATH, path)
    return True
