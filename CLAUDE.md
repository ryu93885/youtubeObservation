# このリポジトリでの作業ルール

- やり取り・ドキュメント・画面の文言は日本語で書く。
- API キー（`config.json`）、取得データ（`data/`）、ログ（`logs/`）はコミットしない（`.gitignore` 参照）。`config.example.json` の `api_key` は常に空にしておく。
- 増加数の計算は `analytics.py` にだけ書き、通知（`monitor.py`）とダッシュボード（`dashboard.py`）の両方から使う。同じ計算を別の場所に書かない。
- ダッシュボードは保存済みのファイルを読むだけにし、YouTube API を呼ばない。
- プログラムを変更したら `python -m pytest -q` を実行して確認する（YouTube API は使わないテストにする）。
- 機能・設定・コマンドを変えたら `README.md`（使い方・設定項目の表・コマンド一覧）と `docs/design.md` を更新する。
- 対象は Windows。`.bat` は ASCII のみ・CRLF で書く。パスは `pathlib` で扱う。
