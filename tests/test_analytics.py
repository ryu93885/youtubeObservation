"""増加数・増加速度の計算（通知と可視化で共通のロジック）のテスト。"""
import pandas as pd
import pytest

import analytics as A
from tests.conftest import T0, regular, series

H = pd.Timedelta(hours=1)


def test_tolerance_rules():
    assert A.tolerance_for(H) == pd.Timedelta(minutes=10)            # 10% < 10 分 → 10 分
    assert A.tolerance_for(24 * H) == pd.Timedelta(minutes=144)      # 24 時間の 10%
    assert A.tolerance_for(pd.Timedelta(minutes=15)) == pd.Timedelta(minutes=7.5)  # 期間の半分まで
    assert A.tolerance_for(H, interval=1800) == pd.Timedelta(minutes=30)  # 監視間隔より狭くしない


def test_increase_over_regular_interval():
    s = regular(25)  # 5 分ごと 120 分、毎回 +10
    inc = A.increase_over(s, H, interval=300)
    assert inc.ok
    assert inc.increase == 120
    assert inc.span == H
    assert inc.per_hour == pytest.approx(120)
    assert inc.per_minute == pytest.approx(2)
    assert inc.base_time == pd.Timestamp(T0) + pd.Timedelta(minutes=60)


def test_base_is_nearest_sample_to_window_start():
    # 終わり 100 分、始まりの目標 40 分。候補は 33 分（7 分ずれ）と 44 分（4 分ずれ）→ 44 分
    s = series([0, 33, 44, 100], [0, 330, 440, 1000])
    inc = A.increase_over(s, H)
    assert inc.base_time == pd.Timestamp(T0) + pd.Timedelta(minutes=44)
    assert inc.increase == 560
    assert inc.span == pd.Timedelta(minutes=56)
    assert inc.per_hour == pytest.approx(560 / (56 / 60))  # 実際の時間差で割る
    assert inc.span_differs


def test_tie_prefers_earlier_sample():
    s = series([0, 35, 45, 100], [0, 350, 450, 1000])  # 目標 40 分から 5 分ずつ
    inc = A.increase_over(s, H)
    assert inc.base_time == pd.Timestamp(T0) + pd.Timedelta(minutes=35)


def test_insufficient_data_returns_no_number():
    s = regular(6)  # 25 分ぶんしかない
    inc = A.increase_over(s, H)
    assert not inc.ok
    assert inc.increase is None and inc.per_hour is None
    assert "データ不足" in inc.reason


def test_gap_around_window_start_is_insufficient():
    # 目標時刻（終わり 120 分 − 60 分 = 60 分）の前後 10 分以内にデータがない
    s = series([0, 5, 45, 80, 120], [0, 5, 45, 80, 120])
    inc = A.increase_over(s, H)
    assert not inc.ok


def test_empty_series():
    empty = series([], [])
    assert not A.increase_over(empty, H).ok
    assert A.rolling_increase(empty).empty
    assert A.bucket_increases(empty, H).empty
    assert A.compare_speed(empty).trend == "判定不可"


def test_decrease_is_kept_negative():
    s = series([0, 30, 60], [1000, 1100, 900])
    inc = A.increase_over(s, H)
    assert inc.increase == -100
    assert inc.decreased
    dec = A.find_decreases(s)
    assert list(dec["change"]) == [-200]


def test_irregular_intervals_and_duplicates():
    raw = pd.concat([series([0, 7, 13, 22, 31, 38, 52, 61, 66], [0, 7, 13, 22, 31, 38, 52, 61, 66]),
                     series([31], [31])])  # 同じ時刻の重複
    s = A.prepare_series(raw)
    assert len(s) == 9
    inc = A.increase_over(s, H)
    assert inc.base_time == pd.Timestamp(T0) + pd.Timedelta(minutes=7)
    assert inc.per_hour == pytest.approx(60.0)  # 1 分に 1 回 → 1 時間 60 回


def test_explicit_end_time():
    s = regular(25)
    inc = A.increase_over(s, H, end=pd.Timestamp(T0) + pd.Timedelta(minutes=61))
    assert inc.end_time == pd.Timestamp(T0) + pd.Timedelta(minutes=60)
    assert inc.increase == 120


def test_rolling_increase_matches_increase_over():
    s = regular(40, step_views=7)
    r = A.rolling_increase(s, H, interval=300)
    assert r["increase"].iloc[-1] == A.increase_over(s, H, interval=300).increase
    assert r["per_hour"].iloc[-1] == pytest.approx(84)
    # 最初の数件は基準がないので NaN（0 にしない）
    assert r["increase"].iloc[0] != r["increase"].iloc[0]  # NaN


