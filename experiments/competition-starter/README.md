# competition-starter 実験記録

`synthetic_funnel/docs/COMPETITION.md` §8 の規約（`experiments/_template/` をコピー）に従う、
コンペ用スターターキットの実験記録です。

```text
competition-starter/
  evaluate/
    20261118_100000/         # yyyyMMdd_HHmmss
      input/
        config.toml
        README.md
        solution/README.md   # starter_kit/ へのポインタ
      output/
        .gitkeep
        summary.md           # 仮説・結果・考察（評価器の実測結果を記入済み）
      run_metadata.json
```

`train` / `select` の run は、参加者が抽出戦略や校正手法を試した時点で同じ
`yyyyMMdd_HHmmss` 規約で追加してください。この run はベースライン提出の
初期化で、issue #35 の評価器が利用可能になり `status: succeeded` に更新しました
（実測値は `output/metrics.json` / `output/summary.md`）。

評価器の出力は `output/`（seed 71）、`output/seed72/`（seed 72）、
`output/fallback/`（2 シード、単日）に分けて保存しています。
`output/evaluator_summary_seed71.md` は CLI が自動生成した要約の控えです。

提出ファイル自体（`*.parquet`）は Git 管理外です。生成物は
`starter_kit/data/proof/participant/` にあり、ハッシュのみ `run_metadata.json`
に記録します。
