# 出力データ仕様

出力は求職者マスター `user`、求人マスター `item`、参加者ごとの潜在値 `user_latent` / `item_latent`、紹介履歴 `events`、紹介ごとの潜在値 `events_latent`、日次集計 `daily` の計7テーブルです。データは[継続市場のシミュレーション](MARKET.md)だけで生成します。市場のルールと生成手順は MARKET.md を、確率の計算方法は[紹介履歴の確率モデル](PROBABILITY_MODEL.md)、このデータから日次バッチ推論候補を作るSQLレシピは[日次バッチ推論候補](DAILY_CANDIDATES.md)を参照してください。

## 出力形式とファイル名

| 指定 | `output/` に作られるデータ | 主な用途 |
| --- | --- | --- |
| `--format sqlite`（既定） | `dataset.sqlite.db` の `user`、`item`、`user_latent`、`item_latent`、`events`、`events_latent`、`daily` テーブル | 1つのDBとして扱い、SQLで検索・結合する。 |
| `--format parquet` | `user.parquet`、`item.parquet`、`user_latent.parquet`、`item_latent.parquet`、`events.parquet`、`events_latent.parquet`、`daily.parquet` | Pythonで列単位に読み込み、分析する。 |
| `--format csv` | `user.csv`、`item.csv`、`user_latent.csv`、`item_latent.csv`、`events.csv`、`events_latent.csv`、`daily.csv` | 数行を目視で確認したり、テキストとして受け渡したりする。 |

どの形式でも `manifest.json` が付きます。フィールド一覧はこの文書の後半にあります。

## テーブルの単位とキー

| テーブル | 1行の単位 | 主キー | 外部キー |
| --- | --- | --- | --- |
| `user` | 求職者の1参加 | `user_id` | なし |
| `item` | 求人の1掲載 | `item_id` | なし |
| `user_latent` | 求職者の1参加（`user` と1対1） | `user_id` | `user_id` → `user` |
| `item_latent` | 求人の1掲載（`item` と1対1） | `item_id` | `item_id` → `item` |
| `events` | 紹介1件 | `event_id` | `user_id` → `user`、`item_id` → `item` |
| `events_latent` | 紹介1件（`events` と1対1） | `event_id` | `event_id` → `events` |
| `daily` | 市場の1日 | `day_dt` | なし |

`user` と `item` は1参加=1行で、在籍中は属性が不変です。そのため `events` には属性列を持たず、必要な属性はマスター結合で取得します。紹介時点の属性の値はマスターの値と一致します。参加時に決まる隠れた意欲（モチベーション）も同様に不変で、`user_latent` と `item_latent` に1参加=1行で保存します。紹介ごとに同じ値を重複して持たせず、`events` から `user` / `item` を通じて結合します。

```sql
SELECT e.event_id, u.user_skill, i.required_skill,
       ul.user_motivation, il.item_motivation
FROM events AS e
JOIN user AS u USING (user_id)
JOIN item AS i USING (item_id)
JOIN user_latent AS ul USING (user_id)
JOIN item_latent AS il USING (item_id)
```

型の表記はSQLiteの宣言です。Parquetでは TEXT=string、INTEGER=int64、REAL=double、CSVでは空欄がNULLに相当します。日時はUTCオフセット付きISO 8601の文字列です。

## `user` 求職者マスター

| 列 | 型 | NULLの意味 | 内容 | 値の例 |
| --- | --- | --- | --- | --- |
| `user_id` | TEXT | なし（主キー） | 求職者のID。 | `user-000000042` |
| `arrival_dt` | TEXT | なし | 市場への参加日時。 | `2025-01-05T00:00:00+09:00` |
| `user_skill` | REAL | なし | スキル。0〜1。 | `0.62` |
| `desired_salary` | REAL | なし | 希望給与。正の実数。 | `860.0` |
| `occupation` | TEXT | なし | 職種。8種類の日本語の値のいずれか（下記）。 | `エンジニア` |
| `region` | TEXT | なし | 勤務地。8種類の日本語の値のいずれか（下記）。 | `東京` |
| `wants_remote` | INTEGER | なし | リモート希望。1=希望、0=不希望。 | `1` |
| `departure_dt` | TEXT | NULL=観測締切時点で在籍中 | 退出日時。退出理由は保存しない。 | NULL |

## `item` 求人マスター

