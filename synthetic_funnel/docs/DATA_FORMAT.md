# 出力データ仕様

継続市場の出力も `events` と `truth` の列を共有します。市場の追加テーブルと、共通列の値が指す範囲は[継続市場のシミュレーション](MARKET.md)を参照してください。

| 指定 | `output/` に作られるデータ | 主な用途 |
| --- | --- | --- |
| `--format sqlite`（既定） | `dataset.sqlite.db` の `events`、`truth` テーブル | 1つのDBとして扱い、SQLで検索・結合する。 |
| `--format parquet` | `events.parquet`、`truth.parquet` | Pythonで列単位に読み込み、分析する。 |
| `--format csv` | `events.csv`、`truth.csv` | 数行を目視で確認したり、テキストとして受け渡したりする。 |

どの形式でも `manifest.json` が付きます。元モデルスコアを有効にすると `legacy_models.json` に各段階の学習条件と係数も保存されます。

## `events` 紹介時点の特徴と観測結果

通常の生成器では1行が実際に紹介した1ペアを表します。生成内で `event_id` と `user_id` は一意で、`item_id` は複数行に現れます。すべて文字列です。SQLiteの2テーブルでは `event_id` がそれぞれ主キーで、`events.shoukai_dt` にはインデックスがあります。

| 列 | 内容 |
| --- | --- |
| `event_id`, `user_id`, `item_id` | 紹介イベント、求職者、求人のID。`event_id` で `truth` と1対1に結合できる。 |
| `seeker_skill`, `required_skill`, `desired_salary`, `offered_salary` | 求職者・求人のスキルと給与の設定値。 |
| `wants_remote`, `allows_remote` | リモート希望・可否を示す0/1。 |
| `skill_match`, `skill_shortfall`, `overqualification`, `salary_shortfall`, `remote_mismatch` | 上記の属性から計算した適合度、スキル不足・過剰、給与不足、リモート条件の不一致。 |
| `other_applications`, `competing_candidates` | 紹介時点の他社応募件数、同じ求人で選考中の他候補者数。 |
| `legacy_apply_score`, `legacy_screen_score`, `legacy_decision_score` | 各段階の元モデルスコア。学習を無効にした場合、出力ファイル・テーブルではこの3列の値が欠損になる。 |
| `shoukai_dt`, `oubo_dt`, `shorui_tsuuka_dt`, `kettei_dt` | 紹介、応募、通過、決定の日時。 |

日時はUTCオフセット付きISO 8601の文字列です。未到達や `as_of` より後の日時はSQLiteとParquetで `NULL`、CSVで空欄です。3暦月以内の結果を評価する場合は、紹介からの期限と観測締切を比較してラベルが成熟した行を選び、到達日時を期限と比較します。通過の分母は期限内の応募者、決定の分母は期限内の通過者です。

## `truth` 評価用の真値と潜在結果

`<stage>` は `application`、`screening`、`decision` のいずれかです。`truth` は、真値との比較や生成器の診断に使います。学習や推論の特徴量は `events` の紹介時点の列から選びます。

| 列 | 内容 |
| --- | --- |
| `event_id` | `events` と共通の紹介イベントID。 |
| `p_<stage>_eventual` | 前段の到達を条件とした、期限を設けない真の移行確率。紹介時点の特徴と隠れた相性が与えられた値。 |
| `p_candidate_<stage>`, `p_employer_<stage>` | 各段階の求職者側・企業側の確率。積が `p_<stage>_eventual` になる。 |
| `seeker_affinity`, `employer_affinity` | 観測データに現れない両者の相性。 |
| `failure_stage`, `failure_reason` | 潜在的に到達が途切れた段階と理由。理由は `not_applied`、`employer_rejection`、`candidate_withdrawal` のいずれか。全段階を通過した行は欠損。 |
| `application_screening_gap`, `relative_application_screening_gap` | 応募と通過の確率の差、各基準率で割った値の差。決定のペナルティには後者を使う。 |
| `latent_oubo_dt`, `latent_shorui_tsuuka_dt`, `latent_kettei_dt` | 観測打切りを適用する前の到達日時。永久に未到達なら欠損。 |

応募・通過・決定の真値の積は、同じペアの期限を設けない紹介から決定までの確率です。3暦月以内の確率を評価する際は、所要時間も考慮します。[紹介履歴の確率モデル](PROBABILITY_MODEL.md)

## 実行条件と集計

`manifest.json` に全設定とseed、パッケージとPythonのバージョン、出力形式、データファイルのSHA-256が入ります。同じ条件を再現するときは、設定・seedと使用したコード・依存環境を揃えます。生成規則や乱数の割り当てが変わると、同じseedから生成される行も変わることがあります。

- `rate_anchor.expected_rates` は基礎確率を合わせるために使った参照集団での期待率です。
- `generated_expected_rates` は出力したペアの潜在確率を前段到達確率で重み付けした期待率です。
- `generated_realized_rates` は抽選後の潜在結果の実績率です。前段到達が0件なら、その段階の率は `null` です。`generated_latent_stage_counts` に到達件数が入ります。

これらの率はどれも `as_of` や3暦月期限による打切りの前に計算されます。観測済みの集計には `events` を使います。
