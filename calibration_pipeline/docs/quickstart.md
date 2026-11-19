# クイックスタート

## 初めて試す方へ

元スコアは順位を表せても、その数値が応募率とは限りません。確率校正では、予測値と実際の到達率が対応するように調整します。このデモでは合成の紹介履歴を使い、「紹介後3暦月以内の応募確率」を新しい候補8件について計算できます。実在の求職者・求人データは使いません。

- **学習**：train区分で元スコアと特徴量からCatBoostを学びます。
- **校正**：別のcalibrate区分でCatBoostの出力をIsotonic Regressionで調整します。
- **検証**：学習・校正で使っていないvalidation区分で予測と実績を比較します。
- **推論**：保存した学習済みモデルを新しい候補に適用します。再学習はしません。

GUIの「学習」ボタンは学習・校正・検証をまとめて実行します。入力DBは履歴・特徴量の保管先、SQLは必要な行・列を取り出す手順、configはスコア・ラベル・特徴量・出力列の指定です。候補CSVは推論対象の元スコアを持ちます。configの各項目・列契約の詳細は[入力・出力と実行の流れ](guide.md)を参照してください。


## demo GUIで学習と推論を試す

Python 3.11以上とuvを使います。リポジトリを取得し、`calibration_pipeline/` で実行してください。
依存関係の追加はありません。

```powershell
uv sync --locked
uv run --locked python -m calibration_pipeline.demo_gui
```

ブラウザが開きます（既定は `http://127.0.0.1:8000/`）。最初に固定fixture DBの内容確認が表示され、その下に学習・推論のタブがあります。

