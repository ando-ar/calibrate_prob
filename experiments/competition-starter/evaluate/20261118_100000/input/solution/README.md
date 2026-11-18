# solution/

ベースライン解法の置き場です。コード自体は再利用可能なキットとして
`starter_kit/` に置いてあります。

- `starter_kit/build_baseline.py`: warmup モデルを観測データから再学習し、
  評価日候補へ適用して `submission.parquet` / `.csv` を生成します。
- `starter_kit/generate_dataset.py`: 配布データ（参加者用 DB）の再生成。
- `starter_kit/README.md`: 実行手順、行数制約、提出スキーマ、ベースライン導出。

再現手順:

```powershell
uv run --project synthetic_funnel python starter_kit/generate_dataset.py --force
uv run --project synthetic_funnel python starter_kit/build_baseline.py
```

参加者が自有の解法を試すときは、このディレクトリにコードと学習手順をコピーし、
`config.toml` の `learning.extraction_strategy` と `ranking.ranking_rule` を更新して
別の `run_id` として記録してください（COMPETITION.md §8）。
