"""YouTube Data API v3 との通信。

- 動画 URL / 動画 ID から動画 ID を取り出す
- videos.list を最大 50 件ずつまとめて呼び出し、視聴回数などを取得する
- 通信エラー・一時的なサーバーエラーは指数バックオフで再試行する
- クォータ超過・API キーの誤りは、再試行せずに専用の例外で知らせる

API キーは URL ではなく ``X-Goog-Api-Key`` ヘッダーで送る。URL に載せると、
通信エラーのメッセージ（URL を含む）を通じてログに API キーが残るためである。
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable
from urllib.parse import parse_qs, urlparse

import requests

logger = logging.getLogger(__name__)

API_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"
MAX_IDS_PER_REQUEST = 50
# videos.list は part の数によらず 1 回 1 ユニット（1 日の既定上限は 10,000 ユニット）
QUOTA_COST_PER_REQUEST = 1
# 帯域を節約するため、必要な項目だけを返してもらう（YouTube 公式ドキュメントの記法）
RESPONSE_FIELDS = (
    "items(id,snippet(title,channelId,channelTitle,publishedAt,liveBroadcastContent),statistics)"
)

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "gaming.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_PATH_PREFIXES = ("shorts", "live", "embed", "v", "e")

# 再試行しても直らない API キー・権限まわりのエラー
_AUTH_REASONS = {
    "keyInvalid",
    "API_KEY_INVALID",
    "accessNotConfigured",
    "SERVICE_DISABLED",
    "forbidden",
    "ACCESS_DENIED",
    "ipRefererBlocked",
    "API_KEY_HTTP_REFERRER_BLOCKED",
    "API_KEY_IP_ADDRESS_BLOCKED",
    "API_KEY_SERVICE_BLOCKED",
    "keyExpired",
    "API_KEY_EXPIRED",
}
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "RATE_LIMIT_EXCEEDED_DAILY"}
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED"}


class InvalidVideoReference(ValueError):
    """動画 URL / 動画 ID として解釈できない文字列。"""


class YouTubeAPIError(Exception):
    """YouTube API の呼び出しに失敗した。"""

    def __init__(self, message: str, *, status: int | None = None, reason: str = "",
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.retryable = retryable


class QuotaExceededError(YouTubeAPIError):
    """1 日のクォータを使い切った（太平洋時間の 0 時に回復する）。"""


class AuthError(YouTubeAPIError):
    """API キーが無効、API が有効化されていない、キーの制限で拒否された、など。"""


def extract_video_id(reference: str) -> str:
    """動画 URL または動画 ID から 11 文字の動画 ID を取り出す。

    対応する形式: watch?v=, youtu.be/, shorts/, live/, embed/, v/, スキームなし, 動画 ID そのもの。
    """
    if not isinstance(reference, str):
        raise InvalidVideoReference(f"動画 URL は文字列で指定してください: {reference!r}")
    text = reference.strip()
    if not text:
        raise InvalidVideoReference("動画 URL が空です")
    if VIDEO_ID_RE.fullmatch(text):
        return text

    candidate = text if "://" in text else "https://" + text
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]

    video_id = ""
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parts[0] if parts else ""
    elif host in _YOUTUBE_HOSTS:
        if parts and parts[0] == "watch":
            video_id = (parse_qs(parsed.query).get("v") or [""])[0]
        elif len(parts) >= 2 and parts[0] in _PATH_PREFIXES:
            video_id = parts[1]
        elif parts and parts[0] in {"channel", "c", "user", "playlist"} or (
            parts and parts[0].startswith("@")
        ):
            raise InvalidVideoReference(
                f"チャンネルや再生リストの URL です。動画の URL を指定してください: {reference}"
            )
    else:
        raise InvalidVideoReference(f"YouTube の URL ではありません: {reference}")

    if VIDEO_ID_RE.fullmatch(video_id):
        return video_id
    raise InvalidVideoReference(f"URL から動画 ID を読み取れませんでした: {reference}")


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def thumbnail_url(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def _to_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class VideoStats:
    video_id: str
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    title: str = ""
    channel_id: str = ""
    channel_title: str = ""
    published_at: str = ""
    live_status: str = ""  # none / live / upcoming

    @classmethod
    def from_item(cls, item: dict) -> "VideoStats":
        snippet = item.get("snippet") or {}
        stats = item.get("statistics") or {}
        return cls(
            video_id=str(item.get("id", "")),
            view_count=_to_int(stats.get("viewCount")),
            like_count=_to_int(stats.get("likeCount")),
            comment_count=_to_int(stats.get("commentCount")),
            title=str(snippet.get("title", "")),
            channel_id=str(snippet.get("channelId", "")),
            channel_title=str(snippet.get("channelTitle", "")),
            published_at=str(snippet.get("publishedAt", "")),
            live_status=str(snippet.get("liveBroadcastContent", "")),
        )


@dataclass
class FetchResult:
    fetched_at: datetime
    stats: dict[str, VideoStats] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)  # 非公開・削除などで返ってこなかった動画
    quota_used: int = 0


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _error_reasons(payload: object) -> list[str]:
    reasons: list[str] = []
    if not isinstance(payload, dict):
        return reasons
    err = payload.get("error")
    if not isinstance(err, dict):
        return reasons
    for item in err.get("errors") or []:
        if isinstance(item, dict) and item.get("reason"):
            reasons.append(str(item["reason"]))
    for item in err.get("details") or []:
        if isinstance(item, dict) and item.get("reason"):
            reasons.append(str(item["reason"]))
    if err.get("status"):
        reasons.append(str(err["status"]))
    return reasons


class YouTubeClient:
    """videos.list を使って複数動画の統計情報を取得するクライアント。"""

    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        timeout: tuple[float, float] = (10.0, 30.0),
        max_retries: int = 3,
        backoff_base: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not api_key:
            raise AuthError("API キーが設定されていません")
        self._api_key = api_key
        self._session = session or requests.Session()
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._use_fields = True

    def fetch_videos(self, video_ids: Iterable[str]) -> FetchResult:
        """動画 ID の一覧について統計情報を取得する（50 件ずつまとめて 1 リクエスト）。"""
        ids = list(dict.fromkeys(video_ids))  # 順序を保って重複を除く
        result = FetchResult(fetched_at=self._now().replace(microsecond=0))
        for batch in _chunks(ids, MAX_IDS_PER_REQUEST):
            payload, used = self._request_with_retry(batch)
            result.quota_used += used
            for item in payload.get("items") or []:
                stats = VideoStats.from_item(item)
                if stats.video_id:
                    result.stats[stats.video_id] = stats
        # 応答を受け取った時刻をその回の取得時刻とする（同じ回の動画は同じ時刻にそろえる）
        result.fetched_at = self._now().replace(microsecond=0)
        result.missing = [vid for vid in ids if vid not in result.stats]
        return result

    # ------------------------------------------------------------------ 内部処理
    def _request_with_retry(self, batch: list[str]) -> tuple[dict, int]:
        used = 0
        attempt = 0
        while True:
            try:
                payload, counted = self._request_once(batch)
                used += counted
                return payload, used
            except YouTubeAPIError as exc:
                if exc.status is not None:
                    used += QUOTA_COST_PER_REQUEST
                if not exc.retryable or attempt >= self._max_retries:
                    exc.quota_used = used  # type: ignore[attr-defined]
                    raise
                wait = getattr(exc, "retry_after", None)
                if wait is None:
                    wait = self._backoff_base * (2 ** attempt) + random.uniform(0, 1)
                attempt += 1
                logger.warning(
                    "YouTube API の呼び出しに失敗しました（%s）。%.0f 秒後に再試行します（%d/%d 回目）",
                    exc, wait, attempt, self._max_retries,
                )
                self._sleep(wait)

    def _request_once(self, batch: list[str]) -> tuple[dict, int]:
        params = {"part": "snippet,statistics", "id": ",".join(batch), "maxResults": len(batch)}
        if self._use_fields:
            params["fields"] = RESPONSE_FIELDS
        headers = {"X-Goog-Api-Key": self._api_key, "Accept": "application/json"}
        try:
            response = self._session.get(
                API_ENDPOINT, params=params, headers=headers, timeout=self._timeout
            )
        except requests.Timeout as exc:
            raise YouTubeAPIError(f"タイムアウト（{type(exc).__name__}）", retryable=True) from None
        except requests.RequestException as exc:
            raise YouTubeAPIError(f"通信エラー（{type(exc).__name__}）", retryable=True) from None

        status = response.status_code
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if status == 200:
            if not isinstance(payload, dict):
                raise YouTubeAPIError("応答が JSON ではありません", status=status, retryable=True)
            return payload, QUOTA_COST_PER_REQUEST

        reasons = _error_reasons(payload)
        message = ""
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            message = str(payload["error"].get("message", ""))
        summary = f"HTTP {status} {'/'.join(reasons) or ''} {message}".strip()

        if status == 400 and self._use_fields and "field" in message.lower():
            # 万一 fields の指定が拒否されたら、以後は指定せずに取り直す
            logger.warning("fields 指定が拒否されたため、指定なしで再取得します: %s", message)
            self._use_fields = False
            payload, counted = self._request_once(batch)
            return payload, counted + QUOTA_COST_PER_REQUEST

        reason_set = set(reasons)
        if reason_set & _QUOTA_REASONS:
            raise QuotaExceededError(summary, status=status, reason="quotaExceeded")
        if reason_set & _AUTH_REASONS or status in (401,):
            raise AuthError(summary, status=status, reason=(reasons[0] if reasons else ""))
        if status == 429 or reason_set & _RATE_REASONS or status >= 500:
            err = YouTubeAPIError(summary, status=status,
                                  reason=(reasons[0] if reasons else ""), retryable=True)
            retry_after = response.headers.get("Retry-After") if response.headers else None
            if retry_after and str(retry_after).isdigit():
                err.retry_after = min(float(retry_after), 60.0)  # type: ignore[attr-defined]
            raise err
        raise YouTubeAPIError(summary, status=status, reason=(reasons[0] if reasons else ""))


def describe_auth_error(exc: AuthError) -> str:
    """API キー関連エラーの対処方法を日本語で返す。"""
    reason = exc.reason
    if reason in {"keyInvalid", "API_KEY_INVALID", "badRequest"} or "not valid" in str(exc):
        return "API キーが正しくありません。config.json の api_key（または環境変数 YOUTUBE_API_KEY）を確認してください。"
    if reason in {"accessNotConfigured", "SERVICE_DISABLED"}:
        return "Google Cloud のプロジェクトで「YouTube Data API v3」が有効になっていません。API ライブラリから有効にしてください。"
    if "BLOCKED" in reason or reason == "ipRefererBlocked":
        return "API キーの制限（HTTP リファラー / IP アドレス / API の制限）で拒否されました。キーの制限を「YouTube Data API v3」のみ・アプリケーションの制限なしにしてください。"
    if "EXPIRED" in reason.upper():
        return "API キーの有効期限が切れています。新しいキーを作成してください。"
    return "API キーまたは Google Cloud プロジェクトの設定を確認してください。"
