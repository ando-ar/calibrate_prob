# 実験の増やし方と再開

## 段階的な比較

最初から大量の直積を作らず、取得計画→分類器→金額→行重みの順に比較すると、何が効いたか説明しやすい。

1. 全期間の段階件数と、各 split の決定数を `cohort.json` で確認する。学習期間の結果遅延・欠損・紹介前の結果日時は `training_profile.json` に出す。最終期間の集計を探索の調整には使わない。
2. ロジスティック回帰・同じ順位式・同じ行数予算で、直近6/12/24/36か月、一様／段階別抽出を比較する。
3. 良かった取得計画を固定してGBDT、正則化、逆抽出確率重みを比較する。
4. 分類器の条件を揃え、定数・給与帯中央・Delta型給与代理・二段階金額を比較する。
5. 金額モデルだけ期間と行数を変え、取得の効果と金額学習の効果を分ける。

各 `datasets` の `rationale` に、期間・件数・比率・均等化を選んだ具体的な仮説を書く。各 `proxies` にも単位・料率・混合値・欠損時金額の根拠を書く。設定例の英文は出発点の仮説であり、実データの根拠にはならない。

## 取得計画

`mode="uniform"` は期間内から固定ハッシュ順で抽出する。`balance_columns` が空なら母集団全体の一様抽出。`balance_columns=["_month", "market"]` のように指定すると、それぞれのセルに同じ上限を配り、少ないセルの余りを他セルへ再配分する。この場合は自然な分布の一様抽出ではなく、セルを均等にする抽出になる。

`mode="stage"` は期間内の全決定を確保してから、残りを通過のみ・応募のみ・未応募へ配る。`negative_shares` は**負例枠に対する希望配分**。不足した枠は通過のみ→応募のみ→未応募の順で埋める。セル均等化は段階内で行う。最終的な実件数を `sampling.json` に出す。正例だけで予算を超える場合は止め、勝手に正例を捨てない。

同じスナップショット・計画・seed は同じ行を返す。選び順は `md5(row_id + seed)` と row_id の順であり、ファイルの物理順に依存しない。seed変更は新規実験として扱う。分類器に渡す行へ最終セルの採択率 `_sampling_probability=選択数/母集団数` を付ける。

取得された学習全体はメモリに載る。`max_rows` はそれを制御する予算である。複数モデルを含む合計行数の強制制限は設けていないが、`component_training_rows` で分類器と金額回帰の使用件数を重複も含めて加算し、比較できるようにする。

## モデルと金額の設定

`[[models]]` を複製し、名前と `parameters` を変える。`parameters` は sklearn の分類器へ渡す。

```toml
[[models]]
name = "gbdt_deeper"
kind = "hist_gradient_boosting"
sample_weight = "inverse_sampling"
parameters = {max_iter = 200, max_leaf_nodes = 31, learning_rate = 0.05}
```

ロジスティック回帰は中央値補完・標準化・カテゴリのone-hotを行う。GBDTは中央値補完、カテゴリを最大128種類へまとめてネイティブカテゴリとして扱う。前処理は学習行だけで fit する。未知カテゴリを推論可能にしている。高カーディナリティの生IDをそのまま特徴にはしない。分類器の補完やエンコードを金額モデルへ使い回さず、金額モデル側もその学習行だけで前処理を fit する。

ロジスティック回帰の未収束を正常な結果に混ぜない。未収束候補は `failure.json` を残す。GBDTの既定 `early_stopping=false` は、学習内ランダム分割を時系列検証の代わりに使わないため。反復数は検証で比較する。

給与代理の式は次のとおり。単位と料率は設定する。

```text
midpoint: S = (L + U) / 2
delta:    S = (1-w) * (L + U) / 2 + w * clip(A, L, U)
fee_proxy = S * salary_unit_yen * fee_rate
```

L/U は求人の給与下限／上限、A は希望年収。`clip(700, 600, 900)=700`。w=0.5なら S=725。Aだけ欠損なら中央額、給与帯が欠損なら `fallback_fee_yen` を使う。給与下限>上限、負の給与帯や料率は失敗とする。`rate_column` があれば有限値の行で料率定数を上書きする。

