# 出力データ仕様

出力は求職者マスター `user`、求人マスター `item`、紹介履歴 `events`、潜在値 `events_latent`、日次集計 `daily` の計5テーブルです。データは[継続市場のシミュレーション](MARKET.md)だけで生成します。市場のルールと生成手順は MARKET.md を、確率の計算方法は[紹介履歴の確率モデル](PROBABILITY_MODEL.md)を参照してください。

## 出力形式とファイル名

| 指定 | `output/` に作られるデータ | 主な用途 |
| --- | --- | --- |
| `--format sqlite`（既定） | `dataset.sqlite.db` の `user`、`item`、`events`、`events_latent`、`daily` テーブル | 1つのDBとして扱い、SQLで検索・結合する。 |
| `--format parquet` | `user.parquet`、`item.parquet`、`events.parquet`、`events_latent.parquet`、`daily.parquet` | Pythonで列単位に読み込み、分析する。 |
| `--format csv` | `user.csv`、`item.csv`、`events.csv`、`events_latent.csv`、`daily.csv` | 数行を目視で確認したり、テキストとして受け渡したりする。 |

どの形式でも `manifest.json` が付きます。フィールド一覧はこの文書の後半にあります。

## テーブルの単位とキー

| テーブル | 1行の単位 | 主キー | 外部キー |
| --- | --- | --- | --- |
| `user` | 求職者の1参加 | `user_id` | なし |
| `item` | 求人の1掲載 | `item_id` | なし |
| `events` | 紹介1件 | `event_id` | `user_id` → `user`、`item_id` → `item` |
| `events_latent` | 紹介1件（`events` と1対1） | `event_id` | `event_id` → `events` |
| `daily` | 市場の1日 | `day_dt` | なし |

`user` と `item` は1参加=1行で、在籍中は属性が不変です。そのため `events` には属性列を持たず、必要な属性はマスター結合で取得します。紹介時点の属性の値はマスターの値と一致します。

```sql
SELECT e.event_id, u.user_skill, i.required_skill
FROM events AS e
JOIN user AS u USING (user_id)
JOIN item AS i USING (item_id)
```

型の表記はSQLiteの宣言です。Parquetでは TEXT=string、INTEGER=int64、REAL=double、CSVでは空欄がNULLに相当します。日時はUTCオフセット付きISO 8601の文字列です。

## `user` 求職者マスター

| 列 | 型 | NULLの意味 | 内容 | 値の例 |
| --- | --- | --- | --- | --- |
| `user_id` | TEXT | なし（主キー） | 求職者のID。 | `user-000000042` |
| `arrival_dt` | TEXT | なし | 市場への参加日時。 | `2025-01-05T00:00:00+09:00` |
| `user_skill` | REAL | なし | スキル。0〜1。 | `0.62` |
| `desired_salary` | REAL | なし | 希望給与。正の実数。 | `860.0` |
| `occupation` | INTEGER | なし | 職種。 | `1` |
| `region` | INTEGER | なし | 勤務地。 | `2` |
| `wants_remote` | INTEGER | なし | リモート希望。1=希望、0=不希望。 | `1` |
| `user_affinity` | REAL | なし | 隠れた相性（潜在値）。観測データに現れない。 | `0.31` |
| `departure_dt` | TEXT | NULL=観測締切時点で在籍中 | 退出日時。退出理由は保存しない。 | NULL |

## `item` 求人マスター

| 列 | 型 | NULLの意味 | 内容 | 値の例 |
| --- | --- | --- | --- | --- |
| `item_id` | TEXT | なし（主キー） | 求人のID。 | `item-0007` |
| `arrival_dt` | TEXT | なし | 市場への参加（掲載開始）日時。 | `2025-01-06T00:00:00+09:00` |
| `required_skill` | REAL | なし | 要求スキル。0〜1。 | `0.55` |
| `offered_salary` | REAL | なし | 提示給与。正の実数。 | `900.0` |
| `occupation` | INTEGER | なし | 職種。 | `1` |
| `region` | INTEGER | なし | 勤務地。 | `2` |
| `allows_remote` | INTEGER | なし | リモート可否。1=可、0=不可。 | `0` |
| `item_affinity` | REAL | なし | 隠れた相性（潜在値）。観測データに現れない。 | `-0.12` |
| `departure_dt` | TEXT | NULL=観測締切時点で掲載中 | 退出（掲載終了）日時。退出理由は保存しない。 | `2025-02-15T00:00:00+09:00` |

## `events` 紹介と観測された結果

1行が紹介1件です。`event_id` は一意で、`events_latent` と1対1に結合できます。同じ `user_id`、`item_id` は複数行に現れます。属性列はマスター結合で取得する設計のため、このテーブルにはペアから計算した特徴量と観測結果だけが入ります。SQLiteでは `events.shoukai_dt` にインデックスがあり、外部キー制約が有効です。

