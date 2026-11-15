"""YouTube View Monitor — 可視化ダッシュボード（Streamlit）。

起動:
    streamlit run dashboard.py
    （ブラウザで http://localhost:8501 が開く）

- 監視プログラム（main.py）が保存した CSV を読むだけで、YouTube API には一切アクセスしない。
  そのため、ダッシュボードを何枚開いても API のクォータは消費せず、監視が二重に動くこともない。
- 増加数・増加速度の計算は analytics.py（通知の判定と同じ関数）を使う。
- 監視プログラムと同時に起動してよい。CSV が更新されると、自動更新で画面に反映される。

オプション（streamlit run dashboard.py -- の後ろに書く）:
    --config PATH     設定ファイル
    --data-dir PATH   データフォルダー（例: python demo_data.py で書き出したフォルダー）
    --demo            デモデータを表示した状態で起動する
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import analytics as A
from config import AppConfig, NotificationRule, load_config_for_dashboard
from data_manager import DataPaths, read_history_csv, read_json, read_notifications_csv
from demo_data import DEMO_VIDEOS, demo_bundle
from youtube_api import thumbnail_url, video_url

# ---------------------------------------------------------------------- 見た目の設定
# 系列の色は固定の順番で割り当てる（色覚の多様性に配慮して検証済みの並び）
PALETTE = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"],
}
INK = {
    "light": {"primary": "#0b0b0b", "secondary": "#52514e", "muted": "#898781",
              "grid": "#e1e0d9", "axis": "#c3c2b7", "surface": "#fcfcfb", "negative": "#e34948"},
    "dark": {"primary": "#f0efec", "secondary": "#c3c2b7", "muted": "#898781",
             "grid": "#2c2c2a", "axis": "#383835", "surface": "#1a1a19", "negative": "#e66767"},
}
PERIODS: dict[str, pd.Timedelta | None] = {
    "6時間": pd.Timedelta(hours=6), "12時間": pd.Timedelta(hours=12), "24時間": pd.Timedelta(hours=24),
    "3日": pd.Timedelta(days=3), "7日": pd.Timedelta(days=7), "30日": pd.Timedelta(days=30),
    "全期間": None, "日付を指定": None,
}
BUCKETS = {"15分": pd.Timedelta(minutes=15), "30分": pd.Timedelta(minutes=30), "1時間": pd.Timedelta(hours=1),
           "3時間": pd.Timedelta(hours=3), "1日": pd.Timedelta(days=1)}
WINDOW_LABELS = ["過去1時間", "過去3時間", "過去6時間", "過去12時間", "過去24時間"]
TREND_ICON = {"上昇": "↑", "低下": "↓", "横ばい": "→", "判定不可": "・"}
MAX_COMPARE = 8


# ---------------------------------------------------------------------- 起動オプション
def parse_cli(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config")
    parser.add_argument("--data-dir")
    parser.add_argument("--demo", action="store_true")
    args, _unknown = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    return args


# ---------------------------------------------------------------------- データの読み込み
def _file_key(path: Path) -> tuple[int, int]:
    try:
        st_ = path.stat()
        return st_.st_mtime_ns, st_.st_size
    except OSError:
        return 0, 0


@st.cache_data(show_spinner=False, max_entries=4)
def _read_history_cached(path: str, mtime_ns: int, size: int):
    df, report = read_history_csv(Path(path))
    return df, report.messages()


@st.cache_data(show_spinner=False, max_entries=4)
def _read_notifications_cached(path: str, mtime_ns: int, size: int) -> pd.DataFrame:
    return read_notifications_csv(Path(path))


@st.cache_data(show_spinner=False, max_entries=2)
def _demo_cached(slot: str):
    return demo_bundle()


@dataclass
class Context:
    config: AppConfig
    demo: bool
    history: pd.DataFrame
    notifications: pd.DataFrame
    catalog: dict
    status: dict | None
    messages: list[str]
    names: dict[str, str]
    order: list[str]
    theme: str
    data_dir: Path | None = None
    now: pd.Timestamp = field(default_factory=lambda: pd.Timestamp.now(tz="UTC"))
    _series: dict[str, pd.DataFrame] | None = field(default=None, repr=False)

    def series(self, video_id: str) -> pd.DataFrame:
        """動画ごとの時系列（履歴全体を 1 回だけ動画ごとに分け、使い回す）。"""
        if self._series is None:
            self._series = {}
            if not self.history.empty:
                for vid, group in self.history.groupby("video_id", sort=False):
                    self._series[str(vid)] = A.prepare_series(group)
        return self._series.get(video_id, A.video_series(self.history.iloc[0:0], video_id))

    def present(self) -> list[str]:
        """データのある動画（表示順）。"""
        self.series("")  # 分割を済ませる
        return [v for v in self.order if v in (self._series or {})]

    @property
    def tz(self) -> str:
        return self.config.timezone

    @property
    def interval(self) -> int:
        return self.config.interval_seconds

    def name(self, video_id: str) -> str:
        return self.names.get(video_id, video_id)

    def palette(self) -> list[str]:
        return PALETTE[self.theme]

    def ink(self) -> dict[str, str]:
        return INK[self.theme]


def build_names(config: AppConfig, catalog: dict, ids: list[str], demo_names: dict | None = None) -> dict[str, str]:
    names: dict[str, str] = {}
    for vid in ids:
        cfg = config.video(vid)
        names[vid] = ((demo_names or {}).get(vid) or (cfg.name if cfg else "")
                      or str(catalog.get(vid, {}).get("title", "")) or vid)
    counts = pd.Series(list(names.values())).value_counts()
    for vid, n in list(names.items()):
        if counts.get(n, 0) > 1:
            names[vid] = f"{n}（{vid}）"
    return names


def load_context(args: argparse.Namespace, use_demo: bool, theme: str) -> Context:
    config, messages = load_config_for_dashboard(args.config)
    if use_demo:
        slot = pd.Timestamp.now(tz="UTC").floor("5min").isoformat()
        bundle = _demo_cached(slot)
        ids = list(bundle.names)
        names = build_names(config, bundle.catalog, ids, bundle.names)
        return Context(config=config, demo=True, history=bundle.history, notifications=bundle.notifications,
                       catalog=bundle.catalog, status=None, messages=[], names=names, order=ids, theme=theme)

    data_dir = Path(args.data_dir).expanduser().resolve() if args.data_dir else config.data_dir
    paths = DataPaths(data_dir)
    history, load_messages = _read_history_cached(str(paths.history), *_file_key(paths.history))
    notifications = _read_notifications_cached(str(paths.notifications), *_file_key(paths.notifications))
    catalog = read_json(paths.videos, {}) or {}
    status = read_json(paths.status, None)
    configured = [v.video_id for v in config.videos]
    others = sorted(set(history["video_id"].unique()) - set(configured)) if not history.empty else []
    ids = configured + others
    names = build_names(config, catalog, ids)
    return Context(config=config, demo=False, history=history, notifications=notifications, catalog=catalog,
                   status=status if isinstance(status, dict) else None,
                   messages=messages + list(load_messages), names=names, order=ids, theme=theme,
                   data_dir=data_dir)


# ---------------------------------------------------------------------- 表示用の小道具
def to_local(ts: pd.Series | pd.Timestamp, tz: str):
    """UTC の日時を表示用タイムゾーンの「時計の時刻」（タイムゾーン情報なし）にする。"""
    if isinstance(ts, pd.Series):
        return ts.dt.tz_convert(tz).dt.tz_localize(None)
    return ts.tz_convert(tz).tz_localize(None)


def fmt_time(ts: pd.Timestamp | None, tz: str) -> str:
    if ts is None or pd.isna(ts):
        return "-"
    return pd.Timestamp(ts).tz_convert(tz).strftime("%Y/%m/%d %H:%M")


def fmt_age(ts: pd.Timestamp | None, now: pd.Timestamp) -> str:
    if ts is None or pd.isna(ts):
        return ""
    delta = now - ts
    if delta < pd.Timedelta(minutes=1):
        return "たった今"
    return f"{A.format_duration(delta)}前"


def fmt_int(value: float | int | None, sign: bool = False) -> str:
    if value is None or pd.isna(value):
        return "データ不足"
    return f"{int(round(value)):+,}回" if sign else f"{int(round(value)):,}回"


def fmt_rate(value: float | None, unit: str = "時") -> str:
    if value is None or pd.isna(value):
        return "データ不足"
    if abs(value) >= 100:
        return f"{value:,.0f}回/{unit}"
    return f"{value:,.1f}回/{unit}"


def style_figure(fig: go.Figure, ctx: Context, title: str, x_title: str, y_title: str, *,
                 height: int = 380, legend: bool = True, time_axis: bool = True,
                 hovermode: str = "x unified") -> go.Figure:
    ink = ctx.ink()
    fig.update_layout(
        title=dict(text=title, x=0, xanchor="left", font=dict(size=16, color=ink["primary"])),
        height=height, margin=dict(l=8, r=16, t=64, b=8), hovermode=hovermode, showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1,
                    font=dict(color=ink["secondary"])),
        uirevision="keep",  # 自動更新しても拡大・縮小の状態を保つ
        barcornerradius=4, bargap=0.3,
        font=dict(family='system-ui, -apple-system, "Segoe UI", "Yu Gothic UI", "Meiryo", sans-serif'),
    )
    fig.update_xaxes(title_text=x_title, showgrid=False, linecolor=ink["axis"], ticks="outside",
                     tickcolor=ink["axis"], tickfont=dict(color=ink["muted"]),
                     title_font=dict(color=ink["secondary"]))
    if time_axis:
        fig.update_xaxes(hoverformat="%Y/%m/%d %H:%M", tickformatstops=[
            dict(dtickrange=[None, 86_400_000], value="%m/%d %H:%M"),
            dict(dtickrange=[86_400_000, None], value="%Y/%m/%d"),
        ])
    fig.update_yaxes(title_text=y_title, gridcolor=ink["grid"], gridwidth=1, zeroline=True,
                     zerolinecolor=ink["axis"], tickformat=",", tickfont=dict(color=ink["muted"]),
                     title_font=dict(color=ink["secondary"]), linecolor=ink["axis"])
    return fig


def show_chart(fig: go.Figure, key: str) -> None:
    st.plotly_chart(fig, key=key, config={"displaylogo": False, "locale": "ja"})


def period_bounds(choice: str, custom: tuple | None, ctx: Context) -> tuple[pd.Timestamp | None, pd.Timestamp]:
    end = ctx.now
    if choice == "日付を指定" and custom:
        start_d, end_d = (custom + (custom[0],))[:2] if len(custom) == 1 else custom
        start = pd.Timestamp(start_d).tz_localize(ctx.tz).tz_convert("UTC")
        end = (pd.Timestamp(end_d) + pd.Timedelta(days=1)).tz_localize(ctx.tz).tz_convert("UTC")
        return start, end
    span = PERIODS.get(choice)
    return (end - span if span is not None else None), end


def clip(series: pd.DataFrame, start: pd.Timestamp | None, end: pd.Timestamp, col: str = "timestamp") -> pd.DataFrame:
    if series.empty:
        return series
    mask = series[col] <= end
    if start is not None:
        mask &= series[col] >= start
    return series[mask]


def set_time_range(fig: go.Figure, ctx: Context, start: pd.Timestamp | None, end: pd.Timestamp) -> None:
    """横軸を表示期間にそろえる（全期間のときはデータに合わせて自動）。"""
    if start is not None:
        fig.update_xaxes(range=[to_local(start, ctx.tz), to_local(end, ctx.tz)])


def gap_limit(ctx: Context) -> pd.Timedelta:
    return pd.Timedelta(seconds=ctx.interval * 2) + pd.Timedelta(minutes=1)


# ---------------------------------------------------------------------- グラフ
def fig_views(ctx: Context, series: pd.DataFrame, notes: pd.DataFrame, title: str) -> go.Figure:
    ink, color = ctx.ink(), ctx.palette()[0]
    s = series.assign(time=to_local(series["timestamp"], ctx.tz))
    s = A.insert_gap_breaks(s, "time", gap_limit(ctx), ["view_count"])
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=s["time"], y=s["view_count"], mode="lines", name="視聴回数",
                             line=dict(width=2, color=color), connectgaps=False,
                             hovertemplate="%{y:,}回<extra>視聴回数</extra>"))
    if not notes.empty:
        n = notes.assign(time=to_local(notes["timestamp"], ctx.tz))
        labels = [("節目 " + f"{int(m):,}回") if k == "milestone" and pd.notna(m) else
                  ("目標 " + f"{int(m):,}回 達成") if k == "goal" and pd.notna(m) else
                  (f"+{int(i):,}回 / {int(w)}分" if pd.notna(i) and pd.notna(w) else "通知")
                  for k, m, i, w in zip(n["kind"], n["milestone"], n["increase"], n["window_minutes"])]
        fig.add_trace(go.Scatter(x=n["time"], y=n["view_count"], mode="markers", name="通知した時点",
                                 marker=dict(size=11, symbol="diamond", color=ctx.palette()[1],
                                             line=dict(width=2, color=ink["surface"])),
                                 customdata=labels, hovertemplate="通知: %{customdata}<extra></extra>"))
    style_figure(fig, ctx, title, "日時", "視聴回数（回）", legend=not notes.empty)
    return fig


def fig_window_bars(ctx: Context, incs: list[A.Increase]) -> go.Figure:
    ink, color = ctx.ink(), ctx.palette()[0]
    values = [inc.increase if inc.ok else None for inc in incs]
    colors = [ink["negative"] if (v is not None and v < 0) else color for v in values]
    texts = [fmt_int(v, sign=True) if v is not None else "データ不足" for v in values]
    custom = [[A.format_duration(inc.span) if inc.ok else "-", fmt_rate(inc.per_hour), inc.reason or "-"]
              for inc in incs]
    fig = go.Figure(go.Bar(
        x=WINDOW_LABELS, y=[v if v is not None else 0 for v in values], marker_color=colors,
        text=texts, textposition="outside", textfont=dict(color=ink["secondary"]), cliponaxis=False,
        customdata=custom,
        hovertemplate="%{x}<br>増加数: %{text}<br>実際の時間差: %{customdata[0]}<br>"
                      "1時間あたり: %{customdata[1]}<extra></extra>",
    ))
    style_figure(fig, ctx, "期間ごとの増加数（最新の取得時点から）", "期間", "増加数（回）",
                 legend=False, time_axis=False, hovermode="closest", height=360)
    known = [v for v in values if v is not None]
    if known:  # 棒の上の数値が欄外にはみ出さないよう余白をとる
        top, bottom = max(known + [0]), min(known + [0])
        pad = (top - bottom or 1) * 0.18
        fig.update_yaxes(range=[bottom - (pad if bottom < 0 else 0), top + pad])
    return fig


def fig_buckets(ctx: Context, buckets: pd.DataFrame, label: str) -> go.Figure:
    ink, color = ctx.ink(), ctx.palette()[0]
    b = buckets.assign(start=to_local(buckets["bucket_start"].dt.tz_convert("UTC"), ctx.tz))
    colors = [ink["negative"] if (pd.notna(v) and v < 0) else color for v in b["increase"]]
    opacity = [0.45 if p else 1.0 for p in b["partial"]]
    custom = [["途中経過（この区間はまだ終わっていません）" if p else "", fmt_rate(r)]
              for p, r in zip(b["partial"], b["per_hour"])]
    fig = go.Figure(go.Bar(
        x=b["start"], y=b["increase"], marker=dict(color=colors, opacity=opacity), name="増加数",
        customdata=custom,
        hovertemplate="%{x|%m/%d %H:%M}から<br>増加数: %{y:+,}回<br>1時間あたり: %{customdata[1]}"
                      "<br>%{customdata[0]}<extra></extra>",
    ))
    style_figure(fig, ctx, f"時間帯ごとの増加数（{label}ごと）", "時間帯の開始時刻", "増加数（回）",
                 legend=False, hovermode="closest", height=360)
    return fig


def fig_rolling(ctx: Context, rolling: pd.DataFrame, threshold_per_hour: float | None,
                threshold_label: str, average_per_hour: float | None) -> go.Figure:
    ink, color = ctx.ink(), ctx.palette()[0]
    r = rolling.assign(time=to_local(rolling["timestamp"], ctx.tz))
    r = A.insert_gap_breaks(r, "time", gap_limit(ctx), ["per_hour"])
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=r["time"], y=r["per_hour"], mode="lines", name="1時間あたりの増加数",
                             line=dict(width=2, color=color), connectgaps=False,
                             hovertemplate="%{y:,.0f}回/時<extra>増加速度</extra>"))
    if average_per_hour is not None:
        fig.add_hline(y=average_per_hour, line=dict(color=ink["muted"], width=1),
                      annotation=dict(text=f"24時間平均 {fmt_rate(average_per_hour)}", font=dict(color=ink["secondary"])),
                      annotation_position="top left")
    if threshold_per_hour is not None:
        fig.add_hline(y=threshold_per_hour, line=dict(color=ink["secondary"], width=1, dash="dash"),
                      annotation=dict(text=threshold_label, font=dict(color=ink["secondary"])),
                      annotation_position="top right")
    style_figure(fig, ctx, "1時間あたりの視聴回数増加数の推移（直前1時間の増加数）", "日時",
                 "増加数（回/時）", legend=False)
    return fig


def fig_speed(ctx: Context, profile: pd.DataFrame) -> go.Figure:
    ink, color = ctx.ink(), ctx.palette()[0]
    p = profile.iloc[::-1]  # 上から「直近1時間」が来るように
    colors = [color if lab == "直近1時間" else ink["muted"] for lab in p["label"]]
    values = [v if pd.notna(v) else 0 for v in p["per_hour"]]
    texts = [fmt_rate(v) for v in p["per_hour"]]
    fig = go.Figure(go.Bar(
        y=p["label"], x=values, orientation="h", marker_color=colors, text=texts, textposition="outside",
        textfont=dict(color=ink["secondary"]), cliponaxis=False,
        customdata=list(zip(p["span"], p["reason"].replace("", "-"))),
        hovertemplate="%{y}: %{text}<br>実際の時間差: %{customdata[0]}<extra></extra>",
    ))
    style_figure(fig, ctx, "増加速度の比較（直近と過去・1時間あたり）", "増加数（回/時）", "",
                 legend=False, time_axis=False, hovermode="closest", height=360)
    top, bottom = max(values + [0]), min(values + [0])
    pad = (top - bottom or 1) * 0.3
    fig.update_xaxes(showgrid=True, gridcolor=ink["grid"], tickformat=",",
                     range=[bottom - (pad if bottom < 0 else 0), top + pad])
    fig.update_yaxes(showgrid=False, zeroline=False, tickfont=dict(color=ink["secondary"]))
    return fig


def fig_compare(ctx: Context, frames: list[tuple[str, str, pd.DataFrame]], mode: str) -> go.Figure:
    ink = ctx.ink()
    fig = go.Figure()
    y_col = {"視聴回数": "view_count", "期間内の増加数": "gain", "1時間あたりの増加数": "per_hour"}[mode]
    y_title = {"視聴回数": "視聴回数（回）", "期間内の増加数": "増加数（回）",
               "1時間あたりの増加数": "増加数（回/時）"}[mode]
    subtitle = {"視聴回数": "", "期間内の増加数": "（表示期間の最初の時点からの増加）",
                "1時間あたりの増加数": "（各時点の直前1時間の増加数）"}[mode]
    unit = "回/時" if mode == "1時間あたりの増加数" else "回"
    for name, color, df in frames:
        if df.empty:
            continue
        d = df.assign(time=to_local(df["timestamp"], ctx.tz))
        d = A.insert_gap_breaks(d, "time", gap_limit(ctx), [y_col])
        fig.add_trace(go.Scatter(x=d["time"], y=d[y_col], mode="lines", name=name,
                                 line=dict(width=2, color=color), connectgaps=False,
                                 hovertemplate=f"%{{y:,.0f}}{unit}<extra>{name}</extra>"))
        if len(frames) <= 4:  # 系列が少ないときは線の端に名前を添える
            last = d.dropna(subset=[y_col]).tail(1)
            if not last.empty:
                fig.add_annotation(x=last["time"].iloc[0], y=last[y_col].iloc[0], text=name, showarrow=False,
                                   xanchor="left", xshift=6, font=dict(color=ink["secondary"], size=12))
    style_figure(fig, ctx, f"動画の比較: {mode}{subtitle}", "日時", y_title, height=440)
    fig.update_layout(margin=dict(r=140 if len(frames) <= 4 else 16))
    return fig


# ---------------------------------------------------------------------- 画面の部品
def render_status(ctx: Context) -> None:
    if ctx.demo:
        st.info("🧪 デモデータを表示しています。実際のデータや YouTube API には触れていません。"
                "左のメニューで「実データ」に切り替えられます。")
        return
    for msg in ctx.messages:
        st.warning(msg)
    status = ctx.status
    if not status:
        st.warning("⚪ 監視プログラムの状態ファイル（data/status.json）がありません。"
                   "監視を始めるには、別のウィンドウで `python main.py` を実行してください。")
        return
    updated = pd.to_datetime(status.get("updated_at"), utc=True, errors="coerce")
    last_ok = pd.to_datetime(status.get("last_success_at"), utc=True, errors="coerce")
    interval = int(status.get("interval_seconds") or ctx.interval)
    state = status.get("state", "")
    alive = pd.notna(updated) and ctx.now - updated <= pd.Timedelta(seconds=interval * 2 + 120)
    quota = status.get("quota") or {}
    quota_text = f"本日の API 使用量 {int(quota.get('used', 0)):,} / {int(quota.get('limit', 10000)):,} ユニット"
    last_text = f"最終取得 {fmt_time(last_ok, ctx.tz)}（{fmt_age(last_ok, ctx.now)}）" if pd.notna(last_ok) else "まだ取得していません"
    if state == "stopped":
        st.warning(f"⚪ 監視プログラムは停止しています。{last_text}。再開するには `python main.py` を実行してください。")
    elif not alive:
        st.warning(f"⚪ 監視プログラムから応答がありません（停止したか、PC がスリープしていた可能性があります）。"
                   f"{last_text}。動いていなければ `python main.py` を実行してください。")
    elif state == "waiting_quota":
        st.warning(f"🟠 1 日のクォータを使い切ったため待機中です（太平洋時間 0 時に再開）。{last_text}")
    elif state == "error":
        st.error(f"🔴 取得でエラーが続いています（{status.get('consecutive_failures', 0)} 回）: "
                 f"{status.get('last_error', '')}　{last_text}")
    else:
        nxt = pd.to_datetime(status.get("next_run_at"), utc=True, errors="coerce")
        nxt_text = f"・次回 {fmt_time(nxt, ctx.tz)[-5:]}" if pd.notna(nxt) else ""
        st.success(f"🟢 監視中（{interval // 60} 分ごと）・{last_text}{nxt_text}・{quota_text}")
    if status.get("pending_rows"):
        st.warning(f"history.csv に書き込めず {status['pending_rows']} 行を保留中です。"
                   "Excel などで開いている場合は閉じてください。")
    missing = status.get("missing_videos") or []
    if missing:
        st.warning("取得できなかった動画（非公開・削除・URL の誤りの可能性）: "
                   + "、".join(ctx.name(v) for v in missing))


def render_empty(ctx: Context) -> None:
    st.info(
        "まだ視聴回数のデータがありません。\n\n"
        "1. `config.json` に API キーと動画の URL を書く\n"
        "2. `python main.py --check` で確認する\n"
        "3. `python main.py` で監視を始める（5 分ごとにデータが増えます）\n\n"
        "先に画面を試したいときは、左のメニューで「デモデータ」を選んでください。"
    )


def table_expander(df: pd.DataFrame, label: str = "表で見る") -> None:
    with st.expander(label):
        st.dataframe(df, hide_index=True)


def summarize(ctx: Context, video_id: str) -> dict:
    series = ctx.series(video_id)
    incs = A.window_increases(series, interval=ctx.interval)
    cmp_ = A.compare_speed(series, interval=ctx.interval)
    last_ts = series["timestamp"].iloc[-1] if not series.empty else None
    return {
        "series": series, "incs": incs, "cmp": cmp_, "last_ts": last_ts,
        "views": int(series["view_count"].iloc[-1]) if not series.empty else None,
    }


def trend_text(cmp_: A.SpeedComparison) -> str:
    icon = TREND_ICON[cmp_.trend]
    if cmp_.change_percent is None:
        return f"{icon} {cmp_.trend}"
    return f"{icon} {cmp_.trend}（{cmp_.change_percent:+.0f}%）"


def render_detail(ctx: Context, start: pd.Timestamp | None, end: pd.Timestamp) -> None:
    ids = ctx.present() or ctx.order
    video_id = st.selectbox("分析する動画", ids, format_func=ctx.name, key="detail_video")
    info = summarize(ctx, video_id)
    series: pd.DataFrame = info["series"]

    cat = ctx.catalog.get(video_id, {})
    title = cat.get("title") or ctx.name(video_id)
    channel = cat.get("channel_title", "")
    heading = f"**{title}**" + (f"　/　{channel}" if channel else "")
    if ctx.demo:
        st.markdown(heading)
    else:
        head_l, head_r = st.columns([1, 5], vertical_alignment="center")
        head_l.image(thumbnail_url(video_id))
        head_r.markdown(heading)
        head_r.markdown(f"[YouTube で開く]({video_url(video_id)})")
    if series.empty:
        st.info("この動画のデータはまだありません。")
        return

    inc1, inc24 = info["incs"][0], info["incs"][4]
    cmp_: A.SpeedComparison = info["cmp"]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("現在の視聴回数", fmt_int(info["views"]), border=True,
              help=f"最終取得 {fmt_time(info['last_ts'], ctx.tz)}")
    c2.metric("過去1時間の増加数", fmt_int(inc1.increase, sign=True), border=True,
              delta=(f"前1時間比 {cmp_.change_percent:+.0f}%" if cmp_.change_percent is not None else None),
              delta_color="off" if cmp_.trend == "横ばい" else "normal",
              help=inc1.reason or f"実際の時間差 {A.format_duration(inc1.span)}")
    c3.metric("過去24時間の増加数", fmt_int(inc24.increase, sign=True), border=True,
              help=inc24.reason or f"実際の時間差 {A.format_duration(inc24.span)}")
    c4.metric("1時間あたりの平均増加数", fmt_rate(inc24.per_hour), border=True,
              help="過去24時間の平均。" + (f"1分あたり {inc24.per_minute:,.1f}回" if inc24.ok else inc24.reason))
    c5.metric("増加速度の傾向", f"{TREND_ICON[cmp_.trend]} {cmp_.trend}", border=True,
              help="直近1時間と、その前の1時間の1時間あたり増加数を比べています（±10%以内は横ばい）")
    st.caption(f"データの最終取得時刻: {fmt_time(info['last_ts'], ctx.tz)}（{fmt_age(info['last_ts'], ctx.now)}）"
               f"　・　直近1時間の速度: {fmt_rate(cmp_.recent.per_hour)}"
               f"（1分あたり {fmt_rate(cmp_.recent.per_minute, '分')}）")
    if info["last_ts"] is not None and ctx.now - info["last_ts"] > gap_limit(ctx) and not ctx.demo:
        st.warning("この動画の最新データは古くなっています。増加数は最後に取得した時点までの値です。")
    render_goal(ctx, video_id, info["views"], info["last_ts"], inc24, cmp_)

    # グラフ 1: 視聴回数の推移
    shown = clip(series, start, end)
    notes = ctx.notifications
    notes = clip(notes[notes["video_id"] == video_id], start, end) if not notes.empty else notes
    if shown.empty:
        st.info("選んだ表示期間にはデータがありません。表示期間を広げてください。")
    else:
        fig = fig_views(ctx, shown, notes, f"視聴回数の推移: {ctx.name(video_id)}")
        set_time_range(fig, ctx, start, end)
        show_chart(fig, "chart_views")
        table_expander(shown.assign(日時=to_local(shown["timestamp"], ctx.tz))[["日時", "view_count"]]
                       .rename(columns={"view_count": "視聴回数"}).iloc[::-1])

    # グラフ 2 と 4: 期間ごとの増加数 / 速度の比較
    left, right = st.columns(2)
    with left:
        show_chart(fig_window_bars(ctx, info["incs"]), "chart_windows")
        table_expander(pd.DataFrame({
            "期間": WINDOW_LABELS,
            "増加数": [fmt_int(i.increase, sign=True) for i in info["incs"]],
            "実際の時間差": [A.format_duration(i.span) if i.ok else "-" for i in info["incs"]],
            "1時間あたり": [fmt_rate(i.per_hour) for i in info["incs"]],
            "基準にしたデータ": [fmt_time(i.base_time, ctx.tz) if i.ok else i.reason for i in info["incs"]],
        }))
    with right:
        profile = A.speed_profile(series, interval=ctx.interval)
        st.markdown(f"**直近1時間 vs その前の1時間: {trend_text(cmp_)}**")
        show_chart(fig_speed(ctx, profile), "chart_speed")
        table_expander(profile.assign(per_hour=[fmt_rate(v) for v in profile["per_hour"]])
                       .rename(columns={"label": "比較", "per_hour": "1時間あたり", "increase": "増加数",
                                        "span": "実際の時間差", "reason": "備考"}))

    # グラフ 2'（時間帯ごとの増加数）
    bucket_label = st.segmented_control("時間帯の区切り", list(BUCKETS), default="1時間", key="bucket")
    bucket_label = bucket_label or "1時間"
    freq = BUCKETS[bucket_label]
    b_start = start if start is not None else series["timestamp"].iloc[0]
    n_buckets = (end - b_start) / freq
    if n_buckets > 1000:
        st.caption("区間が多すぎるため、区切りを 1 日にして表示しています。")
        bucket_label, freq = "1日", BUCKETS["1日"]
    buckets = A.bucket_increases(series, freq, start=b_start, end=end, tz=ctx.tz, interval=ctx.interval)
    if buckets.empty or buckets["increase"].isna().all():
        st.info("時間帯ごとの増加数を計算できるだけのデータがまだありません。")
    else:
        show_chart(fig_buckets(ctx, buckets, bucket_label), "chart_buckets")
        table_expander(pd.DataFrame({
            "開始": to_local(buckets["bucket_start"].dt.tz_convert("UTC"), ctx.tz),
            "増加数": [fmt_int(v, sign=True) for v in buckets["increase"]],
            "1時間あたり": [fmt_rate(v) for v in buckets["per_hour"]],
            "備考": ["途中経過" if p else r for p, r in zip(buckets["partial"], buckets["reason"])],
        }).iloc[::-1])

    # グラフ 3: 1時間あたりの増加数の推移
    margin = pd.Timedelta(hours=2)
    base_series = clip(series, (start - margin) if start is not None else None, end)
    rolling = clip(A.rolling_increase(base_series, pd.Timedelta(hours=1), interval=ctx.interval), start, end)
    video_cfg = ctx.config.video(video_id)
    rule = video_cfg.rule if video_cfg else ctx.config.notification.default_rule()
    if ctx.demo:
        rule = NotificationRule(window_minutes=60, threshold=int(DEMO_VIDEOS[video_id]["threshold"]))
    if rule.window_minutes == 60:
        thr, thr_label = float(rule.threshold), f"通知の閾値 {rule.threshold:,}回/時"
    else:
        thr = rule.threshold * 60 / rule.window_minutes
        thr_label = f"通知の閾値（{rule.window_minutes}分で{rule.threshold:,}回 → 1時間あたり{thr:,.0f}回）"
    if rolling["per_hour"].notna().any():
        fig = fig_rolling(ctx, rolling, thr, thr_label, inc24.per_hour)
        set_time_range(fig, ctx, start, end)
        show_chart(fig, "chart_rolling")
        table_expander(rolling.assign(日時=to_local(rolling["timestamp"], ctx.tz))[["日時", "increase", "per_hour"]]
                       .rename(columns={"increase": "直前1時間の増加数", "per_hour": "1時間あたり"}).iloc[::-1])
    else:
        st.info("1時間あたりの増加数は、1時間分のデータがそろうと表示されます。")


def goal_values(ctx: Context, video_id: str) -> tuple[int, ...]:
    if ctx.demo:
        goal = DEMO_VIDEOS.get(video_id, {}).get("goal")
        return (int(goal),) if goal else ()
    video = ctx.config.video(video_id)
    return video.goals if video else ()


def render_goal(ctx: Context, video_id: str, views: int | None, last_ts, inc24: A.Increase,
                cmp_: A.SpeedComparison) -> None:
    """目標再生数までの進み具合と、今のペースでの到達予想。"""
    goals = goal_values(ctx, video_id)
    if not goals or views is None:
        return
    goal = A.next_goal(views, goals)
    if goal is None:
        st.success(f"🎯 目標 {max(goals):,}回 を達成しています（現在 {views:,}回）")
        return
    # 速度は過去 24 時間の平均を優先し、なければ直近 1 時間を使う
    rate = inc24.per_hour if inc24.ok else cmp_.recent.per_hour
    eta = A.time_to_goal(views, goal, rate)
    if eta is not None:
        when = fmt_time(last_ts + eta, ctx.tz) if last_ts is not None else "-"
        eta_text = f"今のペース（{fmt_rate(rate)}）だと約{A.format_duration(eta)}後、{when}ごろ到達"
    elif rate is None:
        eta_text = "到達予想は、増加速度を計算できるだけのデータがそろうと表示されます"
    else:
        eta_text = "今のペースでは増えていないため、到達時刻を予想できません"
    st.progress(min(views / goal, 1.0),
                text=f"🎯 目標 {goal:,}回まで あと {goal - views:,}回（{views / goal:.1%}）　{eta_text}")


def assign_colors(selected: list[str], order: list[str], palette: list[str]) -> dict[str, str]:
    """色は動画ごとに固定（並び順で決め、選び直しても変わらない）。9 本目以降は空いている色を使う。"""
    colors: dict[str, str] = {}
    used = set()
    for vid in selected:
        idx = order.index(vid) if vid in order else -1
        if 0 <= idx < len(palette):
            colors[vid] = palette[idx]
            used.add(palette[idx])
    spare = [c for c in palette if c not in used]
    for vid in selected:
        if vid not in colors:
            colors[vid] = spare.pop(0) if spare else palette[-1]
    return colors


def render_overview(ctx: Context, start: pd.Timestamp | None, end: pd.Timestamp) -> None:
    ids = ctx.present()
    if not ids:
        render_empty(ctx)
        return
    rows = []
    for vid in ids:
        info = summarize(ctx, vid)
        inc1, inc24 = info["incs"][0], info["incs"][4]
        rows.append({
            "動画": ctx.name(vid),
            "現在の視聴回数": info["views"],
            "過去1時間": inc1.increase,
            "過去24時間": inc24.increase,
            "1時間あたり（24時間平均）": round(inc24.per_hour) if inc24.per_hour is not None else None,
            "速度の傾向": trend_text(info["cmp"]),
            "最終取得": (pd.Timestamp(info["last_ts"]).tz_convert(ctx.tz).strftime("%m/%d %H:%M")
                         if info["last_ts"] is not None else "-"),
        })
    st.markdown("**監視中の動画の一覧**（増加数は各動画の最新の取得時点から数えています）")
    number = st.column_config.NumberColumn(format="localized")
    st.dataframe(pd.DataFrame(rows), hide_index=True, column_config={
        "現在の視聴回数": number, "過去1時間": number, "過去24時間": number, "1時間あたり（24時間平均）": number,
    })

    default = ids[: min(4, len(ids))]
    selected = st.multiselect(f"比べる動画（最大 {MAX_COMPARE} 本）", ids, default=default,
                              format_func=ctx.name, max_selections=MAX_COMPARE, key="compare_videos")
    mode = st.segmented_control("表示方法", ["視聴回数", "期間内の増加数", "1時間あたりの増加数"],
                                default="期間内の増加数", key="compare_mode") or "期間内の増加数"
    if not selected:
        st.info("比べる動画を選んでください。")
        return
    colors = assign_colors(selected, ctx.order, ctx.palette())
    frames, table = [], []
    for vid in selected:
        series = ctx.series(vid)
        shown = clip(series, start, end).copy()
        if shown.empty:
            continue
        shown["gain"] = shown["view_count"] - shown["view_count"].iloc[0]
        margin = pd.Timedelta(hours=2)
        rolling = A.rolling_increase(clip(series, (start - margin) if start is not None else None, end),
                                     pd.Timedelta(hours=1), interval=ctx.interval)
        shown = shown.merge(rolling[["timestamp", "per_hour"]], on="timestamp", how="left")
        frames.append((ctx.name(vid), colors[vid], shown))
        table.append(shown.assign(動画=ctx.name(vid), 日時=to_local(shown["timestamp"], ctx.tz))
                     [["動画", "日時", "view_count", "gain", "per_hour"]])
    if not frames:
        st.info("選んだ表示期間にはデータがありません。")
        return
    if mode == "視聴回数" and len(frames) > 1:
        st.caption("視聴回数の桁が大きく違う動画を比べるときは「期間内の増加数」か「1時間あたりの増加数」が見やすくなります。")
    fig = fig_compare(ctx, frames, mode)
    set_time_range(fig, ctx, start, end)
    show_chart(fig, "chart_compare")
    table_expander(pd.concat(table).rename(columns={"view_count": "視聴回数", "gain": "期間内の増加数",
                                                    "per_hour": "1時間あたり"}))


def render_notifications(ctx: Context) -> None:
    notes = ctx.notifications
    if notes.empty:
        st.info("まだ通知は送られていません。")
        return
    df = notes.iloc[::-1]
    kinds = {"surge": "急な伸び", "milestone": "節目の突破", "goal": "目標達成"}
    st.dataframe(pd.DataFrame({
        "日時": to_local(df["timestamp"], ctx.tz),
        "動画": [ctx.name(v) for v in df["video_id"]],
        "種類": [kinds.get(k, k) for k in df["kind"]],
        "増加数": [fmt_int(v, sign=True) if pd.notna(v) else "" for v in df["increase"]],
        "判定期間": [f"{int(w)}分" if pd.notna(w) else "" for w in df["window_minutes"]],
        "閾値": [fmt_int(v) if pd.notna(v) else "" for v in df["threshold"]],
        "節目・目標": [fmt_int(v) if pd.notna(v) else "" for v in df["milestone"]],
        "その時の視聴回数": [fmt_int(v) for v in df["view_count"]],
        "表示": ["表示した" if d else "表示できず（ログ参照）" for d in df["delivered"]],
        "内容": list(df["message"].astype(str)),
    }), hide_index=True)


def render_data(ctx: Context) -> None:
    ids = ctx.present()
    if not ids:
        render_empty(ctx)
        return
    video_id = st.selectbox("動画", ids, format_func=ctx.name, key="data_video")
    series = ctx.series(video_id)
    gaps = A.find_gaps(series, ctx.interval)
    decreases = A.find_decreases(series)
    c1, c2, c3 = st.columns(3)
    c1.metric("記録数", f"{len(series):,} 件", border=True,
              help=f"{fmt_time(series['timestamp'].iloc[0], ctx.tz)} から {fmt_time(series['timestamp'].iloc[-1], ctx.tz)}")
    c2.metric("取得が途切れた箇所", f"{len(gaps)} か所", border=True,
              help=f"取得間隔が {A.format_duration(gap_limit(ctx))} を超えて空いた箇所。前後の値は線でつないでいません")
    c3.metric("視聴回数が減った箇所", f"{len(decreases)} か所", border=True,
              help="YouTube 側の集計の見直し（不正な再生の除去など）で減ることがあります。減少は減少のまま表示しています")
    if not gaps.empty:
        with st.expander("取得が途切れた箇所"):
            st.dataframe(pd.DataFrame({
                "開始": to_local(pd.Series(gaps["gap_start"]).dt.tz_convert("UTC"), ctx.tz),
                "再開": to_local(pd.Series(gaps["gap_end"]).dt.tz_convert("UTC"), ctx.tz),
                "長さ": [A.format_duration(pd.Timedelta(d)) for d in gaps["duration"]],
            }), hide_index=True)
    if not decreases.empty:
        with st.expander("視聴回数が減った箇所"):
            st.dataframe(pd.DataFrame({
                "日時": to_local(pd.Series(decreases["timestamp"]).dt.tz_convert("UTC"), ctx.tz),
                "前回": decreases["previous"], "今回": decreases["current"], "変化": decreases["change"],
            }), hide_index=True)
    full = ctx.history[ctx.history["video_id"] == video_id]
    export = full.assign(timestamp_local=to_local(full["timestamp"], ctx.tz).dt.strftime("%Y-%m-%d %H:%M:%S"))
    st.download_button("この動画のデータを CSV でダウンロード（Excel 用）",
                       export.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"history_{video_id}.csv", mime="text/csv")
    number = st.column_config.NumberColumn(format="localized")
    st.dataframe(export.iloc[::-1].head(2000)[["timestamp_local", "view_count", "like_count", "comment_count"]]
                 .rename(columns={"timestamp_local": "日時", "view_count": "視聴回数",
                                  "like_count": "高評価", "comment_count": "コメント"}), hide_index=True,
                 column_config={"視聴回数": number, "高評価": number, "コメント": number})
    if ctx.data_dir is not None:
        st.caption(f"データの保存場所: {ctx.data_dir}")


# ---------------------------------------------------------------------- 画面全体
def current_theme() -> str:
    theme = getattr(getattr(st, "context", None), "theme", None)
    return "dark" if getattr(theme, "type", None) == "dark" else "light"


def main() -> None:
    st.set_page_config(page_title="YouTube View Monitor", page_icon="📈", layout="wide")
    # 数値カードを 5 枚並べても値が切れないよう、文字を少し小さくして折り返しを許す
    st.markdown(
        "<style>[data-testid='stMetricValue']{font-size:1.55rem;}"
        "[data-testid='stMetricValue'] > div{overflow:visible;text-overflow:clip;white-space:normal;}"
        "[data-testid='stMetricLabel'] p{white-space:normal;}</style>",
        unsafe_allow_html=True,
    )
    args = parse_cli()
    config, _ = load_config_for_dashboard(args.config)

    with st.sidebar:
        st.header("📈 YouTube View Monitor")
        source = st.radio("表示するデータ", ["実データ", "デモデータ"], index=1 if args.demo else 0,
                          help="デモデータは API キーがなくても画面を試せる架空のデータです")
        auto = st.toggle("自動更新", value=True,
                         help=f"{config.dashboard.refresh_seconds} 秒ごとに保存済みデータを読み直します（API は使いません）")
        if st.button("今すぐ読み直す"):
            st.cache_data.clear()
        st.divider()
        st.caption("ダッシュボードは保存済みの CSV を読むだけです。監視（データの取得と通知）は "
                   "`python main.py` が行います。両方を同時に起動して使います。")

    run_every = config.dashboard.refresh_seconds if auto else None

    @st.fragment(run_every=run_every)
    def body() -> None:
        ctx = load_context(args, source == "デモデータ", current_theme())
        st.title("視聴回数ダッシュボード")
        render_status(ctx)
        hours = ctx.config.dashboard.default_period_hours
        default_label = next((k for k, v in PERIODS.items() if v == pd.Timedelta(hours=hours)), "24時間")
        period = st.segmented_control("表示期間", list(PERIODS), default=default_label, key="period") or default_label
        custom = None
        if period == "日付を指定":
            today = ctx.now.tz_convert(ctx.tz).date()
            custom = st.date_input("期間", value=(today - timedelta(days=6), today), key="custom_range")
            custom = tuple(custom) if isinstance(custom, (list, tuple)) else (custom,)
        start, end = period_bounds(period, custom, ctx)
        if ctx.history.empty:
            render_empty(ctx)
            return
        tab1, tab2, tab3, tab4 = st.tabs(["動画ごとの分析", "動画の比較・一覧", "通知の履歴", "データ"])
        with tab1:
            render_detail(ctx, start, end)
        with tab2:
            render_overview(ctx, start, end)
        with tab3:
            render_notifications(ctx)
        with tab4:
            render_data(ctx)
        st.caption(f"画面の更新: {ctx.now.tz_convert(ctx.tz).strftime('%H:%M:%S')}"
                   + (f"（{config.dashboard.refresh_seconds} 秒ごとに自動更新）" if auto else ""))

    body()


main()
