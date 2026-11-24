# competition1 / evaluate / baseline の input/

この run はベースライン提出の評価である。data version・config は
`../../../../competitions/competition1/organizer/frozen_config.json` を正とし、生成は
`organizer/build_release.py` が行う（本 run は proof config 50/50 × 1 seed の実測）。

- `config.toml`: run 設定（data version、seed、学習行数、抽出戦略、枠、順序付け、手数料率）。
- `solution/README.md`: ベースライン解法（warmup モデル × fee）へのポインタ。
- `submission.parquet`（`evaluate` のみ）: キー `day_dt` / `user_id` / `item_id` と必須の `rank`。
  確率列は任意で診断用。`build_release.py` が生成し、ハッシュを `run_metadata.json` に記録する。

`*.parquet` は `.gitignore` 対象なので、提出ファイル自体はコミットせず、ハッシュを
`run_metadata.json` に記録する。