| 列 | 型 | NULLの意味 | 内容 | 値の例 |
| --- | --- | --- | --- | --- |
| `item_id` | TEXT | なし（主キー） | 求人のID。 | `item-0007` |
| `arrival_dt` | TEXT | なし | 市場への参加（掲載開始）日時。 | `2025-01-06T00:00:00+09:00` |
| `required_skill` | REAL | なし | 要求スキル。0〜1。 | `0.55` |
| `offered_salary` | REAL | なし | 提示給与。正の実数。現行の `max(200, 300 + 800 × required_skill + noise)`。ノイズ導入・ロジック再設計は将来課題（issue #34）。 | `900.0` |
| `fee_rate` | REAL | なし | 配置手数料率（issue #34）。初期値は全求人で一律 0.33 だが、将来の率変更に備えて列として保持する。 | `0.33` |
| `occupation` | TEXT | なし | 職種。8種類の日本語の値のいずれか（下記）。 | `エンジニア` |
| `region` | TEXT | なし | 勤務地。8種類の日本語の値のいずれか（下記）。 | `東京` |
| `allows_remote` | INTEGER | なし | リモート可否。1=可、0=不可。 | `0` |
| `departure_dt` | TEXT | NULL=観測締切時点で掲載中 | 退出（掲載終了）日時。退出理由は保存しない。 | `2025-02-15T00:00:00+09:00` |

`occupation` と `region` は8種類の日本語の `TEXT` 値です。`occupation` は `エンジニア` / `営業` / `事務` / `デザイン` / `マーケティング` / `介護` / `販売` / `製造`、`region` は `東京` / `大阪` / `名古屋` / `福岡` / `札幌` / `仙台` / `広島` / `沖縄` です。紹介では職種が一致し、地域が一致するか求人が全国リモート可（`allows_remote = 1`）のペアを選びます。SQLiteでは `TEXT`、Parquetでは string、CSVでは日本語の文字列として保存されます。

## `user_latent` 求職者の潜在値

`user` と1対1で、参加時に決まった隠れた意欲（モチベーション）だけを保存します。マスターから分離したのは、観測される属性ではなく反実仮の潜在値だからです。値は参加時にガウス分布（平均0、標準偏差0.5）から1回だけ生成され、在籍中は変化しません。各段階で求職者側のロジットに同じ値が足されます。

| 列 | 型 | NULLの意味 | 内容 | 値の例 |
| --- | --- | --- | --- | --- |
| `user_id` | TEXT | なし（主キー） | 求職者のID。`user.user_id` を参照。 | `user-000000042` |
| `user_motivation` | REAL | なし | 隠れた意欲（潜在値）。観測データに現れない。 | `0.31` |

## `item_latent` 求人の潜在値

`item` と1対1で、参加時に決まった隠れた意欲（モチベーション）だけを保存します。値は参加時にガウス分布（平均0、標準偏差0.5）から1回だけ生成され、掲載中は変化しません。各段階で企業側のロジットに同じ値が足されます。

| 列 | 型 | NULLの意味 | 内容 | 値の例 |
| --- | --- | --- | --- | --- |
| `item_id` | TEXT | なし（主キー） | 求人のID。`item.item_id` を参照。 | `item-0007` |
| `item_motivation` | REAL | なし | 隠れた意欲（潜在値）。観測データに現れない。 | `-0.12` |

## `events` 紹介と観測された結果

1行が紹介1件です。`event_id` は一意で、`events_latent` と1対1に結合できます。同じ `user_id`、`item_id` は複数行に現れます。属性列はマスター結合で取得する設計のため、このテーブルには明確なロジックで計算できる派生特徴量と観測結果だけが入ります。スキル由来の `skill_match` / `skill_shortfall` / `overqualification` は真のスキルが数値で表せないため `events_latent` に移しています。SQLiteでは外部キー制約が有効で、以下の読み取り用インデックスが作成されます（issue #38）。運営用 DB と参加者用 DB の両方に含まれます。

| インデックス | 列 | 用途 |
| --- | --- | --- |
| `events_shoukai_dt` | `events(shoukai_dt)` | 紹介日の範囲検索。 |
| `idx_events_user` | `events(user_id, oubo_dt, kettei_dt, outcome_dt)` | 日次候補レシピの `other_applications` 相関サブクエリ。 |
| `idx_events_item` | `events(item_id, oubo_dt, kettei_dt, outcome_dt)` | 同 `competing_candidates` 相関サブクエリ。 |
| `idx_events_user_item` | `events(user_id, item_id)` | 同 `NOT EXISTS`（未紹介ペア）検索。 |
| `idx_user_occupation` | `user(occupation, arrival_dt, departure_dt)` | 候補レシピのユーザー側マスター検索。 |
| `idx_item_occupation` | `item(occupation, arrival_dt, departure_dt)` | 同求人側マスター検索。 |

