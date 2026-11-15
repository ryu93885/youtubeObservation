# 設計メモ（YouTube View Monitor）

最終更新: 2026-11-15

## 1. 第 1 段階の調査結果（開発開始時点）

- リポジトリ `ryu93885/youtubeObservation` は作成直後で、**ファイルもコミットも存在しなかった**。
  引継ぎ資料にある `main.py`・`youtube_api.py`・`monitor.py`・`notifier.py`・`config.json` などはすべて未実装であった。
- そのため「既存機能」（API v3 での取得、URL / ID からの特定、50 本ずつのまとめ取得、5 分間隔、期間内増加数の判定と通知、
  連続通知の抑制、設定ファイル、CSV 保存と再起動後の読み込み）は、引継ぎ資料の仕様に従って**新規に実装**した。
- 既存のデータ形式・設定値は存在しないため、引継ぎ資料の初期値（300 秒・60 分・1,000 回）を既定値とし、
  データ形式は本書「5. データ形式」のとおり新たに定めた。

## 2. 構成

```
            ┌──────────── 監視プログラム（python main.py）────────────┐
 YouTube    │ youtube_api.py → monitor.py ──→ notifier.py → Windows 通知 │
 Data API ──┤      取得           判定 ↑ analytics.py（増加数の計算）   │
 v3         │                     保存 ↓ data_manager.py               │
            └──────────────────────│──────────────────────────────────┘
                                   ▼
          data/history.csv, notifications.csv, videos.json, status.json
                                   ▲ 読むだけ（API は使わない）
            ┌──────────── ダッシュボード（streamlit run dashboard.py）──┐
            │ dashboard.py ── analytics.py（通知と同じ計算）で集計・描画   │
            └─────────────────────────────────────────────────────────┘
```

| モジュール | 役割 |
|---|---|
| `main.py` | コマンドの受け付け（`--check` `--once` `--test-notify`）、ログ設定（API キーの伏せ字化）、二重起動防止、監視の開始 |
| `monitor.py` | 通知条件の判定（急増・節目・クールダウン・まとめ通知）と、監視ループ（時刻そろえ・クォータ待ち・設定の再読み込み・保存期間の管理・状態ファイル） |
| `analytics.py` | 増加数・増加速度・時間帯別増加数・速度比較・欠損と減少の検出。**通知とダッシュボードの共通ロジック** |
| `youtube_api.py` | URL の解析、videos.list の呼び出し（50 件ずつ）、再試行、エラーの分類 |
| `notifier.py` | Windows トースト通知（失敗時はコンソール / ログ） |
| `data_manager.py` | CSV / JSON の安全な読み書き、保存期間による削除 |
| `config.py` | 設定の読み込み・検証・既定値 |
| `instance_lock.py` | OS のファイルロックによる二重起動防止 |
| `dashboard.py` | Streamlit のダッシュボード |
| `demo_data.py` | デモデータ（`analytics` を使って通知時点も再現する） |

引継ぎ資料の推奨構成からの変更点は次の 3 つである。

1. **`analytics.py` を `monitor.py` から分けた。** 推奨構成では monitor.py が「増加数の計算と通知条件の判定」を担うが、
   ダッシュボードが「監視」モジュールを読み込む形は分かりにくく、監視ループ（通信・ファイル書き込み）と
   純粋な計算を同じ場所に置くとテストしにくい。計算を分けることで、通知とダッシュボードが同じ関数を使うことを構造で保証した。
2. **`config.py` と `instance_lock.py` を追加した。** 設定の検証（日本語の誤り表示）と二重起動防止は、どちらの役割にも属さないため。
3. **`demo_data.py` とダブルクリック用の `.bat` を追加した。** API キーなしで試せること、初心者が起動できることのため。

## 3. 増加数の計算のきまり（`analytics.py`）

1. 期間の終わり = その動画の最新の取得データ（`end` を指定した場合はその時刻に最も近いデータ）。
2. 期間の始まりの基準 = 「終わり − 期間」に**最も近い**取得データ。
   - 許容誤差 = max(期間 × 10%, 10 分, 監視間隔)。ただし期間の半分を超えない。
   - 同じ距離なら過去側（期間を長めにとる側）を選ぶ。
3. 基準が見つからなければ「データ不足」（`Increase.ok == False`, `increase is None`）。0 とは区別する。
4. 増加数 = 終わり − 基準。減少はマイナスのまま返す（`decreased`）。
5. 速度（`per_hour` / `per_minute`）は実際の時間差（`span`）で割る。名目の期間とのずれは `span_differs` で分かる。