def test_bucket_increases_aligned_to_local_hours():
    start = pd.Timestamp("2026-11-15T00:00:00Z")  # 日本時間 9:00
    s = series([i * 5 for i in range(30)], [i * 5 for i in range(30)], start=start.to_pydatetime())
    b = A.bucket_increases(s, H, tz="Asia/Tokyo", interval=300)
    assert str(b["bucket_start"].iloc[0]) == "2026-11-15 09:00:00+09:00"
    assert b["increase"].iloc[0] == 60
    assert b["increase"].iloc[1] == 60
    assert bool(b["partial"].iloc[-1])  # 進行中の区間
    assert b["increase"].iloc[-1] == 25   # 11:00 から最新（11:25）まで


def test_bucket_with_gap_is_marked_insufficient():
    s = series([0, 5, 10, 150, 155, 180], [0, 5, 10, 150, 155, 180])
    b = A.bucket_increases(s, H, tz="UTC")
    assert b["reason"].iloc[0] == "データ不足"
    assert pd.isna(b["increase"].iloc[0])


def test_compare_speed_trends():
    up = series([0, 60, 120], [0, 100, 300])
    down = series([0, 60, 120], [0, 300, 400])
    flat = series([0, 60, 120], [0, 100, 205])
    assert A.compare_speed(up).trend == "上昇"
    assert A.compare_speed(up).change_percent == pytest.approx(100)
    assert A.compare_speed(down).trend == "低下"
    assert A.compare_speed(flat).trend == "横ばい"
    assert A.compare_speed(series([0, 60], [0, 100])).trend == "判定不可"


def test_classify_trend_with_zero_previous():
    assert A.classify_trend(10, 0) == ("上昇", None)
    assert A.classify_trend(0, 0) == ("横ばい", 0.0)
    assert A.classify_trend(None, 5)[0] == "判定不可"


def test_speed_profile_rows():
    p = A.speed_profile(regular(300), interval=300)
    assert list(p["label"]) == ["直近1時間", "その前の1時間", "直近3時間の平均", "直近6時間の平均", "直近24時間の平均"]
    assert p["per_hour"].tolist() == pytest.approx([120.0] * 5)


def test_find_gaps_and_insert_breaks():
    s = series([0, 5, 10, 60, 65], [0, 5, 10, 60, 65])
    gaps = A.find_gaps(s, 300)
    assert len(gaps) == 1
    assert gaps["duration"].iloc[0] == pd.Timedelta(minutes=50)
    broken = A.insert_gap_breaks(s, "timestamp", pd.Timedelta(minutes=11), ["view_count"])
    assert len(broken) == 6 and broken["view_count"].isna().sum() == 1


def test_window_increases_reports_each_window():
    incs = A.window_increases(regular(13), interval=300)  # 60 分ぶん
    assert [i.ok for i in incs] == [True, False, False, False, False]


def test_format_duration():
    assert A.format_duration(pd.Timedelta(minutes=45)) == "45分"
    assert A.format_duration(pd.Timedelta(minutes=90)) == "1時間30分"
    assert A.format_duration(pd.Timedelta(days=3, hours=2)) == "3日2時間"
    assert A.format_duration(None) == "-"


def test_resolve_points_matches_brute_force():
    """二分探索による高速版が、素朴な全探索と同じ結果を返すこと（不規則な間隔・同距離を含む）。"""
    import numpy as np

    rng = np.random.default_rng(0)
    minutes = np.cumsum(rng.choice([1, 3, 5, 5, 5, 10, 25], size=300)).astype(float)
    s = series(list(minutes), list(range(300)))
    targets = [pd.Timestamp(T0) + pd.Timedelta(minutes=float(m)) for m in rng.uniform(-30, minutes[-1] + 30, 400)]
    targets += [pd.Timestamp(T0) + pd.Timedelta(minutes=float(m)) for m in (minutes[:50] + 2.5)]  # 同距離になりうる
    tol = pd.Timedelta(minutes=7)
    got = A.resolve_points(s, targets, tol)
    for i, t in enumerate(targets):
        dist = (s["timestamp"] - t).abs()
        ok = dist <= tol
        if not ok.any():
            assert pd.isna(got["sample_time"].iloc[i])
            continue
        best = dist[ok].min()
        candidates = s.loc[ok & (dist == best), "timestamp"]
        expected = candidates.min()  # 同じ距離なら過去側
        assert got["sample_time"].iloc[i] == expected
        assert got["view_count"].iloc[i] == s.loc[s["timestamp"] == expected, "view_count"].iloc[0]
