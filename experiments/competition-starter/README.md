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
        summary.md           # 仮説・結果・考察のひな形（結果は評価器待ち）
      run_metadata.json
```

`train` / `select` の run は、参加者が抽出戦略や校正手法を試した時点で同じ
`yyyyMMdd_HHmmss` 規約で追加してください。この run はベースライン提出の
初期化であり、評価器（issue #35）が未実装のため `status: planned` です。

提出ファイル自体（`*.parquet`）は Git 管理外です。生成物は
`starter_kit/data/proof/participant/` にあり、ハッシュのみ `run_metadata.json`
に記録します。
