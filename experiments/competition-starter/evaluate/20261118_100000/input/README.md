# input/ に置くもの

`experiments/_template/input/README.md` の規約に従う（COMPETITION.md §8）。

- `config.toml`: データ version（manifest ハッシュ）、seed、学習行数上限、抽出戦略、
  枠、順序付け、手数料率。本 run は proof データセット用。
- `solution/`: 参加者解法のコードと学習手順。ベースラインは
  `starter_kit/build_baseline.py` を参照（再現コマンドは `starter_kit/README.md` §2）。
- `submission.parquet`（`evaluate` のみ）: 生成物は Git 管理外で
  `starter_kit/data/proof/participant/` に置く。キー `day_dt` / `user_id` /
  `item_id` と必須の `rank`。確率列は診断用。
- 提出には `submission_metadata.json` を添える。

`*.parquet` は `.gitignore` 対象なので、提出ファイル自体はコミットせず、
ハッシュを `run_metadata.json` に記録します。
