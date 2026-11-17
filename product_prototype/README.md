# 商用紹介ログでの推薦実験プロトタイプ

紹介済みの user–item ペアを使い、**決定例をどう集めるか、どのモデルを学習するか、金額をどう順位に反映するか**を比較する。モノレポ内の独立したPythonパッケージである。

ユーザーとの確認事項：ログは紹介したペアのみ。未紹介ペアは記録されない。`shoukai_dt` は紹介日＝候補を評価した日、`kettei_dt` は実入社日ではなく入社決定日。特徴量は合意どおり**最新状態**を使用する。この前提を全実験のメタデータに残す。

## 最初に動かす

Python 3.11 以上と uv を用意し、このディレクトリで実行する。Windows PowerShell で動作検証している。

```powershell
uv sync --locked
uv run product-prototype demo
uv run product-prototype run --config configs/example.toml
# 同じコマンドで完了済み実験を再利用し、中断した候補から続ける
uv run product-prototype run --config configs/example.toml
```

デモは合成18,000行・42代表日である。実務の性能や学習時間を再現しない。設定例は取得3通り×分類器3通り×順位式4通り＝36候補。実データに切り替える前に、列名、特徴量、期間、紹介枠、給与単位、手数料率を編集する。

最初に読む資料：

- [過去コンペの知見と実装への反映](COMPETITION_LESSONS.md)
- [データ、ラベル、順位・指標の定義](docs/DATA_AND_EVALUATION.md)
- [実験の増やし方と運用](docs/EXPERIMENTS.md)
- [全設定の例](configs/example.toml)

## Redshift に接続する

```powershell
uv sync --locked --extra redshift
$env:REDSHIFT_HOST = "your-cluster.example.region.redshift.amazonaws.com"
$env:REDSHIFT_DATABASE = "your_database"
$env:REDSHIFT_USER = "your_readonly_user"
# REDSHIFT_PASSWORD は既存の秘密情報管理の手順で環境変数へ設定する
Copy-Item configs/example.toml configs/commercial.toml
```

`commercial.toml` の `source.kind = "redshift"`、`source.table = "schema.table"`、各列名と期間を変更する。`experiment.output_dir` は新しいディレクトリにする。`source.path` は Redshift モードでは使わない。

```powershell
# 接続せず、実行する列一覧・期間パラメータを確認
uv run product-prototype sql --config configs/commercial.toml
# SELECTのみ。抽出結果をローカルのParquetに固定
uv run product-prototype extract --config configs/commercial.toml
uv run product-prototype run --config configs/commercial.toml
```

テーブルは1つの名前を指定する。JOIN や独自の業務条件が必要なら、Redshift 側でビューにまとめる。自由な SQL 文字列は受け付けない。実装は識別子を検証し、期間はバインド変数で渡す。TLS証明書を検証する。パスワードは設定・成果物に保存しない。DBアダプターは SELECT を実行し、終了時に rollback して接続を閉じる。

**大量データ**：CSV／Parquet の読み取りとスナップショット書き出しはバッチ処理する。ただし `redshift_connector` の `fetchmany` は、ドライバー内部の結果全体のバッファリングまで防ぐものではない。数年分がメモリを超える場合は、既存のデータ基盤から UNLOAD 等で Parquet を出力し、ローカルファイルモードに渡す。UNLOAD／S3転送の自動化はこの版には含まない。抽出済みの母集団に対するサンプリングは DuckDB で行い、選択行だけを学習用メモリに載せる。

実DBの接続情報は未提供のため、実接続は未検証。SQL生成、DBアダプターの呼び出し、ローカルでの全処理をテストする。

## 入力テーブル

| 列 | 内容 |
| --- | --- |
| user_id / item_id | 求職者・求人。文字列として保持 |
| shoukai_dt | 紹介日時。未応募にも必須。タイムゾーンは設定で指定 |
| oubo_dt | 応募日時、未応募なら NULL |
| shorui_tsuuka_dt | 書類通過日時、未通過なら NULL |
| kettei_dt | 入社決定日時、未決定なら NULL |
| event_id（任意） | 不変の紹介イベントID。無い場合 user/item/紹介日時から作成 |
| 任意の特徴列 | 数値／カテゴリを明示的に列挙。ID・結果日時・実手数料は直接特徴に指定不可 |
| 給与・料率の列（任意） | 使用する金額代理の設定に対応する列 |
| fee_yen（任意） | 決定した場合の観測手数料。二段階学習／実額指標を使う場合に必要 |

同一ペアが再紹介される場合、結果日時が**その紹介イベントに紐付く**ようにする。最新のペア単位結果を全紹介行へコピーしたテーブルでは、同一決定が複数行へ付く。イベントIDだけではこの問題は解消しない。

実額が無い場合、`labels.fee_column` と `kind="two_stage_fee"` の scorer を除き、`evaluation.selection_metric` を `macro_daily_observed_positive_proxy_yen` 等へ変更する。給与列が無い場合は constant proxy のみを使用し、他の proxy と参照 scorer を除く。説明変数の欠損は学習データだけで補完する。

## できる比較

- 取得：全件／件数上限付き一様抽出、決定全件＋段階別負例、直近月数、月・市場などの均等配分。
- モデル：L2ロジスティック回帰、ヒストグラムGBDT。抽出確率の逆数による行重みを付けた条件も比較可能。
- 順位式：決定予測値、予測値×給与代理、オッズ×給与代理、予測値×条件付き実手数料回帰。
- 金額代理：定数、給与帯中央、給与帯中央と帯内に収めた希望年収の混合。料率列にも対応。
- 金額学習：分類器と別の取得計画、金額例の固定ハッシュ部分集合。
- 枠：一人一日上限、対象人数に比例する総枠、絶対総枠、任意の市場別総枠。

30万行はコンペから持ってきた**設定例の予算**であり、実装上の固定制約ではない。`max_rows` を省くと期間内全件を使う。分類器・金額モデルの行数と成分行数合計を別々に記録する。ハイパーパラメータの数値をコンペの独自最適化の L2 値と直接対応させない。

## 出力と推論

`runs/<実験名>/` にスナップショット、取得行と理由、モデル、検証順位、日別指標、一覧、選出モデル、最終評価を保存する。個人IDを含むため `data/` と `runs/` は Git 対象外。学習・推論データを遠隔サービスへ送信する処理はない。

検証で選出したモデルの名前は `selected.json`、実体は `candidates/<名前>/model.joblib`。二段階方式も1つのファイルに入る。次の推論には結果日時や実手数料は不要。設定済みの特徴量、金額代理の列、user/item/紹介予定日時、設定したイベントIDを渡す。

```powershell
uv run product-prototype predict --config configs/commercial.toml `
  --model runs/commercial/candidates/選出モデル名/model.joblib `
  --input data/today_candidates.parquet --output runs/today_prediction
```

`ranked.parquet` の `selected=true` が紹介対象。`user_rank` は一人一日内、`daily_rank` は上限適用後の市場／日内順位。`support_user.parquet` と `support_item.parquet` は別用途の支援順位スコア。モデルは前処理、分類器、必要なら金額回帰、スコア式をまとめて保存している。

## 検証

```powershell
uv run pytest -q --basetemp=.pytest_tmp
```

ラベル期限、期間境界、抽出の再現性と不足枠再配分、紹介上限、金額の単位と clip、モデル保存後の未知カテゴリ推論、検証選択と最終評価の再利用を確認する。
