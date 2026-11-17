# 開発時の検査

- Pythonを変更したら、完了前に `uv run --locked --extra redshift ruff check .` と `uv run --locked --extra redshift ty check --error-on-warning` を実行する。
- 両ツールのエラーと警告を修正する。検査を通すために抑制コメントを追加したり、ルールを無効にしたりしない。
- コミット前には `.githooks/pre-commit` が同じ検査を実行する。
- パッケージ内のインポートは `from calibration_pipeline...` で始まる絶対インポートを使う。Ruff の TID252 で検査する。
- SQLを扱うSourceクラスでは `execute_sql()` と `execute_sql_file()` を公開する。定型のSQL本文は `.sql` ファイルに置き、履歴・特徴量などは `execute_sql_file()` で実行する。日付やIDはバインド引数として渡す。
