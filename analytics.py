"""視聴回数の増加数・増加速度の計算（通知の判定とダッシュボードで共通に使う）。

計算のきまり（docs/design.md にも記載）:

1. 期間の終わり = その動画の最新の取得データ（指定があればその時刻に最も近いデータ）
2. 期間の始まり = 終わり − 期間。この時刻に**最も近い**取得データを基準にする
   - ただし基準データとのずれが「許容誤差」以内のものに限る
   - 許容誤差 = max(期間 × 10%, 10 分, 監視間隔)。ただし期間の半分を超えない
   - 同じ距離なら過去側（期間を長めにとる側）を選ぶ
3. 基準データが見つからなければ「データ不足」とし、増加数は出さない（0 とも表示しない）
4. 増加数 = 終わりの視聴回数 − 基準の視聴回数。減った場合は負の値のまま返す
   （YouTube 側の不正な再生の除去などで実際に減ることがあるため。通知は正の増加だけが対象）
5. 速度（1 時間あたり・1 分あたり）は、名目の期間ではなく実際の時間差で割る。
   これにより取得間隔にばらつきや欠けがあっても計算が破綻しない
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

TOLERANCE_RATIO = 0.10
MIN_TOLERANCE = pd.Timedelta(minutes=10)
STANDARD_WINDOWS: tuple[pd.Timedelta, ...] = tuple(pd.Timedelta(hours=h) for h in (1, 3, 6, 12, 24))
TREND_THRESHOLD_PERCENT = 10.0


def _td(value: pd.Timedelta | float | int | None, unit: str = "s") -> pd.Timedelta | None:
    if value is None:
        return None
    if isinstance(value, pd.Timedelta):
        return value
    return pd.Timedelta(value, unit=unit)


def tolerance_for(window: pd.Timedelta, interval: pd.Timedelta | float | None = None) -> pd.Timedelta:
    """期間 window の基準データを探すときの許容誤差。interval は監視間隔（秒でも可）。"""
    floor = MIN_TOLERANCE
    interval_td = _td(interval)
    if interval_td is not None and interval_td > floor:
        floor = interval_td
    tol = max(window * TOLERANCE_RATIO, floor)
    return min(tol, window / 2)


def format_duration(td: pd.Timedelta | None) -> str:
    if td is None or pd.isna(td):
        return "-"
    minutes = int(round(td.total_seconds() / 60))
    if minutes < 60:
        return f"{minutes}分"
    hours, rem = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}時間" + (f"{rem}分" if rem else "")
    days, hours = divmod(hours, 24)
    return f"{days}日" + (f"{hours}時間" if hours else "")


def _to_utc_ts(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        raise ValueError("タイムゾーンのない日時は扱いません")
    return ts.tz_convert("UTC").as_unit("ns")


def video_series(history: pd.DataFrame, video_id: str) -> pd.DataFrame:
    """履歴の表から 1 本の動画の (timestamp, view_count) を時刻順に取り出す。"""
    if history.empty:
        return pd.DataFrame({"timestamp": pd.Series([], dtype="datetime64[ns, UTC]"),
                             "view_count": pd.Series([], dtype="int64")})
    s = history.loc[history["video_id"] == video_id, ["timestamp", "view_count"]]
    return prepare_series(s)


def prepare_series(series: pd.DataFrame) -> pd.DataFrame:
    """時刻順に並べ、同じ時刻の重複を除き、型をそろえる。"""
    s = series[["timestamp", "view_count"]].dropna()
    s = s.assign(timestamp=pd.to_datetime(s["timestamp"], utc=True).dt.as_unit("ns"))
    s = s.sort_values("timestamp", kind="stable").drop_duplicates("timestamp", keep="last")
    return s.astype({"view_count": "int64"}).reset_index(drop=True)


# ---------------------------------------------------------------------- 基本操作
def resolve_points(series: pd.DataFrame, targets: Iterable, tolerance: pd.Timedelta) -> pd.DataFrame:
    """各目標時刻に最も近い取得データ（許容誤差以内）を返す。

    戻り値の列: target, sample_time, view_count（見つからなければ NaT / NaN）。並びは targets と同じ。
    両側にあれば近いほう、同じ距離なら過去側を選ぶ。二分探索なので長い履歴でも速い。
    """
    target_index = pd.DatetimeIndex([_to_utc_ts(t) for t in targets]).as_unit("ns")
    n_targets = len(target_index)
    if series.empty or n_targets == 0:
        return pd.DataFrame({
            "target": target_index,
            "sample_time": pd.Series(pd.NaT, index=range(n_targets), dtype="datetime64[ns, UTC]"),
            "view_count": np.full(n_targets, np.nan),
        })
    s = series if series["timestamp"].is_monotonic_increasing else series.sort_values("timestamp")
    times = pd.DatetimeIndex(s["timestamp"]).tz_convert("UTC").as_unit("ns")
    t_ns = times.asi8
    values = s["view_count"].to_numpy(dtype="float64")
    goal = target_index.asi8
    tol = int(pd.Timedelta(tolerance).value)
    n = len(t_ns)

    after = np.searchsorted(t_ns, goal, side="right")  # goal より後の最初のデータ
    back = after - 1                                    # goal 以前の最後のデータ（同時刻を含む）
    back_safe = np.clip(back, 0, n - 1)
    fwd_safe = np.clip(after, 0, n - 1)
    back_dist = goal - t_ns[back_safe]
    fwd_dist = t_ns[fwd_safe] - goal
    back_ok = (back >= 0) & (back_dist <= tol)
    fwd_ok = (after < n) & (fwd_dist <= tol)
    use_back = back_ok & (~fwd_ok | (back_dist <= fwd_dist))
    use_fwd = ~use_back & fwd_ok
    chosen = np.where(use_back, back_safe, fwd_safe)
    found = use_back | use_fwd

    sample_time = pd.Series(times[chosen], dtype="datetime64[ns, UTC]").where(found)
    return pd.DataFrame({
        "target": pd.Series(target_index),
        "sample_time": sample_time,
        "view_count": np.where(found, values[chosen], np.nan),
    })


@dataclass(frozen=True)
class Increase:
    """ある期間の増加数。ok でないときは reason に理由が入る。"""

    window: pd.Timedelta | None
    start_target: pd.Timestamp | None
    end_time: pd.Timestamp | None
    end_views: int | None
    base_time: pd.Timestamp | None
    base_views: int | None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason

    @property
    def increase(self) -> int | None:
        if not self.ok:
            return None
        return int(self.end_views) - int(self.base_views)  # type: ignore[arg-type]

    @property
    def span(self) -> pd.Timedelta | None:
        if not self.ok:
            return None
        return self.end_time - self.base_time  # type: ignore[operator]

    @property
    def per_hour(self) -> float | None:
        if not self.ok:
            return None
        hours = self.span.total_seconds() / 3600  # type: ignore[union-attr]
        return self.increase / hours if hours > 0 else None  # type: ignore[operator]

    @property
    def per_minute(self) -> float | None:
        ph = self.per_hour
        return None if ph is None else ph / 60

    @property
    def decreased(self) -> bool:
        inc = self.increase
        return inc is not None and inc < 0

    @property
    def span_differs(self) -> bool:
        """実際の時間差が名目の期間と 1 分以上ずれているか（表示の注記用）。"""
        if not self.ok or self.window is None:
            return False
        return abs((self.span - self.window).total_seconds()) >= 60  # type: ignore[operator]


def _missing(window, start_target, end_time, end_views, reason) -> Increase:
    return Increase(window, start_target, end_time, end_views, None, None, reason)


def increase_between(series: pd.DataFrame, start, end, tolerance: pd.Timedelta) -> Increase:
    """start 付近から end 付近までの増加数（両端とも最も近い取得データを使う）。"""
    start_ts, end_ts = _to_utc_ts(start), _to_utc_ts(end)
    window = end_ts - start_ts
    if series.empty:
        return _missing(window, start_ts, None, None, "データがありません")
    pts = resolve_points(series, [start_ts, end_ts], tolerance)
    base, last = pts.iloc[0], pts.iloc[1]
    if pd.isna(last["sample_time"]):
        return _missing(window, start_ts, None, None, "期間の終わり付近のデータがありません")
    end_time, end_views = last["sample_time"], int(last["view_count"])
    if pd.isna(base["sample_time"]):
        return _missing(window, start_ts, end_time, end_views, _shortage_reason(series, start_ts, tolerance))
    if base["sample_time"] >= end_time:
        return _missing(window, start_ts, end_time, end_views, "データ不足（期間内のデータが 1 件以下）")
    return Increase(window, start_ts, end_time, end_views, base["sample_time"], int(base["view_count"]))


def _shortage_reason(series: pd.DataFrame, start_ts: pd.Timestamp, tolerance: pd.Timedelta) -> str:
    first = series["timestamp"].iloc[0]
    if first > start_ts + tolerance:
        return f"データ不足（取得開始から{format_duration(series['timestamp'].iloc[-1] - first)}）"
    return "データ不足（期間の始まり付近のデータが欠けています）"


def increase_over(series: pd.DataFrame, window: pd.Timedelta, *, end=None,
                  tolerance: pd.Timedelta | None = None,
                  interval: pd.Timedelta | float | None = None) -> Increase:
    """直近 window の増加数。end を省くと、その動画の最新の取得データを終わりとする。"""
    tol = tolerance if tolerance is not None else tolerance_for(window, interval)
    if series.empty:
        return _missing(window, None, None, None, "データがありません")
    if end is None:
        end_ts = series["timestamp"].iloc[-1]
    else:
        end_ts = _to_utc_ts(end)
    return increase_between(series, end_ts - window, end_ts, tol)


def window_increases(series: pd.DataFrame, windows: Sequence[pd.Timedelta] = STANDARD_WINDOWS,
                     *, interval: pd.Timedelta | float | None = None) -> list[Increase]:
    return [increase_over(series, w, interval=interval) for w in windows]


# ---------------------------------------------------------------------- 時系列
def rolling_increase(series: pd.DataFrame, window: pd.Timedelta = pd.Timedelta(hours=1), *,
                     interval: pd.Timedelta | float | None = None) -> pd.DataFrame:
    """各取得時刻について「直前 window の増加数」と 1 時間あたりの速度を計算する。

    列: timestamp, view_count, base_time, increase, span_minutes, per_hour（計算できない時刻は NaN）
    """
    tol = tolerance_for(window, interval)
    if series.empty:
        return pd.DataFrame(columns=["timestamp", "view_count", "base_time", "increase",
                                     "span_minutes", "per_hour"])
    pts = resolve_points(series, series["timestamp"] - window, tol)
    out = series[["timestamp", "view_count"]].reset_index(drop=True).copy()
    # to_numpy() にするとタイムゾーンの情報が落ち、基準が 1 件も見つからない（取得開始から
    # 1 時間未満）ときに比較でエラーになる。Series のまま行番号で合わせて代入する
    out["base_time"] = pts["sample_time"].set_axis(out.index)
    valid = out["base_time"].notna() & (out["base_time"] < out["timestamp"])
    out["increase"] = (out["view_count"] - pts["view_count"]).where(valid)
    span = (out["timestamp"] - out["base_time"]).dt.total_seconds() / 60
    out["span_minutes"] = span.where(valid)
    out["per_hour"] = (out["increase"] / (out["span_minutes"] / 60)).where(valid)
    return out


def bucket_increases(series: pd.DataFrame, freq: pd.Timedelta, *, start=None, end=None,
                     tz: str = "UTC", interval: pd.Timedelta | float | None = None) -> pd.DataFrame:
    """時間帯ごと（例: 毎時 0 分区切り）の増加数。区切りは表示用タイムゾーン tz の時刻にそろえる。

    列: bucket_start, bucket_end（tz の日時）, increase, per_hour, partial（現在進行中の区間）, reason
    """
    columns = ["bucket_start", "bucket_end", "increase", "per_hour", "partial", "reason"]
    if series.empty:
        return pd.DataFrame(columns=columns)
    first, last = series["timestamp"].iloc[0], series["timestamp"].iloc[-1]
    start_ts = _to_utc_ts(start) if start is not None else first
    end_ts = min(_to_utc_ts(end), last) if end is not None else last
    if end_ts <= start_ts:
        return pd.DataFrame(columns=columns)
    tol = tolerance_for(freq, interval)
    local_start = start_ts.tz_convert(tz).floor(freq) if freq < pd.Timedelta(days=1) else \
        start_ts.tz_convert(tz).normalize()
    # 日付をまたぐ区切りは暦（夏時間を含む）に合わせる
    boundaries = pd.date_range(local_start, end_ts.tz_convert(tz) + freq, freq=freq)
    if len(boundaries) < 2:
        return pd.DataFrame(columns=columns)
    pts = resolve_points(series, boundaries.tz_convert("UTC"), tol)
    rows = []
    for i in range(len(boundaries) - 1):
        b0, b1 = boundaries[i], boundaries[i + 1]
        if b0.tz_convert("UTC") > end_ts:
            break
        base, fin = pts.iloc[i], pts.iloc[i + 1]
        partial = b1.tz_convert("UTC") > last
        if partial:  # 進行中の区間は最新データまでの途中経過
            fin = pd.Series({"sample_time": last, "view_count": float(series["view_count"].iloc[-1])})
        reason = ""
        if pd.isna(base["sample_time"]) or pd.isna(fin["sample_time"]):
            reason = "データ不足"
        elif base["sample_time"] >= fin["sample_time"]:
            reason = "データ不足"
        if reason:
            rows.append((b0, b1, np.nan, np.nan, partial, reason))
            continue
        inc = float(fin["view_count"] - base["view_count"])
        hours = (fin["sample_time"] - base["sample_time"]).total_seconds() / 3600
        rows.append((b0, b1, inc, inc / hours if hours > 0 else np.nan, partial, ""))
    return pd.DataFrame(rows, columns=columns)


# ---------------------------------------------------------------------- 速度の比較
@dataclass(frozen=True)
class SpeedComparison:
    recent: Increase
    previous: Increase
    change_percent: float | None
    trend: str  # 上昇 / 低下 / 横ばい / 判定不可


def classify_trend(recent_rate: float | None, previous_rate: float | None,
                   threshold_percent: float = TREND_THRESHOLD_PERCENT) -> tuple[str, float | None]:
    if recent_rate is None or previous_rate is None:
        return "判定不可", None
    if previous_rate <= 0:
        if recent_rate > 0:
            return "上昇", None
        if recent_rate == 0 and previous_rate == 0:
            return "横ばい", 0.0
        return ("低下", None) if recent_rate < previous_rate else ("横ばい", None)
    change = (recent_rate / previous_rate - 1) * 100
    if change > threshold_percent:
        return "上昇", change
    if change < -threshold_percent:
        return "低下", change
    return "横ばい", change


def compare_speed(series: pd.DataFrame, period: pd.Timedelta = pd.Timedelta(hours=1), *,
                  interval: pd.Timedelta | float | None = None,
                  threshold_percent: float = TREND_THRESHOLD_PERCENT) -> SpeedComparison:
    """直近 period と、その前の period の増加速度を比べる。"""
    recent = increase_over(series, period, interval=interval)
    if series.empty:
        previous = _missing(period, None, None, None, "データがありません")
    else:
        end = series["timestamp"].iloc[-1]
        previous = increase_between(series, end - 2 * period, end - period,
                                    tolerance_for(period, interval))
    trend, change = classify_trend(recent.per_hour, previous.per_hour, threshold_percent)
    return SpeedComparison(recent, previous, change, trend)


def speed_profile(series: pd.DataFrame, *, interval: pd.Timedelta | float | None = None) -> pd.DataFrame:
    """直近の速度と過去の平均速度（いずれも 1 時間あたり）を並べた表（グラフ 4 用）。"""
    hour = pd.Timedelta(hours=1)
    cmp_ = compare_speed(series, hour, interval=interval)
    items: list[tuple[str, Increase]] = [
        ("直近1時間", cmp_.recent),
        ("その前の1時間", cmp_.previous),
    ]
    for hours in (3, 6, 24):
        items.append((f"直近{hours}時間の平均", increase_over(series, pd.Timedelta(hours=hours), interval=interval)))
    rows = []
    for label, inc in items:
        rows.append({
            "label": label,
            "per_hour": inc.per_hour,
            "increase": inc.increase,
            "span": format_duration(inc.span) if inc.ok else "-",
            "reason": inc.reason,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------- 目標再生数
def next_goal(current: int, goals: Sequence[int]) -> int | None:
    """まだ到達していない、いちばん近い目標再生数（すべて到達済みなら None）。"""
    return next((g for g in sorted(goals) if g > current), None)


def time_to_goal(current: int, goal: int, per_hour: float | None) -> pd.Timedelta | None:
    """今の速度（1 時間あたりの増加数）のままなら、目標まであとどのくらいかかるか。

    すでに到達していれば 0、速度が分からない・増えていない（0 以下）なら None。
    """
    if current >= goal:
        return pd.Timedelta(0)
    if per_hour is None or pd.isna(per_hour) or per_hour <= 0:
        return None
    return pd.Timedelta(hours=(goal - current) / per_hour)


# ---------------------------------------------------------------------- データの品質
def find_gaps(series: pd.DataFrame, expected_interval: pd.Timedelta | float) -> pd.DataFrame:
    """取得間隔が想定の 2 倍 + 1 分を超えて空いた箇所（停止・通信障害など）。"""
    interval = _td(expected_interval)
    limit = interval * 2 + pd.Timedelta(minutes=1)
    if len(series) < 2:
        return pd.DataFrame(columns=["gap_start", "gap_end", "duration"])
    ts = series["timestamp"].reset_index(drop=True)
    diffs = ts.diff()
    mask = diffs > limit
    return pd.DataFrame({
        "gap_start": ts.shift(1)[mask].reset_index(drop=True),
        "gap_end": ts[mask].reset_index(drop=True),
        "duration": diffs[mask].reset_index(drop=True),
    })


def find_decreases(series: pd.DataFrame) -> pd.DataFrame:
    """前回の取得より視聴回数が減った箇所。"""
    if len(series) < 2:
        return pd.DataFrame(columns=["timestamp", "previous", "current", "change"])
    s = series.reset_index(drop=True)
    prev = s["view_count"].shift(1)
    mask = s["view_count"] < prev
    return pd.DataFrame({
        "timestamp": s.loc[mask, "timestamp"].reset_index(drop=True),
        "previous": prev[mask].astype("int64").reset_index(drop=True),
        "current": s.loc[mask, "view_count"].reset_index(drop=True),
        "change": (s.loc[mask, "view_count"] - prev[mask]).astype("int64").reset_index(drop=True),
    })


def insert_gap_breaks(frame: pd.DataFrame, time_col: str, max_gap: pd.Timedelta,
                      value_cols: Sequence[str]) -> pd.DataFrame:
    """グラフで欠損区間を線でつながないよう、間が空いた所に空の行をはさむ。"""
    if len(frame) < 2:
        return frame
    f = frame.sort_values(time_col).reset_index(drop=True)
    gaps = f[time_col].diff() > max_gap
    if not gaps.any():
        return f
    breaks = f.loc[gaps, [time_col]].copy()
    breaks[time_col] = breaks[time_col] - pd.Timedelta(seconds=1)
    for col in value_cols:
        breaks[col] = np.nan
    merged = pd.concat([f, breaks], ignore_index=True)
    return merged.sort_values(time_col, kind="stable").reset_index(drop=True)
