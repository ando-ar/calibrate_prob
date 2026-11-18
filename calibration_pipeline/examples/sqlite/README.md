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

## fixtureの取得と保存対象

DBはGitに含めません。fixture一式はGitHub Actionsの [Fixture](https://github.com/ando-ar/calibrate_prob/actions/workflows/fixture.yml) の成功した実行から、`calibration-pipeline-fixture` artifact（保持期間14日）をダウンロードします。ZIPを `calibration_pipeline/examples/sqlite/` に展開すると、`train/sqlite_fixture.db`、`train/synthetic_data_manifest.json`、`inference/original_score.csv` がこのページのパスに配置されます。展開後はそのまま学習・推論・GUIを実行できます。保持期間を過ぎたartifactは取得できないため、ローカルで再生成してください。

ローカルで再生成する場合は `calibration_pipeline/` で以下を実行します。合成候補CSVとmanifestも再生成します。

```powershell
uv sync --locked
uv run --locked python examples/sqlite/prepare_example.py
```

`experiments/example_oubo/` の設定・SQL・CSV・評価指標・プロット・メタデータは保存します。DBとモデルは除外します。このページの学習・推論コマンドで新しいrunにDBとモデルを作成できます。保存済みメタデータは当時の記録です。再実行時には別のrun ID・ハッシュが記録されます。他のexperimentはignore対象です。


## 実行前に固定fixture DBを見る

`uv run --locked python -m calibration_pipeline.demo_gui` でGUIを開くと、固定の `examples/sqlite/train/sqlite_fixture.db` の絶対パス、テーブル・ビュー一覧、列名・型、件数、最大5行のサンプルを確認できます。閲覧は読み取り専用で、GUIの学習・推論でも同じ固定DBを使用します。GUIからDBパスを変更する機能はありません。

初回は **準備 → 内容確認 → 学習 → 推論 → 結果確認** の順に進めます。DBが無い・開けない場合は生成コマンドまたはGUIの「fixtureを再生成する」を使い、必要に応じて読み取り権限を確認してください。再生成は既存のDB・候補CSV・manifestを置き換えます。

DBは合成データです。`events` が元のイベント履歴、`scored_referrals` がID・日時・応募スコアを整えたビュー、`user_feature_daily` が日付ごとの `seeker_skill` 特徴量です。学習SQLが `label` と `row_role` を作り、推論SQLが候補CSV由来の一時テーブル `calibration_candidates` と特徴量を結合します。詳細な列の意味と結果の読み方は[初回利用ガイド](../../docs/quickstart.md)を参照してください。