| 関数 | 用途 |
|---|---|
| `resolve_points` | 任意の時刻群について「最も近い取得データ（許容誤差以内）」を求める。以下のすべての土台 |
| `increase_between` / `increase_over` | 2 時点間 / 直近の期間の増加数。**通知の判定はこれを使う** |
| `window_increases` | 過去 1・3・6・12・24 時間の増加数 |
| `rolling_increase` | 各取得時刻における「直前 1 時間の増加数」（グラフ 3） |
| `bucket_increases` | 表示用タイムゾーンの時計にそろえた時間帯ごとの増加数（進行中の区間は `partial`） |
| `compare_speed` / `speed_profile` | 直近 1 時間とその前の 1 時間の比較、3・6・24 時間平均との比較（グラフ 4） |
| `find_gaps` / `find_decreases` | 取得の途切れ（間隔 > 監視間隔 × 2 + 1 分）と減少の検出 |

傾向の判定: 直近とその前の 1 時間あたり増加数の変化率が +10% 超で「上昇」、−10% 未満で「低下」、それ以外は「横ばい」。
どちらかがデータ不足なら「判定不可」。

## 4. 通知のきまり（`monitor.py`）

- **急増（surge）**: `increase_over(window_minutes)` が `threshold` 以上。今回の取得で値が得られた動画だけを判定する（古いデータでは判定しない）。
- **クールダウン**: 同じ動画の急増通知は `cooldown_minutes` あける。基準は前回通知の時刻。
- **節目（milestone）**: 前回と今回の取得の間で `milestone_step` の倍数を超えたら、超えた最大の節目を 1 回だけ通知する。監視開始時点ですでに超えている節目は通知しない。
- **まとめ通知**: 1 回の取得で `max_per_cycle` 件を超えたら 1 件にまとめる（記録は個別に残す）。
- **再起動後の引き継ぎ**: `notifications.csv` から、動画ごとの最後の急増通知の時刻と通知済みの節目を復元する（`NotificationLedger`）。状態ファイルを別に持たないので、記録と状態が食い違わない。
- 通知の表示に失敗しても記録は残す（`delivered=false`）。同じ条件で何度も再送はしない。

## 5. データ形式

### history.csv

```
timestamp,video_id,view_count,like_count,comment_count
2026-11-15T06:10:00+00:00,dQw4w9WgXcQ,1234567,45678,1234
```

- `timestamp` は UTC の ISO 8601（秒まで）。同じ回に取得した動画は同じ時刻になる。
- 高評価数・コメント数は非公開の動画があるため空欄を許す。
- 読み込み時の扱い:
  - 改行で終わっていない最後の行（書き込み中）は読まない。
  - 項目数が 5 でない行（異常終了で途中まで書かれた行）は捨てる。数字が途中で切れた値を使わないため。
  - 日時・動画 ID・視聴回数が読めない行は捨てて件数を報告する。
  - 同じ動画・同じ時刻の重複は、後の行を残す。
  - タイムゾーンのない日時は UTC とみなす。

### notifications.csv

`timestamp, video_id, kind(surge|milestone), window_minutes, increase, threshold, view_count, milestone, delivered, message`

### status.json（監視プログラム → ダッシュボード）

`state`（starting / running / error / waiting_quota / stopped）、`updated_at`（待機中も 1 分ごとに更新）、
`last_success_at`、`next_run_at`、`last_error`、`consecutive_failures`、`quota`（太平洋時間の日付ごとの使用量）、
`missing_videos`、`pending_rows`、`videos`（最新値）。

## 6. 安定性のための工夫