`user_latent` / `item_latent` / `events_latent` のキー検索は各テーブルの主キー（`user_id` / `item_id` / `event_id`）に自動生成されたインデックスが使われます。

| 列 | 型 | NULLの意味 | 内容 | 値の例 |
| --- | --- | --- | --- | --- |
| `event_id` | TEXT | なし（主キー） | 紹介イベントのID。 | `market-000000042-0007` |
| `user_id` | TEXT | なし | 紹介された求職者。`user.user_id` を参照。 | `user-000000042` |
| `item_id` | TEXT | なし | 紹介された求人。`item.item_id` を参照。 | `item-0007` |
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
| `kettei_dt` | TEXT | 未到達ならNULL | 観測された決定（採用）日時。取消結果とは排他。 | NULL |
| `fee_yen` | REAL | `kettei_dt` がNULLならNULL | 決定時に確定した最終手数料（issue #34）。`item.fee_rate × item.offered_salary`。決定に至らなかった行は 0 ではなくNULL。 | `297.0` |
| `status` | TEXT | 終端結果が未観測ならNULL | 観測された終端結果。`not_applied` / `ng` / `withdrawn` / `cancelled_posting_expiry` / `cancelled_seeker_exit` / `cancelled_market_end` のいずれか。採用行はNULL。 | `ng` |
| `outcome_dt` | TEXT | `status` と同じ | 終端結果が観測された時刻（`market_start` からのオフセットを日時文字列化）。 | `2025-02-03T09:12:00+09:00` |
| `auto` | INTEGER | なし（常に0または1） | 1=採用成立による自動連鎖（自動NG／自動辞退）、0=それ以外。`status` が `ng` / `withdrawn` のときだけ意味を持ち、それ以外の行では常に0。 | `0` |

紹介から応募・通過・決定へ進む様子は市場の退出や掲載期限で途切れることがあり、未到達の段階はNULLです。締切までに届かなかった日時もNULLになります。`status` と `outcome_dt` は常に一緒にNULL（または一緒に非NULL）で、`status` が非NULLの行では `kettei_dt` は必ずNULLです（採用と取消は排他）。1行に終端結果は高々1つで、複数の理由が同時に記録されることはありません。`auto = 1` は `status` が `ng` または `withdrawn` の行だけに現れます。

`status` がNULLのことは「失敗しなかった」ではなく「観測された終端結果がない」ことを意味し、採用済み行（`kettei_dt` 非NULL）と進行中／観測打切り行（典型的には `kettei_dt` NULL）が混在します。採用かどうかは従来どおり `kettei_dt` で判定してください。

`ng` / `withdrawn` の発生段階は保存しません。利用者は `shorui_tsuuka_dt` の有無から導出できます。

- `status = 'ng'` かつ `shorui_tsuuka_dt IS NULL` → 書類選考ゲートのNG。
- `status = 'ng'` かつ `shorui_tsuuka_dt IS NOT NULL`（かつ `kettei_dt IS NULL`）→ 書類通過後の決定フェーズ（面接などを含む）のNG。
- `withdrawn` も同様に解釈します（`shorui_tsuuka_dt IS NULL` なら応募後・書類選考前の辞退）。
- `auto = 1` の行では自動連鎖が主情報で、段階の有無は副次的です。

`not_applied` は応募の抽選（紹介時点のゲート）に失敗して応募に至らなかった行に付きます。`cancelled_posting_expiry` は求人の掲載期限満了、`cancelled_seeker_exit` は求職者の応募可能期間満了、`cancelled_market_end` は相手が既に市場から退出していた場合の安全網です。観測締切時点でまだ終端していない行は打切りとして扱い、`status` / `outcome_dt` はNULLのままです。

`status` / `outcome_dt` / `auto` は紹介後に確定する情報でリークになるため、**学習特徴量として使用禁止**です。`kettei_dt` の採用ラベルと3暦月の成熟度判定は従来どおりです。

### 手数料列と日次候補（issue #34）