| 列 | 型 | NULLの意味 | 内容 | 値の例 |
| --- | --- | --- | --- | --- |
| `event_id` | TEXT | なし（主キー） | 紹介イベントのID。 | `market-000000042-0007` |
| `user_id` | TEXT | なし | 紹介された求職者。`user.user_id` を参照。 | `user-000000042` |
| `item_id` | TEXT | なし | 紹介された求人。`item.item_id` を参照。 | `item-0007` |
| `skill_match` | REAL | なし | スキル適合度。`1 - abs(user_skill - required_skill)`。 | `0.93` |
| `skill_shortfall` | REAL | なし | スキル不足。`max(required_skill - user_skill, 0)`。 | `0.0` |
| `overqualification` | REAL | なし | スキル過剰。`max(user_skill - required_skill, 0)`。 | `0.07` |
| `salary_shortfall` | REAL | なし | 給与不足率。`max((desired_salary - offered_salary) / desired_salary, 0)`。 | `0.0` |
| `remote_mismatch` | INTEGER | なし | リモート条件の不一致。`wants_remote × (1 - allows_remote)`。 | `1` |
| `other_applications` | INTEGER | なし | 紹介時点の他社応募件数。 | `2` |
| `competing_candidates` | INTEGER | なし | 紹介時点の同じ求人で選考中の他候補者数。 | `3` |
| `model_apply_score` | REAL | warmup期間中の紹介はNULL | ウォームアップ学習モデルの応募段階スコア。 | `0.0121` |
| `model_screen_score` | REAL | warmup期間中の紹介はNULL | ウォームアップ学習モデルの書類通過段階スコア。 | `0.0987` |
| `model_decision_score` | REAL | warmup期間中の紹介はNULL | ウォームアップ学習モデルの決定段階スコア。 | `0.0614` |
| `shoukai_dt` | TEXT | なし | 紹介日時。 | `2025-01-05T00:14:24+09:00` |
| `oubo_dt` | TEXT | 未到達ならNULL | 観測された応募日時。 | `2025-01-08T10:26:00+09:00` |
| `shorui_tsuuka_dt` | TEXT | 未到達ならNULL | 観測された書類通過日時。 | NULL |
| `kettei_dt` | TEXT | 未到達ならNULL | 観測された決定日時。 | NULL |

紹介から応募・通過・決定へ進む様子は市場の退出や掲載期限で途切れることがあり、未到達の段階はNULLです。締切までに届かなかった日時もNULLになります。

## `events_latent` 潜在値（反実仮の真値）

`<stage>` は `application`、`screening`、`decision` のいずれかです。このテーブルの値はすべて紹介時点で確定する潜在値で、退出・掲載期限・相手の退出による選考取消・観測打切りを無視し、紹介後も仲介が継続した場合の真値（反実仮）です。実績の選考取消理由は保存しません。校正やモデル評価の真値との比較、生成器の診断に使います。学習や推論の特徴量は `events` の紹介時点の列とマスター結合した属性から選びます。

| 列 | 型 | NULLの意味 | 内容 | 値の例 |
| --- | --- | --- | --- | --- |
| `event_id` | TEXT | なし（主キー） | `events` と共通の紹介イベントID。 | `market-000000042-0007` |
| `p_<stage>_eventual` | REAL | なし | 前段の到達を条件とした、期限を設けない真の移行確率。 | `0.112` |
| `p_candidate_<stage>` | REAL | なし | 各段階の求職者側の確率。 | `0.310` |
| `p_employer_<stage>` | REAL | なし | 各段階の企業側の確率。応募は求職者側だけで決まるため、`p_employer_application` は1。 | `0.361` |
| `user_affinity` | REAL | なし | 紹介した求職者の隠れた相性。`user` の値と同じ。 | `0.31` |
| `item_affinity` | REAL | なし | 紹介した求人の隠れた相性。`item` の値と同じ。 | `-0.12` |
| `failure_stage` | TEXT | 全段階を通過する見込みならNULL | 潜在的に到達が途切れる段階。 | `screening` |
| `failure_reason` | TEXT | 同上 | `not_applied`、`employer_rejection`、`candidate_withdrawal` のいずれか。 | `employer_rejection` |
| `application_screening_gap` | REAL | なし | 応募と通過の確率の差。 | `-0.0988` |
| `relative_application_screening_gap` | REAL | なし | 各基準率（既定では応募1%、通過10%）で割った値の差。決定のペナルティには後者を使う。 | `0.20` |
| `latent_oubo_dt` | TEXT | 永久に到達しなければNULL | 打切りを適用する前の応募日時。 | `2025-01-08T10:26:00+09:00` |
| `latent_shorui_tsuuka_dt` | TEXT | 同上 | 打切りを適用する前の書類通過日時。 | NULL |
| `latent_kettei_dt` | TEXT | 同上 | 打切りを適用する前の決定日時。 | NULL |