1. **準備**：GitHub Actionsの `calibration-pipeline-fixture` artifact（保持期間14日）を `examples/sqlite/` に展開してDBと候補CSVを用意します（[取得方法](https://github.com/ando-ar/calibrate_prob/blob/main/calibration_pipeline/examples/sqlite/README.md)）。CLIでローカル生成する場合は `uv run --locked python examples/sqlite/prepare_example.py` を実行します（同じリポジトリの `../synthetic_funnel/` が必要です。市場専用CLIを1回呼び、市場内warmupで学習したモデル付きのDBを生成します）。
2. **内容確認**：使用DBの絶対パスと、各テーブル・ビューの列名・宣言型・件数・最大5行のサンプルを確認します。ビューなどの型は未宣言の場合があります。件数はSQLで学習対象を絞る前の件数です。warmup期間の紹介は `model_*_score` が `NULL` で、`scored_referrals` ビューには含まれません。
3. **学習**：experiment名に `sqlite_oubo_demo` などを入力し、初回は事前入力されたconfig・SQLで「学習を実行する」を押します。
4. **推論**：推論タブで同じexperiment名を入力します。既定の候補CSVと最新の成功runで「推論を実行する」を押します。
5. **結果確認**：学習の指標・プロットと推論の確率表を確認します。結果欄のrunディレクトリに保存ファイルがあります。

GUIのDBは `examples/sqlite/train/sqlite_fixture.db` に固定され、任意のDBへの変更やfixtureなしの実行はできません。閲覧は読み取り専用で、学習・推論も同じパスのDBをrunにコピーし、メモリへ読み込んで使います。生成後は更新された内容が表示されます。外部でDBを変更した場合は「内容を再読み込み」で再確認してください。

DBが無い・開けない場合は、上記のartifact取得または次のコマンドを使います。読み取り権限も確認してください。生成に失敗した場合はCLI出力を開き、依存関係と `synthetic_funnel/` の配置を確認します。

```powershell
uv run --locked python examples/sqlite/prepare_example.py
```

### デモDBの読み方

| テーブル・ビュー | 意味・主な列 |
| --- | --- |
| `user` / `item` | 求職者・求人のマスター。参加時に決まり在籍中は不変の属性（`user_skill` / `occupation` など）を持ちます |
| `user_latent` / `item_latent` | 参加時に決まり在籍中は不変の隠れた意欲。既存の学習・推論SQLでは使用しません |
| `events` | synthetic_funnelが生成した紹介ごとの観測履歴。`model_apply_score` は市場内warmupで学習したモデルの元スコア（warmup中は `NULL`）。属性列はマスターへ分離済み |
| `events_latent` | 生成時の潜在確率・潜在イベント時刻などの検証用データ。既定の学習・推論SQLでは使用しません |
| `daily` | 市場の日次集計。既定の学習・推論SQLでは使用しません |
| `scored_referrals`（ビュー） | 学習用にID・日時を変換した紹介履歴。`user_id` / `item_id` は整数化したID、`shoukai_dt` は紹介日時、`oubo_dt` は応募日時、`model_apply_score` は元スコア。属性は `user` マスターをJOINして取得し、`model_apply_score IS NOT NULL` でwarmup中のスコア欠損行を除外します |
| `user_feature_daily` | 紹介日ごとの求職者特徴量。`snapshot_date` は基準日、`seeker_skill` は `user.user_skill`、`occupation` は `user.occupation` 由来。推論候補分の特徴量も含みます |

`label`（期限内応募なら1、そうでなければ0）と `row_role`（train / calibrate / validation）は学習SQLで作る列で、DBの保存列ではありません。候補CSVの `user_id`、`item_id`、`model_apply_score`、`shoukai_dt` は推論時に一時テーブル `calibration_candidates` へ入り、特徴量との結合に使います。このテーブルは閲覧対象の元DBには保存されません。

ボタンを押すと本物のCLI（`python -m calibration_pipeline.cli`）が別プロセスで同期実行され、
完了後に同じページへ次の内容が表示されます。

| タブ | 表示内容 |
| --- | --- |
| 学習 | 状態とCLI出力、`calibration_metrics.json`（件数・実績率・ECE・Brier・ビン毎）、`calibration_plot.svg`、runディレクトリ |
| 推論 | 状態とCLI出力、使用した学習run、`calibrated_score` の候補ごとの表 |

学習と推論のフォームはexperiment名（model name）・seed・config・SQL・候補ファイルを変更できます。DBは固定です。
推論では、使用する学習runを「最新」または過去の成功runから選べます（`--train-run` に相当します）。
生成物は `experiments/demo_gui/<model_name>/...` に保存され、Gitの対象外です。
入力が不正な場合はCLIのエラーメッセージがそのまま表示されます。

### 結果の読み方

既定の `oubo_prob` は0〜1の応募確率です。0.2なら、似た条件の候補を集めたときに約20%が応募するという予測で、個人の応募を保証しません。候補と同じ行順でCSVの末尾に確率列が加わります。

- `calibration_metrics.json`：検証の件数・実績率、ECE（ビン単位の予測と実績のずれ）、Brier score（確率と0/1の結果の平均二乗誤差）を確認します。両指標は小さいほど良好です。校正前後を同じ検証行で比較します。
- `calibration_plot.svg`：横軸は平均予測確率、縦軸は実績率です。対角線に近いほど整合しています。ビンごとの件数も確認し、少数の行によるばらつきに注意します。
- `calibration_model.pkl`：推論に使う保存モデルです。
- `calibrated_score.csv`：各候補の確率。Parquet入力なら出力もParquetです。

GUIの保存先は `experiments/demo_gui/<model>/train/<run_id>/output/` と `experiments/demo_gui/<model>/inference/<run_id>/output/` です。各runの `input/` にDBを含む入力のコピー、`run_metadata.json` に状態と入力ハッシュを保存し、推論では使用した学習runも記録します。合成データでの良好な指標は実データでの性能を保証しません。

## SQLiteでCLIから学習と推論を試す

demo GUIと同じ流れをコマンドで実行します。fixture生成には、同じリポジトリの `../synthetic_funnel/` が必要です。fixture一式は[SQLiteの実行例](https://github.com/ando-ar/calibrate_prob/blob/main/calibration_pipeline/examples/sqlite/README.md)に従ってActionsのartifactから取得することもできます（保持期間14日）。

```powershell
uv sync --locked
uv run --locked python examples/sqlite/prepare_example.py
uv run --locked calibration-pipeline train --model-name sqlite_oubo_demo --sqlite --sqlite-fixture examples/sqlite/train/sqlite_fixture.db --config examples/sqlite/train/config.toml --sql examples/sqlite/train/train.sql
uv run --locked calibration-pipeline inference --model-name sqlite_oubo_demo --sqlite --sqlite-fixture examples/sqlite/train/sqlite_fixture.db --config examples/sqlite/inference/config.toml --sql examples/sqlite/inference/inference.sql --original-score examples/sqlite/inference/original_score.csv
```

学習結果は `experiments/sqlite_oubo_demo/train/<run_id>/output/`、推論結果は
`experiments/sqlite_oubo_demo/inference/<run_id>/output/` に保存されます。
推論の `calibrated_score.csv` には入力候補と同じ行順で確率列が加わります。
繰り返し実行すると新しいrunが作られます。DBはGitに含めず、上記スクリプトで再生成するか、Actionsのartifactから取得します。

## Redshiftで実行する

`uv sync --locked --extra redshift` を実行し、`REDSHIFT_HOST`、`REDSHIFT_DATABASE`、
`REDSHIFT_USER`、`REDSHIFT_PASSWORD` を環境変数に設定します。
`REDSHIFT_PORT` の既定値は5439です。読み取り権限に加え、推論には一時テーブルの作成権限が必要です。

```powershell
uv run --locked --extra redshift calibration-pipeline train --model-name my_model --config job/train.toml --sql job/train.sql
uv run --locked --extra redshift calibration-pipeline inference --model-name my_model --config job/inference.toml --sql job/inference.sql --original-score job/original_score.parquet
```

SQLiteのSQL例はRedshift向けに調整してください。設定・SQLの契約は[入力・出力と実行の流れ](guide.md)で説明しています。

