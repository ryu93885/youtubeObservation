"""API キーがなくてもダッシュボードやテストを試せるデモデータ。

実際の history.csv と同じ形式の表を作る。ダッシュボードはメモリー上で使うので、
data フォルダーの実データには一切触れない。

ファイルとして見たいとき:
    python demo_data.py            → data/demo/ に history.csv などを書き出す
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import analytics

DEMO_VIDEOS = {
    "demoVideo01": {"name": "新曲MV（デモ）", "title": "【MV】新曲 Official Music Video",
                    "channel_title": "デモチャンネルA", "threshold": 10000, "goal": 800000},
    "demoVideo02": {"name": "ライブ配信アーカイブ（デモ）", "title": "【歌枠】3周年記念ライブ",
                    "channel_title": "デモチャンネルA", "threshold": 1000},
    "demoVideo03": {"name": "歌ってみた（デモ）", "title": "【歌ってみた】カバー曲",
                    "channel_title": "デモチャンネルB", "threshold": 500},
}


@dataclass
class DemoBundle:
    history: pd.DataFrame
    notifications: pd.DataFrame
    catalog: dict[str, dict]
    names: dict[str, str]


def _daily_cycle(local_hours: np.ndarray) -> np.ndarray:
    """日本の夜（21 時ごろ）に多く、明け方（5 時ごろ）に少ない視聴のリズム。"""
    return 1.0 + 0.55 * np.cos((local_hours - 21.0) / 24.0 * 2 * np.pi)


def generate_history(now: datetime | None = None, *, days: float = 3.0, interval_minutes: int = 5,
                     seed: int = 7) -> pd.DataFrame:
    """3 本の動画のデモ履歴（5 分間隔）。欠損・減少・急な伸びを意図的に含める。"""
    rng = np.random.default_rng(seed)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = pd.Timestamp(now).floor(f"{interval_minutes}min")
    times = pd.date_range(end - pd.Timedelta(days=days), end, freq=f"{interval_minutes}min")
    hours_since_start = (times - times[0]).total_seconds().to_numpy() / 3600
    local_hours = times.tz_convert("Asia/Tokyo").hour.to_numpy() + times.tz_convert("Asia/Tokyo").minute.to_numpy() / 60
    cycle = _daily_cycle(local_hours)
    step_h = interval_minutes / 60
    frames = []

    # 1) 公開直後の新曲 MV: 伸びがだんだん落ち着き、途中で一度バズる
    age = hours_since_start + 6
    rate = 30000 / (1 + age / 10) * cycle
    spike_at = hours_since_start[-1] * 0.62
    rate += 25000 * np.exp(-((hours_since_start - spike_at) / 1.5) ** 2)
    frames.append(("demoVideo01", 180000, rate))

    # 2) 安定して伸びる配信アーカイブ: 途中で監視が止まった時間と、再生数の減少を含む
    rate = 700 * cycle
    frames.append(("demoVideo02", 1250000, rate))

    # 3) 小さめの歌ってみた: 直近 2 時間で急に伸びている
    rate = 40 * cycle
    rate += np.where(hours_since_start > hours_since_start[-1] - 2, 900, 0)
    frames.append(("demoVideo03", 98000, rate))

    rows = []
    for video_id, start_views, per_hour in frames:
        noise = rng.normal(1.0, 0.15, size=len(times)).clip(0.3, None)
        increments = rng.poisson(np.maximum(per_hour * step_h * noise, 0))
        views = start_views + np.cumsum(increments)
        likes = (views * 0.045).astype(int)
        comments = (views * 0.002).astype(int)
        df = pd.DataFrame({"timestamp": times, "video_id": video_id, "view_count": views,
                           "like_count": likes, "comment_count": comments})
        if video_id == "demoVideo02":
            # 監視プログラムが 2 時間止まっていた想定の欠損
            gap_start = times[len(times) // 3]
            df = df[(df["timestamp"] < gap_start) | (df["timestamp"] >= gap_start + pd.Timedelta(hours=2))]
            # YouTube 側の集計見直しで 600 回減った想定
            drop_at = times[int(len(times) * 0.8)]
            df.loc[df["timestamp"] >= drop_at, "view_count"] -= 600
        rows.append(df)
    history = pd.concat(rows, ignore_index=True)
    history["timestamp"] = history["timestamp"].dt.as_unit("ns")
    history["view_count"] = history["view_count"].astype("int64")
    history["like_count"] = history["like_count"].astype("Int64")
    history["comment_count"] = history["comment_count"].astype("Int64")
    return history.sort_values(["video_id", "timestamp"]).reset_index(drop=True)


def simulate_notifications(history: pd.DataFrame, *, window_minutes: int = 60,
                           thresholds: dict[str, int] | None = None, default_threshold: int = 1000,
                           cooldown_minutes: int = 60, interval_seconds: int = 300) -> pd.DataFrame:
    """監視プログラムと同じ計算（analytics.rolling_increase）で、通知が出たはずの時刻を求める。"""
    records = []
    window = pd.Timedelta(minutes=window_minutes)
    if thresholds is None:
        thresholds = {vid: int(v["threshold"]) for vid, v in DEMO_VIDEOS.items()}
    for video_id in history["video_id"].unique():
        threshold = thresholds.get(video_id, default_threshold)
        series = analytics.video_series(history, video_id)
        rolling = analytics.rolling_increase(series, window, interval=interval_seconds)
        last_sent = None
        for row in rolling.itertuples(index=False):
            if pd.isna(row.increase) or row.increase < threshold:
                continue
            if last_sent is not None and row.timestamp - last_sent < pd.Timedelta(minutes=cooldown_minutes):
                continue
            last_sent = row.timestamp
            records.append({
                "timestamp": row.timestamp, "video_id": video_id, "kind": "surge",
                "window_minutes": window_minutes, "increase": float(row.increase),
                "threshold": float(threshold), "view_count": float(row.view_count),
                "milestone": np.nan, "delivered": True,
                "message": f"📈 再生数が伸びています: {DEMO_VIDEOS.get(video_id, {}).get('name', video_id)}",
            })
    df = pd.DataFrame(records, columns=["timestamp", "video_id", "kind", "window_minutes", "increase",
                                        "threshold", "view_count", "milestone", "delivered", "message"])
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.as_unit("ns")
    return df.sort_values("timestamp").reset_index(drop=True)


def demo_bundle(now: datetime | None = None) -> DemoBundle:
    history = generate_history(now)
    notifications = simulate_notifications(history)
    catalog = {vid: {"title": v["title"], "channel_title": v["channel_title"]} for vid, v in DEMO_VIDEOS.items()}
    names = {vid: v["name"] for vid, v in DEMO_VIDEOS.items()}
    return DemoBundle(history, notifications, catalog, names)


def write_demo_files(out_dir: Path, now: datetime | None = None) -> None:
    import json

    from data_manager import format_timestamp

    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = demo_bundle(now)
    hist = bundle.history.copy()
    hist["timestamp"] = [format_timestamp(t.to_pydatetime()) for t in hist["timestamp"]]
    hist.to_csv(out_dir / "history.csv", index=False, lineterminator="\n")
    notes = bundle.notifications.copy()
    notes["timestamp"] = [format_timestamp(t.to_pydatetime()) for t in notes["timestamp"]]
    notes["delivered"] = notes["delivered"].map(lambda v: str(bool(v)).lower())
    notes.to_csv(out_dir / "notifications.csv", index=False, lineterminator="\n")
    (out_dir / "videos.json").write_text(json.dumps(bundle.catalog, ensure_ascii=False, indent=2),
                                         encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="デモ用の履歴データを書き出します（実データには触れません）")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "data" / "demo"),
                        help="書き出し先フォルダー（既定: data/demo）")
    args = parser.parse_args()
    out = Path(args.out)
    write_demo_files(out)
    print(f"デモデータを書き出しました: {out}")
    print(f"ダッシュボードで見るには: streamlit run dashboard.py -- --data-dir \"{out}\"")


if __name__ == "__main__":
    main()
