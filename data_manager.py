"""CSV / JSON の読み書きと履歴管理。

保存するファイル（すべて data フォルダー内）:

- history.csv        視聴回数の取得履歴（追記のみ）
- notifications.csv  送った通知の履歴（再起動後のクールダウン判定にも使う）
- videos.json        動画のタイトル・チャンネル名など（ダッシュボード表示用）
- status.json        監視プログラムの稼働状況（最終取得時刻・エラー・クォータ消費量）
- monitor.lock       監視プログラムの二重起動防止用

設計上の注意:

- 日時はすべて UTC の ISO 8601（例: 2026-11-15T06:10:00+00:00）で保存し、表示するときに変換する
- 監視プログラムが書き込み中の行をダッシュボードが読んでも壊れないよう、
  改行で終わっていない最後の行は読み飛ばす
- Excel などでファイルを開いていて書き込めないときは、データを捨てずにメモリーに保留し、
  次の回にまとめて書き込む
- 同じ動画・同じ時刻の重複行は、後から書いたほうを残す
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

logger = logging.getLogger(__name__)

HISTORY_COLUMNS = ["timestamp", "video_id", "view_count", "like_count", "comment_count"]
NOTIFICATION_COLUMNS = [
    "timestamp", "video_id", "kind", "window_minutes", "increase", "threshold",
    "view_count", "milestone", "delivered", "message",
]
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class DataFormatError(Exception):
    """既存の CSV の形式が想定と異なる（勝手に書き換えないために処理を止める）。"""


# ---------------------------------------------------------------------- 日時
def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("タイムゾーンのない日時は扱いません（UTC に統一するため）")
    return dt.astimezone(timezone.utc)


def format_timestamp(dt: datetime) -> str:
    return to_utc(dt).replace(microsecond=0).isoformat(timespec="seconds")


def parse_timestamps(values: pd.Series) -> pd.Series:
    """ISO 8601 文字列を UTC の日時（ナノ秒単位）に変換する。読めない値は NaT。

    タイムゾーンの書かれていない日時は UTC とみなす。
    """
    text = values.astype(str).str.strip()
    has_tz = text.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", regex=True, na=False)
    if bool(has_tz.all()):  # ふつうはすべて UTC 付き（監視プログラムが書いた行）
        return pd.to_datetime(text, utc=True, errors="coerce", format="ISO8601").dt.as_unit("ns")
    # タイムゾーンあり・なしが混ざると pandas の版によっては時刻がずれるため、分けて変換する
    out = pd.Series(pd.NaT, index=text.index, dtype="datetime64[ns, UTC]")
    if bool(has_tz.any()):
        aware = pd.to_datetime(text[has_tz], utc=True, errors="coerce", format="ISO8601")
        out[has_tz] = aware.dt.as_unit("ns")
    naive = pd.to_datetime(text[~has_tz], errors="coerce", format="ISO8601")
    out[~has_tz] = naive.dt.tz_localize("UTC").dt.as_unit("ns")
    return out


# ---------------------------------------------------------------------- パス
@dataclass(frozen=True)
class DataPaths:
    data_dir: Path

    @property
    def history(self) -> Path:
        return self.data_dir / "history.csv"

    @property
    def notifications(self) -> Path:
        return self.data_dir / "notifications.csv"

    @property
    def videos(self) -> Path:
        return self.data_dir / "videos.json"

    @property
    def status(self) -> Path:
        return self.data_dir / "status.json"

    @property
    def lock(self) -> Path:
        return self.data_dir / "monitor.lock"


# ---------------------------------------------------------------------- 安全な読み込み
def read_complete_text(path: Path) -> tuple[str, bool]:
    """ファイルを読み、改行で終わっていない最後の行（書き込み途中の可能性）を除いて返す。

    戻り値: (テキスト, 途中の行を除いたか)
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "", False
    partial = False
    if raw and not raw.endswith(b"\n"):
        cut = raw.rfind(b"\n")
        raw = raw[:cut + 1] if cut >= 0 else b""
        partial = True
    return raw.decode("utf-8-sig", errors="replace"), partial


