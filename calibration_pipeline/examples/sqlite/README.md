# SQLiteで学習と推論を試す

`calibration_pipeline/` で実行します。fixture生成スクリプトは、隣の `../synthetic_funnel/` のCLIを2段階で呼びます。まず小さな通常モードのrunで `legacy_models.json` を作り、そのモデルを使う市場モードのrunから `user` / `item` / `events` / `events_latent` / `daily` を持つSQLite DBを出力します。学習・校正・検証に用いるDBを `examples/sqlite/train/sqlite_fixture.db`、市場の最後の紹介8件を元にした候補CSVを `examples/sqlite/inference/original_score.csv` に書き出します。別の場所にある生成器は `--generator-root` で指定できます。

```powershell
uv sync --locked
uv run --locked python examples/sqlite/prepare_example.py
uv run --locked calibration-pipeline train --model-name sqlite_oubo_demo --sqlite --sqlite-fixture examples/sqlite/train/sqlite_fixture.db --config examples/sqlite/train/config.toml --sql examples/sqlite/train/train.sql
uv run --locked calibration-pipeline inference --model-name sqlite_oubo_demo --sqlite --sqlite-fixture examples/sqlite/train/sqlite_fixture.db --config examples/sqlite/inference/config.toml --sql examples/sqlite/inference/inference.sql --original-score examples/sqlite/inference/original_score.csv
```

コマンドは結果ディレクトリを表示します。学習は `experiments/sqlite_oubo_demo/train/<run_id>/` にモデル・指標・プロットを、推論は `experiments/sqlite_oubo_demo/inference/<run_id>/` に候補8件の確率を保存します。各runにはDBを含む入力のコピーが保存されます。fixtureは接続ごとにメモリに読み込み、推論候補は接続内の一時テーブル `calibration_candidates` にアップロードします。

同じコマンドを繰り返すと、新しいrunが作られます。推論が使用した学習run IDは推論の `run_metadata.json` に記録します。特定の学習runを使うには、推論コマンドに `--train-run <run_id>` を付けます。

学習データは、紹介から共通3暦月以内の応募を判定します。学習区分は2024-02〜04月、校正は2024-05〜07月、検証は2024-08〜10月の紹介を使い、各観測締切を満たした行だけを対象にします。ウォームアップ中のスコア欠損行（将来の市場専用CLIで作られる `NULL`）は学習から除外します。SQLはSQLite用の例です。

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

スクリプトは `synthetic_funnel/` のCLIを次の2段階で呼びます（`--generator-root` で生成器の場所を変更できます）。

```powershell
# 1) 小さな通常モードのrun（rows=800、legacy_scores有効）から legacy_models.json を作る
cd ../synthetic_funnel
uv run --locked python -m synthetic_funnel --run-dir <一時dir>/history --format sqlite
# 2) 市場モード（days=500、流入8件/日、紹介1件/日/人、application_days=6、posting_days=12）
uv run --locked python -m synthetic_funnel --market --run-dir <一時dir>/market --format sqlite
```

各runの `input/config.json` は `prepare_example.py` の `HISTORY_CONFIG` / `MARKET_CONFIG` から作られ、市場設定の `legacy_models` には手順1のモデルへのパスが入ります。市場の `output/dataset.sqlite.db` に `schema.sql` のビュー・派生テーブルを追加したものがfixtureです。候補CSVの `model_apply_score` は市場の最後の紹介8件のスコアです。将来、市場専用CLIへ切り替えるときは `run_generator()` と上記の設定だけを差し替えます。

`experiments/example_oubo/` の設定・SQL・CSV・評価指標・プロット・メタデータは保存します。DBとモデルは除外します。このページの学習・推論コマンドで新しいrunにDBとモデルを作成できます。保存済みメタデータは当時の記録です。再実行時には別のrun ID・ハッシュが記録されます。他のexperimentはignore対象です。


## 実行前に固定fixture DBを見る

`uv run --locked python -m calibration_pipeline.demo_gui` でGUIを開くと、固定の `examples/sqlite/train/sqlite_fixture.db` の絶対パス、テーブル・ビュー一覧、列名・型、件数、最大5行のサンプルを確認できます。閲覧は読み取り専用で、GUIの学習・推論でも同じ固定DBを使用します。GUIからDBパスを変更する機能はありません。

初回は **準備 → 内容確認 → 学習 → 推論 → 結果確認** の順に進めます。DBが無い・開けない場合は上記のartifact取得または生成コマンドを使い、必要に応じて読み取り権限を確認してください。GUIに再生成機能はなく、閲覧は読み取り専用です。CLIでの再生成は既存のDB・候補CSV・manifestを置き換えます。

DBは合成データです。`user` / `item` が不変の属性を持つマスター、`events` が紹介ごとの観測履歴、`events_latent` が反実仮の潜在値、`daily` が日次集計です。`scored_referrals` はID・日時・元スコア (`model_apply_score`) を整え、属性は `user` マスターへJOINして取得するビュー、`user_feature_daily` は紹介日ごとの `seeker_skill`（`user.user_skill`）と `occupation` を持つ派生テーブルです。学習SQLが `label` と `row_role` を作り、推論SQLが候補CSV由来の一時テーブル `calibration_candidates` と特徴量を結合します。詳細な列の意味と結果の読み方は[初回利用ガイド](../../docs/quickstart.md)を参照してください。
