# SQLiteで学習と推論を試す

`calibration_pipeline/` で実行します。fixture生成スクリプトは、隣の `../synthetic_funnel/` の市場専用CLIを1回だけ呼びます。市場は開始から `warmup` 日間の紹介履歴で3段階のモデルを学習し、以降の紹介に `model_apply_score` などを付けます。`user` / `item` / `user_latent` / `item_latent` / `events` / `events_latent` / `daily` の7表を持つSQLite DBを出力し、学習・校正・検証に用いるDBを `examples/sqlite/train/sqlite_fixture.db`、市場の最後の紹介8件を元にした候補CSVを `examples/sqlite/inference/original_score.csv` に書き出します。別の場所にある生成器は `--generator-root` で指定できます。

```powershell
uv sync --locked
uv run --locked python examples/sqlite/prepare_example.py
uv run --locked calibration-pipeline train --model-name sqlite_oubo_demo --sqlite --sqlite-fixture examples/sqlite/train/sqlite_fixture.db --config examples/sqlite/train/config.toml --sql examples/sqlite/train/train.sql
uv run --locked calibration-pipeline inference --model-name sqlite_oubo_demo --sqlite --sqlite-fixture examples/sqlite/train/sqlite_fixture.db --config examples/sqlite/inference/config.toml --sql examples/sqlite/inference/inference.sql --original-score examples/sqlite/inference/original_score.csv
```

コマンドは結果ディレクトリを表示します。学習は `experiments/sqlite_oubo_demo/train/<run_id>/` にモデル・指標・プロットを、推論は `experiments/sqlite_oubo_demo/inference/<run_id>/` に候補8件の確率を保存します。各runにはDBを含む入力のコピーが保存されます。fixtureは接続ごとにメモリに読み込み、推論候補は接続内の一時テーブル `calibration_candidates` にアップロードします。

同じコマンドを繰り返すと、新しいrunが作られます。推論が使用した学習run IDは推論の `run_metadata.json` に記録します。特定の学習runを使うには、推論コマンドに `--train-run <run_id>` を付けます。

学習データは、紹介から共通3暦月以内の応募を判定します。学習区分は2025-02-04〜03-05、校正は2025-03-06〜04-04、検証は2025-04-05〜05-04の紹介を使い、各観測締切を満たした行だけを対象にします。**warmup期間中（`warmup_end` より前）の紹介は `model_*_score` が `NULL` なので、元スコアで学習できない**点に注意してください。`scored_referrals` ビューと学習SQLの両方で `model_apply_score IS NOT NULL` により明示的に除外しています。SQLはSQLite用の例です。

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

スクリプトは `synthetic_funnel/` の市場専用CLIを1回だけ呼びます（`--generator-root` で生成器の場所を変更できます）。

```powershell
cd ../synthetic_funnel
uv run --locked python -m synthetic_funnel --run-dir <一時dir>/market --format sqlite
```

`prepare_example.py` の `MARKET_CONFIG` から `input/config.json` を作ります。現在の設定は `market_start=2024-01-01`、`days=600`、`warmup=400`、`seed=73`、`seeker_arrivals/job_arrivals=7`（CLIの `--seeker-arrivals` / `--job-arrivals` に相当）、`application_days=20`、`posting_days=40`、`referrals=1` です。8種類ずつの日本語の職種・地域からペアを絞るため、3段階すべてを観測できる十分なwarmup規模が必要です。seedはwarmup期間中に3段階それぞれ最低2件の観測結果が得られる値で、issue #40の市場乱数変更に合わせて再選定しています。この設定では約58,600件の `events`（うち約20,600件がスコア付き）と約34MBのSQLite DBができます。市場の `output/dataset.sqlite.db` に `schema.sql` のビュー・派生テーブルを追加したものがfixtureです。候補CSVの `model_apply_score` は市場の最後のスコア付き紹介8件の値です。

`experiments/example_oubo/` の設定・SQL・CSV・評価指標・プロット・メタデータは保存します。DBとモデルは除外します。このページの学習・推論コマンドで新しいrunにDBとモデルを作成できます。保存済みメタデータは当時の記録です。再実行時には別のrun ID・ハッシュが記録されます。他のexperimentはignore対象です。


## 実行前に固定fixture DBを見る

`uv run --locked python -m calibration_pipeline.demo_gui` でGUIを開くと、固定の `examples/sqlite/train/sqlite_fixture.db` の絶対パス、テーブル・ビュー一覧、列名・型、件数、最大5行のサンプルを確認できます。閲覧は読み取り専用で、GUIの学習・推論でも同じ固定DBを使用します。GUIからDBパスを変更する機能はありません。

初回は **準備 → 内容確認 → 学習 → 推論 → 結果確認** の順に進めます。DBが無い・開けない場合は上記のartifact取得または生成コマンドを使い、必要に応じて読み取り権限を確認してください。GUIに再生成機能はなく、閲覧は読み取り専用です。CLIでの再生成は既存のDB・候補CSV・manifestを置き換えます。

DBは合成データです。`user` / `item` が不変の属性を持つマスター、`user_latent` / `item_latent` が参加時に決まる隠れた意欲、`events` が紹介ごとの観測履歴、`events_latent` が反実仮の潜在値、`daily` が日次集計です。`scored_referrals` はID・日時・元スコア (`model_apply_score`) を整え、属性は `user` マスターへJOINして取得し、warmup中のスコア欠損行を除外するビュー、`user_feature_daily` は紹介日ごとの `seeker_skill`（`user.user_skill`）と日本語TEXTの `occupation` を持つ派生テーブルです。学習SQLが `label` と `row_role` を作り、推論SQLが候補CSV由来の一時テーブル `calibration_candidates` と特徴量を結合します。詳細な列の意味と結果の読み方は[初回利用ガイド](../../docs/quickstart.md)を参照してください。