| 課題 | 対策 |
|---|---|
| 通信エラー・5xx・429 | 指数バックオフ（2・4・8 秒 + ゆらぎ）で 3 回まで再試行。`Retry-After` があれば従う（最大 60 秒）。それでも失敗した回は飛ばして次の時刻に再試行 |
| クォータ超過 | 再試行しない。太平洋時間の翌 0 時 5 分まで待って再開 |
| API キーの誤り | 再試行しない。原因別の対処を表示。`config.json` を直せば再起動なしで反映 |
| `fields` 指定の拒否（万一） | 自動で `fields` なしに切り替える |
| 二重起動 | OS のファイルロック（異常終了しても自動で外れる） |
| Excel で CSV を開いている | 書き込めない行をメモリーに保留して次回まとめて書く。判定はメモリー上の履歴で続ける |
| 書き込み途中の読み込み | 1 回の `write` でまとめて追記し、読み手は未完了の行を読まない |
| JSON の置き換え中の読み込み | 一時ファイル + `os.replace`（Windows で失敗したら再試行）。読み手は失敗したら少し待って読み直す |
| PC のスリープ | 最大 5 秒ずつ待つので、復帰後すぐに取得する。取りこぼした回をまとめて取り直すことはしない（API を無駄に使わない） |
| 取得時刻のずれ | 時計の切りのよい時刻（0・5・10 分…）にそろえる。起動直後の取得の直後には取らない |
| 動画タイトルに含まれる記号 | 通知の文面は XML としてエスケープし、Base64 で PowerShell に渡す（下記） |
| 想定外のエラー（ディスク容量不足など） | その回の処理だけをあきらめ、詳細をログに残して次の回に進む（Ctrl+C では止まる） |
| 長い履歴でのダッシュボードの速度 | 基準データの探索を二分探索で行い（50 本 × 30 日 ≒ 43 万行で再描画約 1 秒）、CSV はファイルが変わったときだけ読み直す |

### Windows 通知の実装について

通知ライブラリ winotify 1.1.0 のソースを確認したところ、タイトルや本文をエスケープせずに PowerShell の
ダブルクォート文字列（`@" "@` と `"..."`）へ埋め込んでいた。他人の動画タイトルに `"` や `$( )` が含まれると、
任意の PowerShell コマンドが実行されうる。そこで外部ライブラリは使わず、

1. 文面を `xml.sax.saxutils` で XML としてエスケープし、
2. XML 全体を Base64 にしてスクリプトに埋め込み（外部の文字列は英数字と `+/=` だけになる）、
3. スクリプトは `-EncodedCommand` ではなく標準入力で `powershell.exe -Command -` に渡す
   （長い Base64 をコマンドラインに載せる形はセキュリティソフトに不審な動作と判定されやすいため）

という方式にした。通知元の名前は `HKEY_CURRENT_USER\Software\Classes\AppUserModelId\YouTubeViewMonitor` に
表示名を登録する（管理者権限は不要）。表示されない環境向けに `app_id: "powershell"` を用意した。

**注意**: 開発環境は Linux のため、トースト通知の実際の表示は Windows 上で `python main.py --test-notify` により確認すること。
テストでは PowerShell に渡す内容（エスケープ・Base64・コマンドライン）を検証している。

## 7. 保存期間（`retention_days`）

YouTube API サービスの開発者ポリシーでは、API で取得したデータを最新の状態に更新しないまま 30 日を超えて保存しないことが
求められている（統計を長期保存できるのは審査で認められた場合に限る）。そのため既定値を 30 日とし、
監視プログラムが起動時と日付が変わったときに、古い行を一時ファイル経由で削除する。日時を読み取れない行は念のため残す。
`0` で無効化できる。

## 8. 動作確認済みの環境

- Python 3.13、pandas 3.1 / streamlit 1.67 / plotly 7.1（最新）と、pandas 2.2 / streamlit 1.55 / plotly 5.24 / requests 2.31（`requirements.txt` の最低バージョン）の両方で全テストが通ることを確認した。
- ダッシュボードは Chromium（Playwright）で、ライト / ダークの両テーマ、デモデータ / 実データ形式 / データなしの各状態を表示して確認した。
- Windows 固有の処理（トースト通知、`msvcrt` によるロック、`.bat`）は Windows 上での確認が必要である。

## 9. 今後の拡張のための入口

| やりたいこと | 触る場所 |
|---|---|
| 通知先を増やす（LINE・Discord など） | `notifier.Notifier` を継承したクラスを作り、`create_notifier` で選べるようにする |
| 通知条件を増やす（例: 速度が前の 1 時間の 2 倍） | `monitor.py` に `evaluate_xxx` を追加し、`MonitorService._evaluate` で呼ぶ。計算は `analytics` の関数を使う |
| チャンネルの新着動画を自動で監視対象にする | `youtube_api.py` に playlistItems.list（アップロード再生リスト）を追加し、`config.py` に `channels` を追加 |
| 保存先を SQLite にする | `data_manager.py` の `HistoryStore` / `read_history_csv` と同じ戻り値（`video_id`, `timestamp`, `view_count` の DataFrame）を返す実装を作る |
| グラフを追加する | `dashboard.py` に `fig_xxx` を追加。色は `PALETTE` の順番で使い、`style_figure` で体裁をそろえる |
