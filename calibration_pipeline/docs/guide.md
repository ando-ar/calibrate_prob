# 入力・出力と実行の流れ

## 学習

学習TOMLの例です。設定値の検証は {py:func}`calibration_pipeline.config.load_config` が行います。

```{literalinclude} ../examples/sqlite/train/config.toml
:language: toml
```

SQLは元スコア・特徴量・確定二値ラベルと `row_role` を返します。

| row_role | 用途 |
| --- | --- |
| train | CatBoostの学習 |
| calibrate | 後続期間でIsotonic Regressionによる校正 |
| validation | さらに後続期間での評価 |

SQL作成者が期間の順序・重複・ラベルの確定・母集団・特徴量の取得時点を管理します。
数値特徴量はNULL・NaNを欠損として扱いますが、無限値は使えません。
元スコアは欠損のない有限値、ラベルは数値の0か1、カテゴリ特徴量は文字列または数値の列です。
1,2,3のような数値コードを順序として扱うかカテゴリとして扱うかは、設定のどちらのリストに
列を入れるかで決まります。カテゴリのNULLや学習時に現れなかった値も使用できます。

SQLite用の抽出例です。

```{literalinclude} ../examples/sqlite/train/train.sql
:language: sql
```

学習・校正の処理は {py:func}`calibration_pipeline.pipeline.fit`、評価は
{py:func}`calibration_pipeline.evaluation.metrics` を参照してください。
検証データで校正前後のECE・Brier score・ビンごとの確率と実績率を計算し、JSONとSVGに保存します。

保存されるSVGは、横軸にビンごとの平均予測確率、縦軸に実績率を取り、
Isotonic適用前後の2系列を対角線（理想的な校正）と並べて示します。
例の合成データで学習したrunのプロットが次の通りです。

```{figure} _static/calibration_plot_example.svg
:alt: 検証データでの校正プロット。横軸が平均予測確率、縦軸が実績率で、Before isotonic と After isotonic の2系列を対角線と並べて示す。
:width: 480px

例のrun（`sqlite_oubo_demo`）で保存された校正プロット。点が対角線に近いほど校正されている。
```

## 推論

候補はUTF-8 CSVまたはParquetで、1行1候補です。元スコア、`user_id`、`item_id`、
特徴量の結合に使う列を含めます。ユーザーIDは10000000〜19999999、求人IDは20000000〜29999999です。
CSVでは8桁の数字、Parquetでは整数型を使います。

入力に `candidate_row_id` を付け、SQLと同じ接続の一時テーブル `calibration_candidates` にアップロードします。
推論SQLは各候補に対してちょうど1行、`candidate_row_id`・元スコア・必要な特徴量を返してください。
推論TOMLは空かコメントのみです。列の設定は学習済みモデルから読み込みます。

```{literalinclude} ../examples/sqlite/inference/inference.sql
:language: sql
```

出力は入力と同じ形式・行順で、確率列を追加します。
入力検証と行の対応は {py:mod}`calibration_pipeline.contracts` に実装されています。

## experimentと再現

`--experiments-dir` の既定値は `experiments/` です。
`<model_name>/<trainまたはinference>/<run_id>/` の下に次のファイルを保存します。

| 保存先 | 内容 |
| --- | --- |
| input/ | 設定・SQL・候補・指定したSQLite DBのコピー |
| run_metadata.json | 開始・終了・状態・入力SHA-256、推論で使った学習run |
| output/ | 成功時に公開するモデル・指標・SVG、または推論結果 |

run IDはJSTの開始時刻で、同秒の実行には連番が付きます。
推論開始時に、成功した学習runのうち開始時刻が最も新しいものを選びます。
`--train-run <run_id>` で固定できます。モデルは信頼できるexperimentの保存物を使用してください。

`--seed` の既定値は42です。学習ではCatBoostとSQL、推論ではSQLに使います。
Redshiftの分散実行では、同じseedでも行の割り振りにより抽出結果が変わることがあります。

