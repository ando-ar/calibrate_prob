# クイックスタート

## SQLiteで学習と推論を試す

Python 3.11以上とuvを使います。リポジトリを取得し、`calibration_pipeline/` で実行してください。
fixture生成には、同じリポジトリの `../synthetic_funnel/` が必要です。

```powershell
uv sync --locked
uv run --locked python examples/sqlite/prepare_example.py
uv run --locked calibration-pipeline train --model-name sqlite_oubo_demo --sqlite --sqlite-fixture examples/sqlite/train/sqlite_fixture.db --config examples/sqlite/train/config.toml --sql examples/sqlite/train/train.sql
uv run --locked calibration-pipeline inference --model-name sqlite_oubo_demo --sqlite --sqlite-fixture examples/sqlite/train/sqlite_fixture.db --config examples/sqlite/inference/config.toml --sql examples/sqlite/inference/inference.sql --original-score examples/sqlite/inference/original_score.csv
```

学習結果は `experiments/sqlite_oubo_demo/train/<run_id>/output/`、推論結果は
`experiments/sqlite_oubo_demo/inference/<run_id>/output/` に保存されます。
推論の `calibrated_score.csv` には入力候補と同じ行順で確率列が加わります。
繰り返し実行すると新しいrunが作られます。DBはGitに含めず、上記スクリプトで再生成します。

## Redshiftで実行する

`uv sync --locked --extra redshift` を実行し、`REDSHIFT_HOST`、`REDSHIFT_DATABASE`、
`REDSHIFT_USER`、`REDSHIFT_PASSWORD` を環境変数に設定します。
`REDSHIFT_PORT` の既定値は5439です。読み取り権限に加え、推論には一時テーブルの作成権限が必要です。

```powershell
uv run --locked --extra redshift calibration-pipeline train --model-name my_model --config job/train.toml --sql job/train.sql
uv run --locked --extra redshift calibration-pipeline inference --model-name my_model --config job/inference.toml --sql job/inference.sql --original-score job/original_score.parquet
```

SQLiteのSQL例はRedshift向けに調整してください。設定・SQLの契約は[入力・出力と実行の流れ](guide.md)で説明しています。

