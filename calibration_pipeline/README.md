# 紹介候補スコアの校正

既存の予測モデルのスコアを確率に変換するバッチです。応募・書類通過・決定はそれぞれ異なる元モデルとして管理します。たとえば、応募用の二塔モデルを `20261118_twotower_logQcorrection` というexperiment名で登録し、同じ名前で学習と推論を実行します。

## Sphinxドキュメント

[GitHub ActionsのDocumentation](https://github.com/ando-ar/calibrate_prob/actions/workflows/docs.yml) の成功した実行から `calibration-pipeline-docs` artifact（保持期間14日）をダウンロードし、ZIPをすべて展開して **`index.html` をブラウザーで開く**と、日本語の利用ガイド・APIリファレンスを読めます。取得後はオフラインで閲覧でき、Pythonの準備やWebサーバーは不要です。
生成HTMLはGit管理しません。ローカルで生成する場合は `calibration_pipeline/` で以下を実行し、`docs/html/index.html` を開きます。

```powershell
uv sync --locked --group docs
uv run --locked --group docs python dev/build_docs.py
```

APIはコードから生成し、push・PR時にはGitHub Actionsで自動ビルドします。
コード変更を監視するローカルプレビューとCI成果物の取得方法は[ドキュメント開発手順](docs/development.md)を参照してください。

## 実行する

`calibration_pipeline/` で依存関係を準備し、元モデルごとに名前と入力ファイルを渡します。

```powershell
uv sync --locked --extra redshift
uv run --locked --extra redshift calibration-pipeline train --model-name 20261118_twotower_logQcorrection --config job/train.toml --sql job/train.sql
uv run --locked --extra redshift calibration-pipeline inference --model-name 20261118_twotower_logQcorrection --config job/inference.toml --sql job/inference.sql --original-score job/original_score.parquet
```

推論は開始時に、同じexperimentで成功している学習runのうち、開始時刻が最も新しいものを選びます。学習完了時刻の順序は選択に影響しません。固定した学習runで再現したい場合は、推論に `--train-run <run_id>` を付けます。

実行前に `REDSHIFT_HOST`、`REDSHIFT_DATABASE`、`REDSHIFT_USER`、`REDSHIFT_PASSWORD` を環境変数に設定します。`REDSHIFT_PORT` の既定値は5439です。学習と推論は参照テーブルの読み取り権限を使います。推論はさらに一時テーブルを作成する権限を使います。

Redshiftを使わずに実行するには [SQLiteの実行例](examples/sqlite/README.md) を参照してください。

## 入力と出力

CLIは指定ファイルをコピーし、コピーを使って処理します。既定の保存先は `experiments/` です。別の保存先は `--experiments-dir` で指定できます。run IDは開始時刻をJSTの `yyyyMMdd_HHmmss` で表し、同じexperiment内の同じ処理が同秒に実行された場合は `_01` などを付けます。

```text
experiments/<model_name>/
  train/<run_id>/
    input/
      config.toml
      train.sql
    run_metadata.json
    output/
      calibration_model.pkl
      calibration_metrics.json
      calibration_plot.svg
  inference/<run_id>/
    input/
      config.toml
      inference.sql
      original_score.csv または original_score.parquet
    run_metadata.json
    output/
      calibrated_score.csv または calibrated_score.parquet
      inference_metadata.json
```

SQLiteで `--sqlite-fixture` を指定した場合は、DBも `input/sqlite_fixture.db` にコピーします。runメタデータには開始時刻、終了時刻、状態とコピー済み入力のSHA-256を記録します。推論には使用した学習run IDとモデルのSHA-256も記録します。失敗時はコピー済み入力と失敗状態が残り、`output/` は成功時に公開されます。

### 学習設定とSQL

学習のTOMLでは、対象スコア、ラベル、出力確率、数値特徴量、カテゴリ特徴量を設定します。[学習のTOML例](examples/sqlite/train/config.toml)・[学習のSQL例](examples/sqlite/train/train.sql)

```toml
[model]
score = "oubo_score"
label = "label"
probability = "oubo_prob"
numeric_features = ["seeker_skill"]
categorical_features = ["occupation"]
bins = 10
```

`train.sql` は次の3区分を一つの結果表にまとめます。全区分で元スコア、設定した特徴量、確定二値ラベルを返します。

| `row_role` | 返す行 |
|---|---|
| `train` | CatBoostの学習に使う過去の対象フェーズ履歴 |
| `calibrate` | CatBoostの出力を校正する、学習とは別の後続期間の履歴 |
| `validation` | 学習と校正に使っていない、さらに後続の期間の履歴 |

元スコアは欠損のない有限値の数値列、ラベルは数値の0または1です。数値特徴量ではNULLとNaNを欠損として扱い、無限値はエラーになります。カテゴリ特徴量は文字列列とし、NULLと学習時に見なかった文字列も使えます。数値・カテゴリ特徴量がすべてNULLの列も保持します。パイプラインがカテゴリ欠損を通常の文字列と区別してCatBoostに渡します。

校正対象が紹介から3暦月以内の到達確率なら、分母は応募で全紹介、通過で期限内の応募、決定で期限内の通過です。期限未到達の行を除き、対象フェーズの自然な陽性率を保って抽出します。SQL作成者が期間の順序と重複、ラベルの確定、分母、特徴量の取得時点を管理します。

### 推論の候補ファイルとSQL

候補ファイルは1行1候補です。元スコア、`user_id`、`item_id`、特徴量の結合に必要な列を含めます。IDは8桁の整数で、`user_id` は10000000〜19999999、`item_id` は20000000〜29999999です。CSVはUTF-8を使用し、IDは8桁の数字で記述します。ParquetではIDを整数型にします。

CLIは `candidate_row_id` を付け、SQLを実行する同じ接続に一時テーブル `calibration_candidates` を作ります。ユーザーが用意する `inference.sql` はこの固定名を参照し、`candidate_row_id`、アップロードされた元スコア、学習モデルに必要な特徴量を返します。出力は候補ごとにちょうど1行です。ファイルは入力と同じ形式と行順で、予測確率列が末尾に加わります。[推論のSQL例](examples/sqlite/inference/inference.sql)

推論時の列契約は学習済みモデルから読み取ります。推論のTOMLは空またはコメントだけのファイルを指定します。推論で上書きする設定項目はなく、設定値がある場合はエラーになります。

### seedと評価

`--seed` は既定値42で、範囲は0〜2147483647です。学習ではCatBoostとSQL、推論ではSQLの乱数に使います。SQLiteは接続ごとに乱数を固定します。Redshiftは同じ接続で `SET SEED` を実行します。分散実行における行の割り振りによっては、同じseedでも抽出結果が変わります。[RedshiftのRANDOMの仕様](https://docs.aws.amazon.com/redshift/latest/dg/r_RANDOM.html)

元スコアのみの場合も特徴量を使う場合も、CatBoostを `train` で学び、`calibrate` でその出力にIsotonic Regressionを当てます。元スコアの単調制約は設定しません。`validation` ではCatBoostの出力とIsotonic適用後の確率を同じ行で比較します。元スコアのすべてが0〜1に収まる場合は、元スコアを確率扱いした比較指標もJSONに保存します。

CatBoostはCPUで200反復、木の深さ4、1スレッドで学習します。これらは現在の実装で固定です。seedはCLIから渡し、クラス重み付けと単調制約は使いません。他のパラメータはCatBoostの既定値を使います。

評価には件数、検証の実績率、ECE、Brier score、ビンごとの予測平均と実績率を使います。プロットの横軸は平均予測確率、縦軸は実績率です。学習・校正に使った紹介済み履歴と全候補で分布が異なる場合は、検証値の適用範囲に注意してください。

校正モデルの読み込みには、同じ依存関係と信頼できるexperimentの保存物を使ってください。実験の入力と出力は原則Gitの対象外です。合成データの `experiments/example_oubo/` とSQLite実行例のCSVは保存します。DBとモデルは除外します。DBの再生成方法とActions artifactからの取得方法は [SQLiteの実行例](examples/sqlite/README.md) を参照してください。

## demo GUI

パイプラインの学習と推論をブラウザから試せるデモです。入力フォームはSQLite実行例で事前入力されており、ボタンを押すと本物のCLIが別プロセスで同期実行され、結果（状態・CLI出力・指標JSON・校正プロット・校正後スコアの表）が同じページに表示されます。生成物は `experiments/demo_gui/` に保存され、Gitの対象外です。

```powershell
uv sync --locked
uv run --locked python -m calibration_pipeline.demo_gui
```

起動すると http://127.0.0.1:8000/ をブラウザで開きます（ポートが使用中の場合は別の空きポート）。学習タブと推論タブを切り替えられ、初回は「fixtureを再生成する」ボタンで `examples/sqlite/` のデモデータを作れます。依存関係の追加はしていません。

[開発用README](dev/README.md)


### 最初の一回の進め方

GUIでは **固定fixture DBの準備 → 内容確認 → 学習 → 推論 → 結果確認** の順に進みます。使用DBは `examples/sqlite/train/sqlite_fixture.db` に固定です。最初の画面でテーブル・ビュー、列名・型、件数、最大5行のサンプルを読み取り専用で確認できます。DBが無い・開けない場合は画面の案内に従って生成または読み取り権限を確認します。学習・推論も表示された同じDBをコピーして使用します。

初回は「fixtureを再生成する」で合成DBと候補CSVを作成し、内容を確認します。experiment名（例：`sqlite_oubo_demo`）を入力して既定の設定で学習し、推論タブでも同じ名前を入力して実行してください。再生成は既存のデモDB・候補CSV・manifestを置き換えます。

デモは合成の紹介履歴から紹介後3暦月以内の応募確率を学び、新しい候補8件の `oubo_prob` を出力します。学習・校正・検証の違い、DBの主要列、入力の役割、評価指標と確率の読み方は[初回利用ガイド](docs/quickstart.md)にまとめています。GUI内のリンクから同じ利用ガイドのHTMLを開けます。学習configの詳細は本READMEの「学習設定とSQL」を参照してください。
