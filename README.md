# calibrate_prob

求人紹介の予測スコアの確率校正と、その検証に使う合成データ・推薦実験のプロジェクトです。

| パッケージ | 役割 |
| --- | --- |
| [calibration_pipeline](calibration_pipeline/README.md) | CatBoostとIsotonic Regressionによる校正、学習・推論の実験管理 |
| [synthetic_funnel](synthetic_funnel/README.md) | 紹介・応募・書類通過・決定の合成履歴を生成 |
| [product_prototype](product_prototype/README.md) | データ取得方法、モデル、金額を考慮した推薦順位の比較 |

Pythonの依存関係は各パッケージのディレクトリで `uv sync --locked` により準備します。実行方法と検証手順は各READMEを参照してください。

## Issueベースの開発

1. 作業前にGitHub Issueを作り、目的・変更範囲・完了条件を記載します。
2. `issue-<番号>-<短い説明>` のブランチで実装します。
3. 関連するテストと各ディレクトリの `AGENTS.md` に定めた検査を実行します。
4. PRに変更内容と検証結果を記載し、`Closes #<番号>` でIssueを関連付けます。
5. レビュー後にmainへマージします。

初回clone後は `git config core.hooksPath .githooks` を実行してください。コミット時には校正パイプラインのRuff・型検査が実行されます。

## 保存するもの

ソース、テスト、文書、再現可能な設定例、依存関係のロックファイルをGitで管理します。認証情報、実データ、学習済みモデル、ローカル環境は含めません。生成データ・実験出力は原則除外し、`calibration_pipeline/experiments/example_oubo/` とSQLite実行例の合成CSVのみ保存します。DBは除外し、再生成方法を文書化しています。IssueやPRにも認証情報・個人情報・実データを貼り付けないでください。