@dataclass
class LoadReport:
    rows_total: int = 0
    rows_invalid: int = 0
    duplicates_removed: int = 0
    partial_line_skipped: bool = False
    header_ok: bool = True

    def messages(self) -> list[str]:
        out = []
        if not self.header_ok:
            out.append("CSV の見出し行が想定と異なります")
        if self.rows_invalid:
            out.append(f"読み取れない行を {self.rows_invalid:,} 行読み飛ばしました")
        if self.duplicates_removed:
            out.append(f"同じ動画・同じ時刻の重複を {self.duplicates_removed:,} 行除きました")
        return out


def empty_history() -> pd.DataFrame:
    df = pd.DataFrame({
        "timestamp": pd.Series([], dtype="datetime64[ns, UTC]"),
        "video_id": pd.Series([], dtype=object),
        "view_count": pd.Series([], dtype="int64"),
        "like_count": pd.Series([], dtype="Int64"),
        "comment_count": pd.Series([], dtype="Int64"),
    })
    return df


def normalize_history(raw: pd.DataFrame, report: LoadReport | None = None) -> pd.DataFrame:
    """文字列の表を検証・型変換し、重複を除いて (video_id, timestamp) 順に並べる。"""
    report = report or LoadReport()
    if raw.empty:
        return empty_history()
    for col in HISTORY_COLUMNS:
        if col not in raw.columns:
            raw[col] = ""
    df = pd.DataFrame({
        "timestamp": parse_timestamps(raw["timestamp"].astype(str).str.strip()),
        "video_id": raw["video_id"].astype(str).str.strip(),
        "view_count": pd.to_numeric(raw["view_count"], errors="coerce"),
        "like_count": pd.to_numeric(raw["like_count"], errors="coerce"),
        "comment_count": pd.to_numeric(raw["comment_count"], errors="coerce"),
    })
    valid = (
        df["timestamp"].notna()
        & df["video_id"].str.fullmatch(_VIDEO_ID_RE.pattern).fillna(False).astype(bool)
        & df["view_count"].notna()
        & (df["view_count"] >= 0)
    )
    report.rows_invalid += int((~valid).sum())
    df = df[valid].copy()
    before = len(df)
    df = df.drop_duplicates(subset=["video_id", "timestamp"], keep="last")
    report.duplicates_removed += before - len(df)
    df["view_count"] = df["view_count"].astype("int64")
    df["like_count"] = df["like_count"].round().astype("Int64")
    df["comment_count"] = df["comment_count"].round().astype("Int64")
    df["video_id"] = df["video_id"].astype(object)
    return df.sort_values(["video_id", "timestamp"], kind="stable").reset_index(drop=True)


def read_history_csv(path: Path) -> tuple[pd.DataFrame, LoadReport]:
    """history.csv を読み込む。存在しない・空のときは空の表を返す。"""
    report = LoadReport()
    text, partial = read_complete_text(path)
    report.partial_line_skipped = partial
    if not text.strip():
        return empty_history(), report
    lines = pd.Series(text.splitlines())
    lines = lines[lines.str.strip() != ""]
    header = lines.iloc[0].strip().split(",") if len(lines) else []
    report.header_ok = header[:3] == HISTORY_COLUMNS[:3]
    if not report.header_ok:
        return empty_history(), report
    body = lines.iloc[1:]
    report.rows_total = len(body)
    # この CSV の行は必ず 5 項目（カンマ 4 個）。異常終了で途中まで書かれた行
    # （例: "...,dQw4w9WgXcQ,12" と数字が切れた行）は項目数が合わないので除外する
    complete = body.str.count(",") == len(header) - 1
    report.rows_invalid += int((~complete).sum())
    csv_text = lines.iloc[0] + "\n" + "\n".join(body[complete]) + "\n"
    raw = pd.read_csv(io.StringIO(csv_text), dtype=str, keep_default_na=False,
                      on_bad_lines="skip", skip_blank_lines=True)
    return normalize_history(raw, report), report


