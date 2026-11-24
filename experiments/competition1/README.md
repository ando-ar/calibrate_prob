# competition1 実験記録

`competitions/competition1` のコンペ実験記録。規約は `synthetic_funnel/docs/COMPETITION.md` §8
（`experiments/_template/` をコピー）に従う。

```text
competition1/
  evaluate/
    20261119_000000/            # ベースライン提出の評価（proof config 50/50 x 1 seed）
      input/config.toml
      input/README.md
      input/solution/README.md
      output/summary.md
      run_metadata.json
```

- **run_id 命名**: `yyyyMMdd_HHmmss`。
- **ベースライン**: `organizer/build_release.py` が生成したベースライン提出を評価した run。
  proof config（50/50 × 1 seed）でパイプラインを実証済み。本番 config（200/200 × 5 seed、
  seed 71-75）のベースラインは、運営が `build_release.py --seeds 71,72,73,74,75` を実行した
  時点で新しい run_id として追加する。
- **参加者 run**: `train/`・`select/`（抽出戦略・校正手法の試行）と `evaluate/`（提出の評価）に
  分けて記録する。`input/config.toml` に data version（manifest ハッシュ）・seed・学習行数・
  抽出戦略・枠・順序付け・手数料率を記録する。
- data version は `organizer/metrics/data_versions.json`（seed ごとの manifest ハッシュ）と一致させる。
- 提出ファイル（`*.parquet`）は Git 管理外。ハッシュのみ `run_metadata.json` に記録する。
