"""YouTube API 通信のテスト（本物の API には接続しない）。"""
import pytest
import requests

import youtube_api as Y
from tests.conftest import FAKE_KEY, VID_A


@pytest.mark.parametrize("ref", [
    "dQw4w9WgXcQ",
    " https://www.youtube.com/watch?v=dQw4w9WgXcQ ",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s&list=PL123",
    "https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?si=abcdef",
    "youtu.be/dQw4w9WgXcQ",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube.com/live/dQw4w9WgXcQ?feature=share",
    "https://www.youtube.com/embed/dQw4w9WgXcQ",
    "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
    "www.youtube.com/watch?v=dQw4w9WgXcQ",
])
def test_extract_video_id(ref):
    assert Y.extract_video_id(ref) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("ref", [
    "", "abc", "https://www.youtube.com/@somechannel", "https://www.youtube.com/channel/UCxxxx",
    "https://www.youtube.com/playlist?list=PL123", "https://example.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/watch?v=short", None,
])
def test_extract_video_id_rejects(ref):
    with pytest.raises(Y.InvalidVideoReference):
        Y.extract_video_id(ref)


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params), "headers": dict(headers)})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        if callable(r):
            return r(params)
        return r


def ok_for(params):
    ids = params["id"].split(",")
    items = [{"id": vid, "snippet": {"title": f"t-{vid}", "channelTitle": "ch"},
              "statistics": {"viewCount": "123", "likeCount": "4"}} for vid in ids if vid != "missingVid0"]
    return FakeResponse(200, {"items": items})


def error_payload(code, reason, message="", detail=None):
    err = {"code": code, "message": message, "errors": [{"reason": reason, "message": message}]}
    if detail:
        err["details"] = [{"reason": detail}]
    return {"error": err}


def client(session, waits=None):
    return Y.YouTubeClient(FAKE_KEY, session=session, sleep=(waits.append if waits is not None else (lambda s: None)))


def test_batches_of_50_and_key_in_header_only():
    ids = [f"vid{i:08d}" for i in range(120)]
    session = FakeSession([ok_for, ok_for, ok_for])
    result = client(session).fetch_videos(ids)
    assert [len(c["params"]["id"].split(",")) for c in session.calls] == [50, 50, 20]
    assert result.quota_used == 3
    assert len(result.stats) == 120 and result.missing == []
    for call in session.calls:
        assert call["headers"]["X-Goog-Api-Key"] == FAKE_KEY
        assert FAKE_KEY not in str(call["params"])  # URL にキーを載せない
        assert call["params"]["part"] == "snippet,statistics"
    stats = result.stats["vid00000000"]
    assert stats.view_count == 123 and stats.like_count == 4 and stats.comment_count is None


def test_duplicate_ids_requested_once_and_missing_reported():
    session = FakeSession([ok_for])
    result = client(session).fetch_videos([VID_A, VID_A, "missingVid0"])
    assert session.calls[0]["params"]["id"] == f"{VID_A},missingVid0"
    assert result.missing == ["missingVid0"]


def test_retries_server_error_then_succeeds():
    waits = []
    session = FakeSession([FakeResponse(503, error_payload(503, "backendError")), ok_for])
    result = client(session, waits).fetch_videos([VID_A])
    assert VID_A in result.stats
    assert len(waits) == 1 and 2 <= waits[0] <= 3
    assert result.quota_used == 2


def test_retry_after_header_is_respected():
    waits = []
    session = FakeSession([FakeResponse(429, None, {"Retry-After": "7"}), ok_for])
    client(session, waits).fetch_videos([VID_A])
    assert waits == [7.0]


def test_network_errors_give_up_after_retries():
    waits = []
    session = FakeSession([requests.ConnectionError(f"https://x/?key={FAKE_KEY}")] * 4)
    with pytest.raises(Y.YouTubeAPIError) as exc:
        client(session, waits).fetch_videos([VID_A])
    assert exc.value.retryable
    assert len(session.calls) == 4 and len(waits) == 3
    assert FAKE_KEY not in str(exc.value)  # 例外の文面に API キーを含めない


def test_quota_exceeded_is_not_retried():
    session = FakeSession([FakeResponse(403, error_payload(403, "quotaExceeded", "quota"))])
    with pytest.raises(Y.QuotaExceededError):
        client(session).fetch_videos([VID_A])
    assert len(session.calls) == 1


def test_invalid_key_real_response_shape():
    # 実際に YouTube API へダミーキーを送ったときと同じ形の応答
    payload = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                         "errors": [{"message": "API key not valid. Please pass a valid API key.",
                                     "domain": "global", "reason": "badRequest"}],
                         "status": "INVALID_ARGUMENT",
                         "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                      "reason": "API_KEY_INVALID", "domain": "googleapis.com"}]}}
    session = FakeSession([FakeResponse(400, payload)])
    with pytest.raises(Y.AuthError) as exc:
        client(session).fetch_videos([VID_A])
    assert len(session.calls) == 1
    assert "API キーが正しくありません" in Y.describe_auth_error(exc.value)


def test_api_not_enabled_message():
    session = FakeSession([FakeResponse(403, error_payload(403, "accessNotConfigured", "disabled"))])
    with pytest.raises(Y.AuthError) as exc:
        client(session).fetch_videos([VID_A])
    assert "有効" in Y.describe_auth_error(exc.value)


def test_fields_rejected_falls_back_without_fields():
    session = FakeSession([FakeResponse(400, error_payload(400, "invalidParameter", "Invalid field selection")),
                           ok_for, ok_for])
    c = client(session)
    assert VID_A in c.fetch_videos([VID_A]).stats
    assert "fields" in session.calls[0]["params"] and "fields" not in session.calls[1]["params"]
    c.fetch_videos([VID_A])
    assert "fields" not in session.calls[2]["params"]


def test_other_client_errors_are_not_retried():
    session = FakeSession([FakeResponse(400, error_payload(400, "invalidParameter", "bad id"))])
    with pytest.raises(Y.YouTubeAPIError) as exc:
        client(session).fetch_videos([VID_A])
    assert not exc.value.retryable and len(session.calls) == 1


def test_missing_api_key():
    with pytest.raises(Y.AuthError):
        Y.YouTubeClient("")
