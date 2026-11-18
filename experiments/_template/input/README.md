# input/ に置くもの

このディレクトリを `experiments/<name>/<stage>/<run_id>/input/` へコピーして使う。`synthetic_funnel/docs/COMPETITION.md` §8 が規約である。

- `config.toml`: データ version（manifest ハッシュ）、seed、学習行数上限、抽出戦略、枠、順序付け、手数料率。
- `solution/`: 参加者解法のコードと学習手順。再現に必要なファイルをすべて置く。
- `submission.parquet`（`evaluate` のみ）: キー `day_dt` / `user_id` / `item_id` と必須の `rank`。確率列は任意で診断用。
- 提出には `submission_metadata.json` を添える（seed、実行環境、git commit、config ハッシュ、data version、author、生成時刻）。

`*.parquet` は `.gitignore` 対象なので、提出ファイル自体はコミットせず、ハッシュを `run_metadata.json` に記録する。
