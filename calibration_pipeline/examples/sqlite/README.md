# SQLiteで学習と推論を試す

`calibration_pipeline/` で実行します。fixture生成スクリプトは、隣の `../synthetic_funnel/` から紹介履歴と候補を生成します。学習・校正・検証に用いるSQLite DBを `examples/sqlite/train/sqlite_fixture.db`、候補CSVを `examples/sqlite/inference/original_score.csv` に書き出します。

```powershell
uv sync --locked
uv run --locked python examples/sqlite/prepare_example.py
uv run --locked calibration-pipeline train --model-name sqlite_oubo_demo --sqlite --sqlite-fixture examples/sqlite/train/sqlite_fixture.db --config examples/sqlite/train/config.toml --sql examples/sqlite/train/train.sql
uv run --locked calibration-pipeline inference --model-name sqlite_oubo_demo --sqlite --sqlite-fixture examples/sqlite/train/sqlite_fixture.db --config examples/sqlite/inference/config.toml --sql examples/sqlite/inference/inference.sql --original-score examples/sqlite/inference/original_score.csv
```

コマンドは結果ディレクトリを表示します。学習は `experiments/sqlite_oubo_demo/train/<run_id>/` にモデル・指標・プロットを、推論は `experiments/sqlite_oubo_demo/inference/<run_id>/` に候補8件の確率を保存します。各runにはDBを含む入力のコピーが保存されます。fixtureは接続ごとにメモリに読み込み、推論候補は接続内の一時テーブル `calibration_candidates` にアップロードします。

同じコマンドを繰り返すと、新しいrunが作られます。推論が使用した学習run IDは推論の `run_metadata.json` に記録します。特定の学習runを使うには、推論コマンドに `--train-run <run_id>` を付けます。

学習データは、紹介から共通3暦月以内の応募を判定します。学習・校正・検証で期間を分け、各観測締切を満たした紹介を使います。SQLはSQLite用の例です。

- [train/config.toml](train/config.toml) と [train/train.sql](train/train.sql)
- [inference/config.toml](inference/config.toml) と [inference/inference.sql](inference/inference.sql)
- [パイプラインの仕様とRedshiftでの実行方法](../../README.md)

## DBの再生成と保存対象

DBはGitに含めません。`calibration_pipeline/` で以下を実行すると `examples/sqlite/train/sqlite_fixture.db` を生成します。合成候補CSVとmanifestも再生成します。

```powershell
uv sync --locked
uv run --locked python examples/sqlite/prepare_example.py
```

`experiments/example_oubo/` の設定・SQL・CSV・評価指標・プロット・メタデータは保存します。DBとモデルは除外します。このページの学習・推論コマンドで新しいrunにDBとモデルを作成できます。保存済みメタデータは当時の記録です。再実行時には別のrun ID・ハッシュが記録されます。他のexperimentはignore対象です。