手数料は確定的な属性値で、潜在値や確率ではありません。`item.fee_rate` は全求人に一律 0.33 ですが列として保持し、`events.fee_yen` は決定（採用）した紹介にだけ `item.fee_rate × item.offered_salary` を記録します。未決定の行の `fee_yen` は NULL で、金額不明の行を暗黙の 0 として数えません。提示給与の生成ロジックは現行のルールベースのままで、ノイズ追加などの再設計は将来課題です。

日次候補 `candidates` / `candidates_latent` にも、観測列として `fee_rate` と `fee_if_decided_yen = offered_salary × fee_rate` が入ります（SQL レシピは[日次バッチ推論候補](DAILY_CANDIDATES.md)を参照）。料率は全参加者共通の既知値なので潜在情報ではなく、参加者はこの列から期待手数料を計算できます。

## `events_latent` 潜在値（反実仮の真値）

`<stage>` は `application`、`screening`、`decision` のいずれかです。このテーブルの値はすべて紹介時点で確定する潜在値で、退出・掲載期限・相手の退出による選考取消・観測打切りを無視し、紹介後も仲介が継続した場合の真値（反実仮）です。実績の選考取消理由は保存しません。校正やモデル評価の真値との比較、生成器の診断に使います。学習や推論の特徴量のうち、給与不足率・リモート不一致・他社応募件数・競合候補者数は明確に計算できるため `events` の紹介時点の列とマスター結合した属性から選びます。`skill_match` / `skill_shortfall` / `overqualification` は真のスキルが数値で表せないため、このテーブルに潜在値として保存します。`user.user_skill` / `item.required_skill` は便宜上の代理値としてマスターに残します。参加者の隠れた意欲は属性と同じく不変なので、このテーブルには重複して保存しません。`events` から `user` / `item` を経由して `user_latent.user_motivation` / `item_latent.item_motivation` を結合してください。片側確率 `p_candidate_*` / `p_employer_*` と `p_*_eventual` は、紹介時点の特徴量・職種（`occupation`）・地域（`region`）・紹介日から mechanism が計算した値です。issue #46 より、職種・地域のセグメント効果（決定段階）と時間ドリフト（全段階）が加わっています（`segment_drift` 設定）。列名は変わりませんが、値は旧バージョンと異なります。

| 列 | 型 | NULLの意味 | 内容 | 値の例 |
| --- | --- | --- | --- | --- |
| `event_id` | TEXT | なし（主キー） | `events` と共通の紹介イベントID。 | `market-000000042-0007` |
| `skill_match` | REAL | なし | スキル適合度。`1 - abs(user_skill - required_skill)`。真のスキルは数値で表せず、マスターの数値スキルは便宜上の代理値なので潜在値として扱う。 | `0.93` |
| `skill_shortfall` | REAL | なし | スキル不足。`max(required_skill - user_skill, 0)`。代理スキルから計算した潜在値（理由は `skill_match` と同じ）。 | `0.0` |
| `overqualification` | REAL | なし | スキル過剰。`max(user_skill - required_skill, 0)`。代理スキルから計算した潜在値（理由は `skill_match` と同じ）。 | `0.07` |
| `p_<stage>_eventual` | REAL | なし | 前段の到達を条件とした、期限を設けない真の移行確率。 | `0.112` |
| `p_candidate_<stage>` | REAL | なし | 各段階の求職者側の確率。 | `0.310` |
| `p_employer_<stage>` | REAL | なし | 各段階の企業側の確率。応募は求職者側だけで決まるため、`p_employer_application` は1。 | `0.361` |
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
| 保存先 | `events` の日時列と `status` / `outcome_dt` / `auto`、`daily` の当日到達数 | `events_latent` 全体、`user_latent`、`item_latent` |
| 受ける影響 | 市場の退出、掲載期限、相手の退出による選考取消、観測締切 | なし（反実仮の真値） |
| 未到達の表現 | NULL（CSVでは空欄）。終端結果なしもNULLだが、`status` / `outcome_dt` で区別 | 永久に到達しなければNULL、到達するなら日時を記録 |

観測された到達率は、`p_*_eventual` を紹介時点で計算した値よりも市場の打ち切りの分だけ下がり得ます。実績の取消理由は `events.status` / `outcome_dt` / `auto` に記録し、参加者の退出日時はマスターに記録します（退出理由そのものはマスターには保存しません）。観測打切りは取消ではなく、`status` / `outcome_dt` をNULLにして右打切りとして扱います。

