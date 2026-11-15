"""YouTube View Monitor — 監視プログラムの起動。

使い方（くわしくは README.md）:
    python main.py                 5 分ごとに視聴回数を取得し続ける（Ctrl+C で停止）
    python main.py --check         設定と API キーを確認する（API を 1 回だけ使う）
    python main.py --once          1 回だけ取得して終了する（タスク スケジューラー用）
    python main.py --test-notify   Windows 通知が表示されるか試す
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from config import AppConfig, ConfigError, ensure_config_file, load_config, resolve_config_path
from data_manager import DataFormatError
from instance_lock import AlreadyRunningError, InstanceLock
from monitor import APP_VERSION, MonitorService, summarize_targets
from notifier import Message, create_notifier
from youtube_api import AuthError, QuotaExceededError, YouTubeAPIError, YouTubeClient, describe_auth_error

logger = logging.getLogger("ytmonitor")


class RedactingFormatter(logging.Formatter):
    """ログに API キーが出ないよう、万一含まれていたら伏せ字にする。"""

    def __init__(self, fmt: str, secrets: list[str]) -> None:
        super().__init__(fmt, datefmt="%Y-%m-%d %H:%M:%S")
        self.secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text


def configure_console() -> None:
    """Windows のコンソールで絵文字などを含むタイトルを表示しても落ちないようにする。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def setup_logging(config: AppConfig | None, verbose: bool = False) -> None:
    secrets = [config.api_key] if config else []
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if sys.stdout is not None:  # pythonw.exe（画面なし）で起動したときは stdout がない
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(message)s", secrets))
        root.addHandler(console)
    if config is not None:
        config.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            config.log_dir / "monitor.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(RedactingFormatter(
            "%(asctime)s %(levelname)-7s [%(name)s] %(message)s", secrets))
        root.addHandler(file_handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="YouTube の視聴回数を監視し、急に伸びたら Windows に通知します")
    parser.add_argument("--config", help="設定ファイルのパス（既定: このフォルダーの config.json）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="設定と API キーを確認して終了する")
    mode.add_argument("--once", action="store_true", help="1 回だけ取得して終了する")
    mode.add_argument("--test-notify", action="store_true", help="テスト通知を表示して終了する")
    parser.add_argument("--verbose", action="store_true", help="詳しいログを表示する")
    parser.add_argument("--version", action="version", version=f"YouTube View Monitor {APP_VERSION}")
    return parser.parse_args(argv)


def _print_problems(title: str, problems: list[str]) -> None:
    print(f"\n{title}")
    for p in problems:
        print(f"  - {p}")
    print()


def load_or_explain(path: Path, *, require_api_key: bool = True,
                    require_videos: bool = True) -> AppConfig | None:
    try:
        if ensure_config_file(path):
            print(
                f"\n設定ファイル {path.name} を作成しました（場所: {path}）。\n"
                "メモ帳などで開き、次の 2 か所を書き換えてから、もう一度実行してください。\n"
                "  1. \"api_key\": YouTube Data API v3 の API キー（取得方法は README.md）\n"
                "  2. \"videos\": 監視したい動画の URL と表示名\n"
            )
            return None
        return load_config(path, require_api_key=require_api_key, require_videos=require_videos)
    except ConfigError as exc:
        _print_problems(f"{path.name} の内容を確認してください:", exc.problems)
        return None


def run_check(config: AppConfig) -> int:
    print(f"\n設定ファイル: {config.path}")
    print(f"API キー: {config.api_key_source} から読み込み（{config.api_key[:4]}…{config.api_key[-2:]}）")
    print(f"監視間隔: {config.interval_seconds} 秒 / データの保存先: {config.data_dir}")
    retention = f"{config.retention_days} 日" if config.retention_days else "無期限"
    print(f"履歴の保存期間: {retention} / 表示のタイムゾーン: {config.timezone}")
    print(f"1 日の推定クォータ消費: 約 {config.estimated_daily_quota():,} / {config.daily_quota:,} ユニット")
    print("\n監視する動画と通知条件:")
    for line in summarize_targets(config):
        print("  " + line)
    try:
        result = YouTubeClient(config.api_key, max_retries=1).fetch_videos(
            [v.video_id for v in config.active_videos])
    except AuthError as exc:
        print(f"\nNG: API キーのエラーです（{exc}）\n  → {describe_auth_error(exc)}")
        return 1
    except QuotaExceededError:
        print("\nNG: 今日のクォータを使い切っています。太平洋時間の 0 時（日本時間 17 時、夏時間は 16 時）に回復します")
        return 1
    except YouTubeAPIError as exc:
        print(f"\nNG: YouTube API に接続できませんでした（{exc}）。インターネット接続を確認してください")
        return 1
    print("\nYouTube API から取得できました:")
    for v in config.active_videos:
        s = result.stats.get(v.video_id)
        if s is None:
            print(f"  NG {v.display_name()}: 見つかりません（非公開・削除・URL の誤り）")
        else:
            views = f"{s.view_count:,}回" if s.view_count is not None else "視聴回数非公開"
            print(f"  OK {v.display_name()}: {views}  「{s.title}」 / {s.channel_title}")
    for w in config.warnings:
        print(f"  注意: {w}")
    print("\n確認が終わりました。監視を始めるには python main.py を実行してください。")
    return 0 if not result.missing else 1


def run_test_notify(config: AppConfig) -> int:
    notifier = create_notifier(config.notification.app_id, sound=config.notification.sound)
    ok = notifier.send(Message(
        title="📈 テスト通知: YouTube View Monitor",
        body="この通知が見えていれば設定は完了です。クリックするとダッシュボードを開きます。",
        url=config.dashboard.url, tag="test",
    ))
    if sys.platform != "win32":
        print("Windows 以外の環境のため、通知はコンソールに表示しました。")
    elif ok:
        print("テスト通知を送りました。画面右下に表示されない場合は README の「通知が表示されないとき」を見てください。")
    else:
        print("通知を表示できませんでした。logs/monitor.log のエラーを確認してください。")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    configure_console()
    args = parse_args(argv)
    path = resolve_config_path(args.config)
    config = load_or_explain(path, require_api_key=not args.test_notify,
                             require_videos=not args.test_notify)
    if config is None:
        return 1
    setup_logging(config, args.verbose)
    for w in config.warnings:
        logger.warning("設定: %s", w)

    if args.check:
        return run_check(config)
    if args.test_notify:
        return run_test_notify(config)

    lock = InstanceLock(config.data_dir / "monitor.lock")
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        logger.error(str(exc))
        return 1
    try:
        service = MonitorService(config)
        service.startup()
        if args.once:
            try:
                result = service.run_cycle()
            finally:
                service.shutdown()
            return 0 if not result.error else 1
        logger.info("監視を開始しました（%d 本・%d 秒ごと）。停止するには Ctrl+C を押してください",
                    len(config.active_videos), config.interval_seconds)
        for line in summarize_targets(config):
            logger.info(line)
        service.run_forever()
    except KeyboardInterrupt:
        logger.info("監視を停止しました")
    except DataFormatError as exc:
        logger.error(str(exc))
        return 1
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
