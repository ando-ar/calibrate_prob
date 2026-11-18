# calibrate_prob

求人紹介の予測スコアの確率校正と、その検証に使う合成データ・推薦実験のプロジェクトです。

| パッケージ | 役割 |
| --- | --- |
| [calibration_pipeline](calibration_pipeline/README.md) | CatBoostとIsotonic Regressionによる校正、学習・推論の実験管理 |
| [synthetic_funnel](synthetic_funnel/README.md) | 紹介・応募・書類通過・決定の合成履歴を生成 |
| [product_prototype](product_prototype/README.md) | データ取得方法、モデル、金額を考慮した推薦順位の比較 |

Pythonの依存関係は各パッケージのディレクトリで `uv sync --locked` により準備します。実行方法と検証手順は各READMEを参照してください。

校正パイプラインのドキュメントは、[GitHub ActionsのDocumentation](https://github.com/ando-ar/calibrate_prob/actions/workflows/docs.yml) の成功した実行から `calibration-pipeline-docs` artifact（保持期間14日）をダウンロードし、ZIPをすべて展開して `index.html` をブラウザーで開くと読めます。取得後の閲覧にPython環境やWebサーバーは不要です。生成HTMLはGit管理せず、ローカルでも生成できます。詳しくは[ドキュメント開発手順](calibration_pipeline/docs/development.md)を参照してください。

## 最初に試すデモ

[初回利用ガイド](calibration_pipeline/docs/quickstart.md)では、合成DBの準備 → 内容確認 → 学習 → 推論 → 結果確認をブラウザーで試せます。元スコアは順位づけの値であり、そのまま応募率を意味するとは限りません。デモでは履歴からそのずれを校正し、新しい候補の応募確率を出力します。

`synthetic_funnel` が合成履歴・候補を生成し、`calibration_pipeline` が学習・校正・検証と推論を実行します。`product_prototype` は推薦方法や金額を考慮した順位の比較実験用で、このデモの実行には不要です。GUIでは固定fixture DBの構造とデータ例を実行前に確認できます。

## Issueベースの開発

1. 作業前にGitHub Issueを作り、目的・変更範囲・完了条件を記載します。
2. `issue-<番号>-<短い説明>` のブランチで実装します。
3. 関連するテストと各ディレクトリの `AGENTS.md` に定めた検査を実行します。
4. PRに変更内容と検証結果を記載し、`Closes #<番号>` でIssueを関連付けます。
5. レビュー後にmainへマージします。

初回clone後は `git config core.hooksPath .githooks` を実行してください。コミット時には校正パイプラインのRuff・型検査が実行されます。

## 保存するもの

ソース、テスト、文書、再現可能な設定例、依存関係のロックファイルをGitで管理します。認証情報、実データ、学習済みモデル、ローカル環境は含めません。生成データ・実験出力は原則除外し、`calibration_pipeline/experiments/example_oubo/` とSQLite実行例の合成CSVのみ保存します。DBは除外し、再生成方法を文書化しています。IssueやPRにも認証情報・個人情報・実データを貼り付けないでください。