語彙が似ているため、`events` の観測結果と `events_latent.failure_reason` の反実仮との対応を明示します。

| 観測（`events.status`） | 反実仮 `failure_reason` | 違い |
| --- | --- | --- |
| `ng`（`auto=0`） | `employer_rejection` | 観測は実際にその時点で中止。反実仮は「市場が媒介を続けた場合」の funnel 結果。 |
| `withdrawn`（`auto=0`） | `candidate_withdrawal`（stage 0 は `not_applied`） | 同上。 |
| `not_applied` | `not_applied`（stage 0） | 同上。 |
| `ng` / `withdrawn` の `auto=1` | 対応なし | カスケードは市場固有のイベントで反実仮には存在しない。 |
| `cancelled_posting_expiry` / `cancelled_seeker_exit` / `cancelled_market_end` | 対応なし | 退出・掲載期限は反実仮では無視される。 |
| NULL（採用） | NULL（全段階通過） | 採用は `kettei_dt` で表現し、両テーブルとも理由はNULL。 |

3暦月以内の結果を評価する場合は、紹介からの期限と観測締切を比較してラベルが成熟した行を選び、到達日時を期限と比較します。通過の分母は期限内の応募者、決定の分母は期限内の通過者です。

## ウォームアップ期間とスコア

スコアは外部の学習済みモデルではなく、市場自身の紹介履歴から学習します（[市場内ウォームアップ学習](MARKET.md)）。

- 市場開始から `warmup` 期間（market config の `warmup`、既定365日）の紹介履歴で、応募・書類通過・決定の3モデルを学習します。
- 学習済みモデルは以降の紹介に適用され、`model_apply_score`、`model_screen_score`、`model_decision_score` に入ります。
- warmup 期間中（`warmup_end` より前、すなわち `day < warmup`）の紹介のスコア列はNULLです。warmup の `events` も出力に含まれるため、学習データを確認できます。
- `warmup` が `days` 以上のときは、すべての紹介のスコア列がNULLになります。
- manifest の `training_cutoff` と `warmup_end` で、学習データと本運用データを区別できます。

## manifest.json

| フィールド | 内容 |
| --- | --- |
| `generator`, `version` | 生成器の名前とバージョン。 |
| `mode` | 生成モード。市場のみのため常に `"market"`。 |
| `output_format` | `sqlite`、`parquet`、`csv` のいずれか。 |
| `config` | 市場の全設定。`seed`、`seeker_arrivals`、`job_arrivals`、`days`、`warmup`、`application_days`、`posting_days`、`referrals`、`training_window_days`、`training_referrals_per_day`、`negative_reservoir_size`。 |
| `market_start` | 市場の開始日時。 |
| `observation_end_exclusive` | 観測の終了境界。`market_start` に `days` を足した日時で、この日時以降の到達は記録しない。 |
| `training` | warmup学習の設定と結果（`method`、`status`、各ステージの学習窓・件数など）。モデルを学習できなかった場合は `status` が `not_trained` になる。 |
| `training_cutoff` | 学習に使った最後のwarmup紹介の締切日時（`warmup_end` の約1日前）。スコアが付くのは `warmup_end` 以降の紹介（`day >= warmup`）で、`warmup_end` より前の紹介の `model_*_score` はNULL。学習できなかった場合は null。 |
| `warmup_end` | warmup期間の終了日時。この日時以降の紹介（`day >= warmup`）にスコアが付く。 |
| `row_counts` | テーブルごとの件数。`user`、`user_latent`、`item`、`item_latent`、`events`、`events_latent`、`daily`。 |
| `probability_scope` | 潜在確率の意味づけの注記。 |
| `truth_scope` | 反実仮の真値（`events_latent`）の意味づけの注記。 |
| `files` | 出力ファイルごとのSHA-256。 |

manifest は生成時間やタイムスタンプのような実行時計測値を**含みません**。したがって同一の config・seed・コード・依存環境なら manifest.json はバイト単位で同一になり、その SHA-256 を data version として使えます（issue #38）。計測値を入れると再生成のたびにハッシュが変わり data version が再現しなくなるためです。

同じ条件を再現するときは、設定・seedと使用したコード・依存環境を揃えます。生成規則や乱数の割り当てが変わると、同じseedから生成される行も変わることがあります。