def read_notifications_csv(path: Path) -> pd.DataFrame:
    text, _ = read_complete_text(path)
    if not text.strip():
        return pd.DataFrame(columns=NOTIFICATION_COLUMNS)
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, on_bad_lines="skip")
    for col in NOTIFICATION_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df["timestamp"] = parse_timestamps(df["timestamp"])
    for col in ("window_minutes", "increase", "threshold", "view_count", "milestone"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["delivered"] = df["delivered"].astype(str).str.lower().isin(["true", "1", "yes"])
    return df[df["timestamp"].notna()].sort_values("timestamp").reset_index(drop=True)


# ---------------------------------------------------------------------- 追記
def _is_locked_error(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (32, 33)


class CsvAppender:
    """CSV への追記。書き込めないときは行を保留し、次回にまとめて書く。"""

    def __init__(self, path: Path, columns: list[str]) -> None:
        self.path = path
        self.columns = columns
        self._pending: list[list[str]] = []
        self._checked = False
        self._warned_locked = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def check_format(self) -> None:
        """既存ファイルの見出し行を確かめる。違っていれば DataFormatError。"""
        if not self.path.exists() or self.path.stat().st_size == 0:
            self._checked = True
            return
        with self.path.open("r", encoding="utf-8-sig", newline="") as f:
            header = f.readline().strip().split(",")
        if header != self.columns:
            raise DataFormatError(
                f"{self.path.name} の見出し行が想定と異なります。\n"
                f"  現在: {','.join(header)}\n  想定: {','.join(self.columns)}\n"
                "データを守るため自動では書き換えません。ファイル名を変えて退避するか、"
                "config.json の data_dir で別のフォルダーを指定してください。"
            )
        self._checked = True

    def append(self, rows: Iterable[Mapping[str, Any]]) -> bool:
        """行を追記する。書き込めたら True、保留したら False。"""
        for row in rows:
            self._pending.append(["" if row.get(c) is None else str(row.get(c)) for c in self.columns])
        return self.flush()

    def flush(self) -> bool:
        if not self._pending:
            return True
        if not self._checked:
            self.check_format()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        try:
            new_file = not self.path.exists() or self.path.stat().st_size == 0
            needs_newline = False
            if not new_file:
                with self.path.open("rb") as f:
                    f.seek(-1, os.SEEK_END)
                    needs_newline = f.read(1) != b"\n"  # 前回の書き込みが途中で止まっていた
            if needs_newline:
                logger.warning("%s の最後の行が途中で途切れていました（前回の異常終了など）。その行は読み込み時に除外されます",
                               self.path.name)
            if new_file:
                writer.writerow(self.columns)
            writer.writerows(self._pending)
            text = ("\n" if needs_newline else "") + buf.getvalue()
            # 1 回の write でまとめて書く（読み手が途中の状態を見る時間を短くする）
            with self.path.open("a", encoding="utf-8", newline="") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            if _is_locked_error(exc):
                if not self._warned_locked:
                    logger.warning(
                        "%s に書き込めません（Excel などで開いていませんか？）。%d 行を保留し、次回まとめて書き込みます",
                        self.path.name, len(self._pending),
                    )
                    self._warned_locked = True
                return False
            raise
        if self._warned_locked:
            logger.info("%s への保留分（%d 行）を書き込みました", self.path.name, len(self._pending))
        self._warned_locked = False
        self._pending.clear()
        return True


class HistoryStore(CsvAppender):
    def __init__(self, path: Path) -> None:
        super().__init__(path, HISTORY_COLUMNS)

    def append_samples(self, timestamp: datetime, samples: Iterable[Mapping[str, Any]]) -> bool:
        ts = format_timestamp(timestamp)
        rows = []
        for s in samples:
            rows.append({
                "timestamp": ts,
                "video_id": s["video_id"],
                "view_count": s.get("view_count"),
                "like_count": s.get("like_count"),
                "comment_count": s.get("comment_count"),
            })
        return self.append(rows)

    def load(self, since: datetime | None = None) -> tuple[pd.DataFrame, LoadReport]:
        df, report = read_history_csv(self.path)
        if since is not None and not df.empty:
            df = df[df["timestamp"] >= pd.Timestamp(to_utc(since))].reset_index(drop=True)
        return df, report


def prune_csv_before(path: Path, cutoff: datetime) -> int:
    """日時が cutoff より古い行を削除する（保存期間の管理）。削除した行数を返す。

    日時を読み取れない行は、念のため残す。書き換えは一時ファイル経由で行う。
    """
    text, _partial = read_complete_text(path)
    if not text:
        return 0
    cutoff_utc = pd.Timestamp(to_utc(cutoff))
    lines = text.splitlines(keepends=True)
    header, body = lines[0], lines[1:]
    stamps = parse_timestamps(pd.Series([ln.split(",", 1)[0] for ln in body], dtype=object))
    keep = [ln for ln, ts in zip(body, stamps) if pd.isna(ts) or ts >= cutoff_utc]
    removed = len(body) - len(keep)
    if removed == 0:
        return 0
    tmp = path.with_name(path.name + ".tmp")
    # 書き換えは、唯一の書き手である監視プログラム自身が追記の合間に行う
    # （読み込みから置き換えまでの間に追記が割り込まないようにするため）
    with tmp.open("w", encoding="utf-8", newline="") as f:
        f.write(header)
        f.writelines(keep)
        f.flush()
        os.fsync(f.fileno())
    if not replace_with_retry(tmp, path):
        tmp.unlink(missing_ok=True)
        return 0
    return removed


# ---------------------------------------------------------------------- JSON
def replace_with_retry(src: Path, dst: Path, attempts: int = 10, delay: float = 0.2) -> bool:
    """os.replace を再試行付きで行う（Windows では読み手が開いている間は置き換えに失敗する）。"""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return True
        except PermissionError:
            if i == attempts - 1:
                logger.warning("%s を更新できませんでした（他のプログラムが使用中）", dst.name)
                return False
            time.sleep(delay)
    return False


def write_json_atomic(path: Path, data: Any) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    ok = replace_with_retry(tmp, path)
    if not ok:
        tmp.unlink(missing_ok=True)
    return ok


def read_json(path: Path, default: Any = None) -> Any:
    for _ in range(3):
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, PermissionError):
            time.sleep(0.05)  # 置き換えの瞬間に当たった場合は少し待って読み直す
        except OSError:
            return default
    return default


@dataclass
class VideoCatalog:
    """動画のタイトルなどの情報（videos.json）。"""

    path: Path
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "VideoCatalog":
        data = read_json(path, {})
        return cls(path=path, entries=data if isinstance(data, dict) else {})

    def update(self, info: Mapping[str, Mapping[str, Any]], now: datetime) -> bool:
        changed = False
        for vid, values in info.items():
            entry = dict(self.entries.get(vid, {}))
            new_values = {k: v for k, v in values.items() if v not in (None, "")}
            if any(entry.get(k) != v for k, v in new_values.items()):
                changed = True
            entry.update(new_values)
            entry["updated_at"] = format_timestamp(now)
            self.entries[vid] = entry
        return changed

    def prune(self, cutoff: datetime) -> int:
        cutoff_s = format_timestamp(cutoff)
        old = [vid for vid, e in self.entries.items() if str(e.get("updated_at", "")) < cutoff_s]
        for vid in old:
            del self.entries[vid]
        return len(old)

    def save(self) -> bool:
        return write_json_atomic(self.path, self.entries)

    def title(self, video_id: str) -> str:
        return str(self.entries.get(video_id, {}).get("title", ""))
