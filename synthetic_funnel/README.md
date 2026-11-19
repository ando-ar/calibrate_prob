# synthetic-funnel

継続市場のシミュレーションで、紹介から応募、書類通過、入社決定までの履歴を生成するPythonライブラリです。紹介時点の特徴、観測された結果、シミュレーター内の真の確率を使って、校正やモデル評価を試せます。

## HTMLドキュメント

`synthetic_funnel/` で以下を実行し、生成された `dist/docs/index.html` をブラウザーで開きます。

```powershell
uv run --locked --group docs sphinx-build -b html -W docs dist/docs
```

## Markdown原稿

| やりたいこと | 参照先 |
| --- | --- |
| 少量の市場データを生成してSQLiteで見る | [クイックスタート](docs/QUICKSTART.md) |
| 市場のルールとスコアの学習方法を読む | [継続市場のシミュレーション](docs/MARKET.md) |
| 生成条件を設定する、別のPythonプロジェクトから呼ぶ | [設定とPython API](docs/API.md) |
| 出力形式を選ぶ、列や潜在値の意味を調べる | [出力データ仕様](docs/DATA_FORMAT.md) |
| 真の条件付き確率と基準率の計算方法を読む | [紹介履歴の確率モデル](docs/PROBABILITY_MODEL.md) |
| SQLiteを校正パイプラインの入力に使う | [校正パイプラインのfixture作成手順](../calibration_pipeline/examples/sqlite/README.md) |

`synthetic_funnel/` で `uv sync --locked` を実行すると依存ライブラリを準備できます。同じディレクトリで以下を実行してテストします。

```powershell
uv run --locked pytest -q
```
