"""通知条件の判定と、定期的な取得（監視ループ）。

通知の種類:
    surge      直近 window_minutes 分の増加数が threshold 以上（増加数の計算は analytics と共通）
    milestone  視聴回数が milestone_step の倍数（例: 10 万回ごと）を超えた

通知の連発を防ぐしくみ:
    - 同じ動画の surge 通知は cooldown_minutes 分あける
    - 同じ節目（例: 100 万回）の通知は 1 回だけ
    - 1 回の取得で max_per_cycle 件を超えるときは 1 件のまとめ通知にする
    - 送った通知は notifications.csv に記録し、再起動後もクールダウンを引き継ぐ
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

import analytics
from config import AppConfig, ConfigError, VideoConfig, load_config
from data_manager import (
    NOTIFICATION_COLUMNS,
    CsvAppender,
    DataPaths,
    HistoryStore,
    VideoCatalog,
    format_timestamp,
    prune_csv_before,
    read_json,
    read_notifications_csv,
    utc_now,
    write_json_atomic,
)
from notifier import Message, Notifier, create_notifier
from youtube_api import (
    AuthError,
    FetchResult,
    QuotaExceededError,
    YouTubeAPIError,
    YouTubeClient,
    describe_auth_error,
    video_url,
)

logger = logging.getLogger(__name__)

APP_VERSION = "1.0.0"
PACIFIC = ZoneInfo("America/Los_Angeles")  # YouTube API のクォータは太平洋時間の 0 時に回復する


# ---------------------------------------------------------------------- 通知の判定
@dataclass(frozen=True)
class Alert:
    kind: str  # "surge" | "milestone"
    video_id: str
    name: str
    timestamp: pd.Timestamp
    view_count: int
    increase: int | None = None
    window_minutes: int | None = None
    threshold: int | None = None
    milestone: int | None = None
    span_minutes: float | None = None  # 増加数を測った実際の時間差

    def _span_note(self) -> str:
        if self.span_minutes is None or self.window_minutes is None:
            return ""
        if abs(self.span_minutes - self.window_minutes) < 1:
            return ""
        return f"（実測{int(round(self.span_minutes))}分）"

    def message(self, dashboard_url: str = "") -> Message:
        buttons = [("動画を開く", video_url(self.video_id))]
        if dashboard_url:
            buttons.append(("ダッシュボード", dashboard_url))
        if self.kind == "milestone":
            title = f"🎉 {self.milestone:,}回 突破: {self.name}"
            body = f"現在 {self.view_count:,}回"
        else:
            title = f"📈 再生数が伸びています: {self.name}"
            body = (f"直近{_minutes_label(self.window_minutes)}で +{self.increase:,}回{self._span_note()}"
                    f"（閾値 {self.threshold:,}回）\n現在 {self.view_count:,}回")
        return Message(title=title, body=body, url=video_url(self.video_id),
                       tag=self.video_id, buttons=tuple(buttons))

    def summary(self) -> str:
        if self.kind == "milestone":
            return f"{self.name}: {self.milestone:,}回突破"
        return f"{self.name}: +{self.increase:,}回/{_minutes_label(self.window_minutes)}"

    def record(self, delivered: bool, message: str) -> dict:
        return {
            "timestamp": format_timestamp(self.timestamp.to_pydatetime()),
            "video_id": self.video_id,
            "kind": self.kind,
            "window_minutes": self.window_minutes,
            "increase": self.increase,
            "threshold": self.threshold,
            "view_count": self.view_count,
            "milestone": self.milestone,
            "delivered": str(bool(delivered)).lower(),
            "message": message.replace("\n", " "),
        }


def _minutes_label(minutes: int | None) -> str:
    if minutes is None:
        return "-"
    if minutes % 60 == 0:
        return f"{minutes // 60}時間"
    return f"{minutes}分"


class NotificationLedger:
    """通知の履歴（クールダウンと節目の通知済みを判定するため）。"""

    def __init__(self, records: pd.DataFrame | None = None) -> None:
        self.last_surge: dict[str, pd.Timestamp] = {}
        self.milestones: dict[str, set[int]] = {}
        if records is not None and not records.empty:
            for row in records.itertuples(index=False):
                self._remember(row.kind, row.video_id, row.timestamp, row.milestone)

    def _remember(self, kind: str, video_id: str, ts: pd.Timestamp, milestone) -> None:
        if kind == "surge":
            prev = self.last_surge.get(video_id)
            if prev is None or ts > prev:
                self.last_surge[video_id] = ts
        elif kind == "milestone" and milestone is not None and not pd.isna(milestone):
            self.milestones.setdefault(video_id, set()).add(int(milestone))

    def remember(self, alert: Alert) -> None:
        self._remember(alert.kind, alert.video_id, alert.timestamp, alert.milestone)

    def in_cooldown(self, video_id: str, now: pd.Timestamp, cooldown: timedelta) -> bool:
        last = self.last_surge.get(video_id)
        return last is not None and now - last < cooldown


def evaluate_surge(series: pd.DataFrame, video: VideoConfig, ledger: NotificationLedger, *,
                   interval_seconds: float) -> Alert | None:
    """直近の増加数が閾値以上で、クールダウン中でなければ通知を返す。"""
    if not video.notify or series.empty:
        return None
    rule = video.rule
    inc = analytics.increase_over(series, pd.Timedelta(minutes=rule.window_minutes),
                                  interval=interval_seconds)
    if not inc.ok or inc.increase is None or inc.increase < rule.threshold:
        return None
    now = inc.end_time
    if ledger.in_cooldown(video.video_id, now, timedelta(minutes=rule.cooldown_minutes)):
        return None
    return Alert("surge", video.video_id, video.display_name(), now, int(inc.end_views),
                 increase=inc.increase, window_minutes=rule.window_minutes, threshold=rule.threshold,
                 span_minutes=inc.span.total_seconds() / 60)


def evaluate_milestone(series: pd.DataFrame, video: VideoConfig,
                       ledger: NotificationLedger) -> Alert | None:
    """前回の取得から今回までの間に節目（milestone_step の倍数）を超えたら通知を返す。"""
    step = video.rule.milestone_step
    if not video.notify or step <= 0 or len(series) < 2:
        return None
    prev, cur = int(series["view_count"].iloc[-2]), int(series["view_count"].iloc[-1])
    if cur // step <= prev // step:
        return None
    milestone = (cur // step) * step
    if milestone <= 0 or milestone in ledger.milestones.get(video.video_id, set()):
        return None
    return Alert("milestone", video.video_id, video.display_name(), series["timestamp"].iloc[-1],
                 cur, milestone=milestone)


def plan_messages(alerts: list[Alert], max_per_cycle: int, dashboard_url: str = "") -> list[Message]:
    """通知が多すぎるときは 1 件にまとめる。"""
    if not alerts:
        return []
    if len(alerts) <= max_per_cycle:
        return [a.message(dashboard_url) for a in alerts]
    lines = [a.summary() for a in alerts[:5]]
    if len(alerts) > 5:
        lines.append(f"ほか {len(alerts) - 5} 件")
    buttons = (("ダッシュボード", dashboard_url),) if dashboard_url else ()
    return [Message(title=f"📈 {len(alerts)} 件の通知があります", body="\n".join(lines),
                    url=dashboard_url, tag="summary", buttons=buttons)]


# ---------------------------------------------------------------------- 監視ループ
@dataclass
class CycleResult:
    fetched_at: datetime | None = None
    fetched: int = 0
    missing: list[str] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    error: str = ""
    quota_exceeded: bool = False


def next_aligned_time(now: datetime, interval_seconds: int) -> datetime:
    """次の取得時刻。時計の切りのよい時刻（5 分間隔なら 0 分, 5 分, 10 分…）にそろえる。"""
    epoch = now.timestamp()
    nxt = (epoch // interval_seconds + 1) * interval_seconds
    if nxt - epoch < interval_seconds / 2:  # 直前に取得したばかりなら 1 つ先にする
        nxt += interval_seconds
    return datetime.fromtimestamp(nxt, tz=timezone.utc)


def next_quota_reset(now: datetime) -> datetime:
    """クォータが回復する時刻（太平洋時間の翌 0 時 + 5 分）。"""
    local = now.astimezone(PACIFIC)
    tomorrow = (local + timedelta(days=1)).date()
    reset = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 0, 5, tzinfo=PACIFIC)
    return reset.astimezone(timezone.utc)


class MonitorService:
    def __init__(
        self,
        config: AppConfig,
        *,
        client: YouTubeClient | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
        config_loader: Callable[[Path], AppConfig] | None = None,
    ) -> None:
        self.config = config
        self.paths = DataPaths(config.data_dir)
        self.client = client or YouTubeClient(config.api_key)
        self._client_injected = client is not None
        self.notifier = notifier or create_notifier(config.notification.app_id,
                                                    sound=config.notification.sound)
        self._notifier_injected = notifier is not None
        self.clock = clock
        self.sleep = sleep
        self.config_loader = config_loader or (lambda p: load_config(p))
        self.history_store = HistoryStore(self.paths.history)
        self.notification_store = CsvAppender(self.paths.notifications, NOTIFICATION_COLUMNS)
        self.catalog = VideoCatalog.load(self.paths.videos)
        self.ledger = NotificationLedger()
        self.history = pd.DataFrame()
        self.started_at = clock()
        self.status: dict = {}
        self._config_mtime = self._read_config_mtime()
        self._last_prune_date: str = ""
        self._last_missing: set[str] = set()
        self._catalog_saved_at: datetime | None = None
        self._consecutive_failures = 0

    # ------------------------------------------------------------ 起動時の準備
    def startup(self) -> None:
        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        self.history_store.check_format()
        self.notification_store.check_format()
        self._prune_if_needed(force=True)
        since = self.clock() - self._memory_window()
        self.history, report = self.history_store.load(since=since)
        for msg in report.messages():
            logger.warning("history.csv: %s", msg)
        self.ledger = NotificationLedger(read_notifications_csv(self.paths.notifications))
        previous = read_json(self.paths.status, {}) or {}
        quota = previous.get("quota") or {}
        today = self._quota_day(self.clock())
        self.status = {
            "pid": os.getpid(),
            "version": APP_VERSION,
            "started_at": format_timestamp(self.started_at),
            "interval_seconds": self.config.interval_seconds,
            "state": "starting",
            "quota": {
                "date_pt": today,
                "used": int(quota.get("used", 0)) if quota.get("date_pt") == today else 0,
                "limit": self.config.daily_quota,
            },
        }
        logger.info("過去の履歴を %s 行読み込みました（判定に使う直近 %s 分）",
                    f"{len(self.history):,}", int(self._memory_window().total_seconds() // 60))
        self._write_status()

    def _memory_window(self) -> timedelta:
        longest = max([v.rule.window_minutes for v in self.config.videos] or [60])
        window = pd.Timedelta(minutes=longest)
        tol = analytics.tolerance_for(window, self.config.interval_seconds)
        return (window + tol + pd.Timedelta(minutes=30)).to_pytimedelta()

    # ------------------------------------------------------------ 1 回分の処理
    def run_cycle(self) -> CycleResult:
        result = CycleResult()
        self._reload_config_if_changed()
        self._prune_if_needed()
        videos = list(self.config.active_videos)
        now = self.clock()
        self.status["last_attempt_at"] = format_timestamp(now)
        if not videos:
            result.error = "監視する動画がありません"
            logger.warning(result.error)
            self._set_state("idle", result.error)
            return result
        try:
            fetch = self.client.fetch_videos([v.video_id for v in videos])
        except QuotaExceededError as exc:
            self._add_quota(getattr(exc, "quota_used", 1))
            result.quota_exceeded = True
            result.error = "1 日のクォータを使い切りました。太平洋時間の 0 時（日本時間 17 時、夏時間は 16 時）に回復します"
            logger.error(result.error)
            self._set_state("waiting_quota", result.error)
            return result
        except AuthError as exc:
            self._add_quota(getattr(exc, "quota_used", 0))
            result.error = f"API キーのエラー: {exc} → {describe_auth_error(exc)}"
            logger.error(result.error)
            self._fail(result.error)
            return result
        except YouTubeAPIError as exc:
            self._add_quota(getattr(exc, "quota_used", 0))
            result.error = f"視聴回数を取得できませんでした（次の回に再試行します）: {exc}"
            logger.error(result.error)
            self._fail(result.error)
            return result

        self._add_quota(fetch.quota_used)
        self._consecutive_failures = 0
        result.fetched_at = fetch.fetched_at
        result.missing = fetch.missing
        self._report_missing(fetch, videos)
        samples = [
            {"video_id": s.video_id, "view_count": s.view_count,
             "like_count": s.like_count, "comment_count": s.comment_count}
            for s in fetch.stats.values() if s.view_count is not None
        ]
        result.fetched = len(samples)
        self.history_store.append_samples(fetch.fetched_at, samples)
        self._remember_samples(fetch.fetched_at, samples)
        self._update_catalog(fetch)

        result.alerts = self._evaluate(videos, fetch)
        self._deliver(result.alerts)
        self._log_summary(videos, fetch)
        self.status["last_success_at"] = format_timestamp(fetch.fetched_at)
        self.status["videos"] = {
            s["video_id"]: {"view_count": s["view_count"], "fetched_at": format_timestamp(fetch.fetched_at)}
            for s in samples
        }
        self.status["missing_videos"] = fetch.missing
        self._set_state("running", "")
        return result

    def _remember_samples(self, ts: datetime, samples: list[dict]) -> None:
        if not samples:
            return
        new = pd.DataFrame({
            "timestamp": pd.Series([pd.Timestamp(ts)] * len(samples)).dt.tz_convert("UTC").dt.as_unit("ns"),
            "video_id": [s["video_id"] for s in samples],
            "view_count": [int(s["view_count"]) for s in samples],
        })
        frames = [f for f in (self.history, new) if not f.empty]
        merged = pd.concat(frames, ignore_index=True) if len(frames) > 1 else new
        merged = merged.drop_duplicates(subset=["video_id", "timestamp"], keep="last")
        cutoff = pd.Timestamp(ts) - pd.Timedelta(self._memory_window())
        self.history = merged[merged["timestamp"] >= cutoff].sort_values(
            ["video_id", "timestamp"]).reset_index(drop=True)

    def _evaluate(self, videos: list[VideoConfig], fetch: FetchResult) -> list[Alert]:
        if not self.config.notification.enabled:
            return []
        fetched_ts = pd.Timestamp(fetch.fetched_at).tz_convert("UTC")
        alerts: list[Alert] = []
        for video in videos:
            series = analytics.video_series(self.history, video.video_id)
            # 今回取得できなかった動画は、古いデータで判定しない
            if series.empty or series["timestamp"].iloc[-1] != fetched_ts:
                continue
            for alert in (
                evaluate_milestone(series, video, self.ledger),
                evaluate_surge(series, video, self.ledger, interval_seconds=self.config.interval_seconds),
            ):
                if alert is not None:
                    alerts.append(alert)
        return alerts

    def _deliver(self, alerts: list[Alert]) -> None:
        if not alerts:
            return
        messages = plan_messages(alerts, self.config.notification.max_per_cycle,
                                 self.config.dashboard.url)
        delivered = True
        for message in messages:
            delivered = self.notifier.send(message) and delivered
        text = messages[0].title if len(messages) == 1 else ""
        records = []
        for alert in alerts:
            self.ledger.remember(alert)
            records.append(alert.record(delivered, text or alert.message().title))
        self.notification_store.append(records)

    def _update_catalog(self, fetch: FetchResult) -> None:
        info = {
            s.video_id: {"title": s.title, "channel_title": s.channel_title, "channel_id": s.channel_id,
                         "published_at": s.published_at, "live_status": s.live_status}
            for s in fetch.stats.values()
        }
        changed = self.catalog.update(info, fetch.fetched_at)
        stale = (self._catalog_saved_at is None
                 or fetch.fetched_at - self._catalog_saved_at > timedelta(hours=1))
        if changed or stale:
            if self.catalog.save():
                self._catalog_saved_at = fetch.fetched_at

    def _report_missing(self, fetch: FetchResult, videos: list[VideoConfig]) -> None:
        missing = set(fetch.missing)
        newly = missing - self._last_missing
        names = {v.video_id: v.display_name() for v in videos}
        for vid in sorted(newly):
            logger.warning("動画 %s（%s）の情報を取得できませんでした。非公開・削除・URL の誤りの可能性があります",
                           names.get(vid, vid), vid)
        for vid in sorted(self._last_missing - missing):
            logger.info("動画 %s の取得が再開しました", names.get(vid, vid))
        self._last_missing = missing

    def _log_summary(self, videos: list[VideoConfig], fetch: FetchResult) -> None:
        for video in videos:
            stats = fetch.stats.get(video.video_id)
            if stats is None or stats.view_count is None:
                continue
            series = analytics.video_series(self.history, video.video_id)
            inc = analytics.increase_over(series, pd.Timedelta(minutes=video.rule.window_minutes),
                                          interval=self.config.interval_seconds)
            if inc.ok:
                change = f"{inc.increase:+,}"
                if inc.span_differs:
                    change += f"（実測{analytics.format_duration(inc.span)}）"
            else:
                change = inc.reason
            logger.info("%s: %s回（直近%s %s）", video.display_name(), f"{stats.view_count:,}",
                        _minutes_label(video.rule.window_minutes), change)

    # ------------------------------------------------------------ 状態・クォータ
    def _quota_day(self, now: datetime) -> str:
        return now.astimezone(PACIFIC).date().isoformat()

    def _add_quota(self, units: int) -> None:
        quota = self.status.setdefault("quota", {"date_pt": "", "used": 0, "limit": self.config.daily_quota})
        today = self._quota_day(self.clock())
        if quota.get("date_pt") != today:
            quota.update({"date_pt": today, "used": 0})
        quota["used"] = int(quota.get("used", 0)) + int(units or 0)
        quota["limit"] = self.config.daily_quota

    def _fail(self, message: str) -> None:
        self._consecutive_failures += 1
        self.status["consecutive_failures"] = self._consecutive_failures
        self._set_state("error", message)

    def _set_state(self, state: str, error: str) -> None:
        self.status["state"] = state
        self.status["last_error"] = error
        if state != "error":
            self.status["consecutive_failures"] = self._consecutive_failures
        self.status["pending_rows"] = self.history_store.pending_count
        self._write_status()

    def _write_status(self) -> None:
        self.status["updated_at"] = format_timestamp(self.clock())
        self.status["interval_seconds"] = self.config.interval_seconds
        write_json_atomic(self.paths.status, self.status)

    # ------------------------------------------------------------ 設定の再読み込み・保存期間
    def _read_config_mtime(self) -> float | None:
        path = self.config.path
        try:
            return path.stat().st_mtime if path else None
        except OSError:
            return None

    def _reload_config_if_changed(self) -> None:
        path = self.config.path
        mtime = self._read_config_mtime()
        if path is None or mtime is None or mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        try:
            new = self.config_loader(path)
        except ConfigError as exc:
            logger.error("config.json の変更を読み込めませんでした（以前の設定で続けます）:\n%s", exc)
            return
        if new.data_dir != self.config.data_dir:
            logger.warning("data_dir の変更は再起動後に反映されます")
            new = _replace_data_dir(new, self.config.data_dir)
        if new.api_key != self.config.api_key and not self._client_injected:
            self.client = YouTubeClient(new.api_key)
        if (new.notification.app_id, new.notification.sound) != (
                self.config.notification.app_id, self.config.notification.sound) and not self._notifier_injected:
            self.notifier = create_notifier(new.notification.app_id, sound=new.notification.sound)
        self.config = new
        for w in new.warnings:
            logger.warning("設定: %s", w)
        logger.info("config.json の変更を反映しました（監視中の動画: %d 本）", len(new.active_videos))

    def _prune_if_needed(self, force: bool = False) -> None:
        days = self.config.retention_days
        if days <= 0:
            return
        today = self.clock().astimezone(self.config.tz).date().isoformat()
        if not force and today == self._last_prune_date:
            return
        if self.history_store.pending_count or self.notification_store.pending_count:
            if not (self.history_store.flush() and self.notification_store.flush()):
                return  # 書き込めない状態（Excel で開いているなど）。次の機会に行う
        cutoff = self.clock() - timedelta(days=days)
        try:
            removed = prune_csv_before(self.paths.history, cutoff)
            removed_n = prune_csv_before(self.paths.notifications, cutoff)
        except OSError as exc:
            logger.warning("古い履歴の削除を延期しました: %s", exc)
            return
        if self.catalog.prune(cutoff):
            self.catalog.save()
        if removed or removed_n:
            logger.info("保存期間（%d 日）を過ぎた履歴 %d 行・通知記録 %d 行を削除しました",
                        days, removed, removed_n)
        self._last_prune_date = today

    # ------------------------------------------------------------ ループ
    def run_forever(self, *, max_cycles: int | None = None) -> None:
        cycles = 0
        try:
            while max_cycles is None or cycles < max_cycles:
                try:
                    result = self.run_cycle()
                except Exception as exc:  # 想定外のエラーでも監視は止めない（Ctrl+C は Exception ではない）
                    logger.exception("予期しないエラーが起きました。次の回に続けます: %s", exc)
                    result = CycleResult(error=f"予期しないエラー: {exc}")
                    try:
                        self._fail(result.error)
                    except Exception:  # 状態ファイルも書けない場合はログだけ残す
                        logger.exception("状態ファイルを更新できませんでした")
                cycles += 1
                if max_cycles is not None and cycles >= max_cycles:
                    break
                now = self.clock()
                if result.quota_exceeded:
                    target = next_quota_reset(now)
                else:
                    target = next_aligned_time(now, self.config.interval_seconds)
                self.status["next_run_at"] = format_timestamp(target)
                self._write_status()
                logger.debug("次の取得: %s", target.astimezone(self.config.tz).strftime("%H:%M:%S"))
                self._sleep_until(target)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """保留中のデータを書き込み、状態を「停止」にする。"""
        self.history_store.flush()
        self.notification_store.flush()
        self.status["state"] = "stopped"
        self.status.pop("next_run_at", None)
        self._write_status()

    def _sleep_until(self, target: datetime) -> None:
        """target まで待つ。短く区切って待つので、PC のスリープ復帰や Ctrl+C にすぐ反応する。"""
        last_heartbeat = self.clock()
        while True:
            now = self.clock()
            remaining = (target - now).total_seconds()
            if remaining <= 0:
                return
            if self._read_config_mtime() != self._config_mtime:
                # 設定は待機中でも読み直す（取得は予定どおりの時刻に行い、API を余分に使わない）
                self._reload_config_if_changed()
                self._write_status()
            elif (now - last_heartbeat).total_seconds() >= 60:
                # 長く待つとき（クォータ回復待ちなど）も「動いている」ことをダッシュボードに伝える
                self._write_status()
                last_heartbeat = now
            self.sleep(min(remaining, 5.0))


def _replace_data_dir(config: AppConfig, data_dir: Path) -> AppConfig:
    from dataclasses import replace

    return replace(config, data_dir=data_dir)


def summarize_targets(config: AppConfig) -> Iterable[str]:
    for v in config.active_videos:
        rule = v.rule
        extra = "" if v.notify else "（通知なし・記録のみ）"
        milestone = f"、{rule.milestone_step:,}回ごとの節目" if rule.milestone_step else ""
        yield (f"- {v.display_name()}（{v.video_id}）: 直近{_minutes_label(rule.window_minutes)}で"
               f"{rule.threshold:,}回以上{milestone}で通知、クールダウン{rule.cooldown_minutes}分{extra}")