二段階方式は決定分類器と、決定かつ実額既知の行だけのRidge回帰。金額は円単位を直接回帰し、予測時の負値だけ0へ収める。これはコンペの金額帯中央に対する学習とは異なり、観測実額を使う商用版の初期実装である。外れ値が大きい場合の対数変換やロバスト損失は未実装。

分類器に対して金額データだけを変える例：

```toml
[[scorers]]
name = "epsilon_fee_1000"
kind = "two_stage_fee"
proxy = "delta"
fee_dataset = "stage_36m"
fee_max_rows = 1000
fee_rationale = "Classifier rows held fixed; compare 1000 full-period decisions with all known-fee decisions to test amount-data value."
ridge_alpha = 1.0
```

`fee_dataset` 省略時は分類器と同じ取得行を使う。`fee_max_rows` 省略時はその取得行の決定・実額既知を全件使う。件数制限は全期間の固定ハッシュ部分集合であり、直近データへの置換ではない。金額データの期間を変える場合は `months` の違う dataset を指定する。金額行のID集合ハッシュも保存する。

この例でも全候補は `evaluation.proxy` という**共通の代理額**で評価する。モデルごとに異なる給与代理額を使って自分自身の指標を有利に変えない。

## 中断と再開

同じ `run` コマンドで再開する。完了した抽出スナップショットと候補の検証結果を再利用する。途中で止まった単一モデルの学習は最初からやり直す。モデルの反復途中の状態は保存しない。抽出の途中中断も、その抽出だけを最初からやり直す。完成前のファイルを完成扱いしないよう atomic replace を使う。

出力単位にOSの排他ロックをかける。プロセスが終了するとロックは解放されるので、残った `.run.lock` を手動で消す必要はない。

主な成果物：

```text
experiment.json             設定／コード／依存バージョン／データの識別情報
snapshot/rows.parquet       最新状態の特徴を含む固定入力
snapshot/manifest.json      抽出条件、日時、行数、ファイルハッシュ
cohort.json                 split別の段階件数
training_profile.json       学習期間の観測結果遅延と欠損・日時の診断
datasets/<name>/            training.parquet、sampling.json、sampling.sql
candidates/<name>/          model.joblib、validation.json、failure.json（失敗時）
candidates/<name>/validation/ranked.parquet、daily.csv、support_*.parquet
leaderboard.json            検証指標の一覧
selected.json               最終評価を見る前に選んだモデル
test.json、test/            選出した1モデルだけの最終評価
```

データ・設定・コード・依存バージョンが変わった場合、同じ出力には継ぎ足せない。新しい `output_dir` を使う。DBの最新状態が変わっても同じ出力の完成済みスナップショットを自動更新しない。更新したい場合も別出力にする。

失敗候補は次の通常実行でスキップする。最終結果をまだ開いていない場合だけ `--retry-failed` で再試行できる。最終結果を開いた後に再探索する場合は、新しい期間の独立した最終評価を用意する。ファイル上の変更制限で、同じholdoutを別ディレクトリから人間が再利用することまでは防げない。

金額指標を選ぶ場合は検証・最終の決定行に実額が必要。正例ゼロ、片クラスの学習、行ID重複、必要列欠落、給与帯逆転などはエラーにする。`daily.csv` と少数例の `ranked.parquet` を確認し、選出値だけで良し悪しを決めない。

## 実データで次に足すと有益なもの

まず担当者別・求人別などの業務制約と、結果のイベント帰属を確定する。次に、過去の分割をずらした複数の時点で比較する。現版の runner は1つの train/validation/test を管理するので、時点ごとに別設定・別出力を用いる。

未紹介候補のログが無いため、推薦の新方策を完全にオフライン評価することはできない。シャドー推論で全候補・スコア・採用結果・日時を記録できるようになれば、候補集合の変化を確認できる。その後のオンライン比較で、選考処理量・決定・実収益を確認する。

技術仕様は [Amazon Redshift Python connector](https://docs.aws.amazon.com/redshift/latest/mgmt/python-connect-examples.html)、[接続設定](https://docs.aws.amazon.com/redshift/latest/mgmt/python-configuration-options.html)、[sklearn HistGradientBoostingClassifier](https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html) を参照。