`p_candidate_<stage>` と `p_employer_<stage>` の積は `p_<stage>_eventual` になります。`failure_stage` と `failure_reason` は潜在的に到達が途切れた段階と理由で、理由は求職者側が応募しない `not_applied`、企業側の否認 `employer_rejection`、求職者側の辞退 `candidate_withdrawal` のいずれかです。たとえば `failure_stage` が `screening` で `failure_reason` が `employer_rejection` の行では、潜在日時は応募まで記録され、書類通過と決定はNULLです。

応募・通過・決定の真値の積は、同じペアの期限を設けない紹介から決定までの確率です。3暦月以内の確率を評価する際は、所要時間も考慮します。[紹介履歴の確率モデル](PROBABILITY_MODEL.md)

## `daily` 市場の日次集計

NULLがなく、`market_start` から観測終了まで1日1行が入ります。市場全体の推移の把握や図表の作成に使う補助テーブルです。

| 列 | 型 | NULLの意味 | 内容 | 値の例 |
| --- | --- | --- | --- | --- |
| `day_dt` | TEXT | なし（主キー） | 市場の開始日時から1日ずつ進む日時。 | `2025-03-10T00:00:00+09:00` |
| `live_users` | INTEGER | なし | 日末の在籍求職者数。 | `152` |
| `live_items` | INTEGER | なし | 日末の掲載中求人数。 | `308` |
| `referrals` | INTEGER | なし | 当日の紹介件数。 | `21` |
| `new_users` | INTEGER | なし | 当日の新規参加求職者数。 | `8` |
| `new_items` | INTEGER | なし | 当日の新規参加求人数。 | `8` |
| `applications` | INTEGER | なし | 当日に観測された応募件数。 | `1` |
| `screenings` | INTEGER | なし | 当日に観測された書類通過件数。 | `1` |
| `decisions` | INTEGER | なし | 当日に観測された決定件数。 | `0` |

## 観測値と潜在値の区別

| | 観測値 | 潜在値 |
| --- | --- | --- |
| 保存先 | `events` の日時列、`daily` の当日到達数 | `events_latent` 全体 |
| 受ける影響 | 市場の退出、掲載期限、相手の退出による選考取消、観測締切 | なし（反実仮の真値） |
| 未到達の表現 | NULL（CSVでは空欄） | 永久に到達しなければNULL、到達するなら日時を記録 |

観測された到達率は、`p_*_eventual` を紹介時点で計算した値よりも市場の打ち切りの分だけ下がり得ます。誰の退出で選考が消えたかといった実績の取消理由は保存せず、退出日時だけをマスターに記録します。

3暦月以内の結果を評価する場合は、紹介からの期限と観測締切を比較してラベルが成熟した行を選び、到達日時を期限と比較します。通過の分母は期限内の応募者、決定の分母は期限内の通過者です。

## ウォームアップ期間とスコア

スコアは外部の学習済みモデルではなく、市場自身の紹介履歴から学習します（[市場内ウォームアップ学習](MARKET.md)）。

- 市場開始から `warmup` 期間（market config の `warmup`、既定365日）の紹介履歴で、応募・書類通過・決定の3モデルを学習します。
- 学習済みモデルは以降の紹介に適用され、`model_apply_score`、`model_screen_score`、`model_decision_score` に入ります。
- warmup 期間中（`training_cutoff` より前）の紹介のスコア列はNULLです。warmup の `events` も出力に含まれるため、学習データを確認できます。
- `warmup` が `days` 以上のときは、すべての紹介のスコア列がNULLになります。
- manifest の `training_cutoff` と `warmup_end` で、学習データと本運用データを区別できます。

## manifest.json

| フィールド | 内容 |
| --- | --- |
| `generator`, `version` | 生成器の名前とバージョン。 |
| `mode` | 生成モード。市場のみのため常に `"market"`。 |
| `output_format` | `sqlite`、`parquet`、`csv` のいずれか。 |
| `config` | 市場の全設定。`seed`、`seeker_arrivals`、`job_arrivals`、`days`、`warmup`、`application_days`、`posting_days`、`referrals`。 |
| `market_start` | 市場の開始日時。 |
| `observation_end_exclusive` | 観測の終了境界。`market_start` に `days` を足した日時で、この日時以降の到達は記録しない。 |
| `training_cutoff` | 学習に使ったwarmup紹介の締切日時。この日時より前の紹介の `model_*_score` はNULL。 |
| `warmup_end` | warmup期間の終了日時。この日時以降の紹介にスコアが付く。 |
| `generation_seconds` | データ生成にかかった秒数（計測値）。 |
| `row_counts` | テーブルごとの件数。`user`、`item`、`events`、`events_latent`、`daily`。 |
| `probability_scope` | 潜在確率の意味づけの注記。 |
| `files` | 出力ファイルごとのSHA-256。 |

同じ条件を再現するときは、設定・seedと使用したコード・依存環境を揃えます。生成規則や乱数の割り当てが変わると、同じseedから生成される行も変わることがあります。
