# 日次バッチ推論候補（SQLレシピ）

継続市場は学習データとして使います。バッチ推論日 D の候補は `user` × `item` のクロス結合から、紹介条件を満たし、まだ紹介されていないペアだけを取り出します。レシピはSQLite想定で、`:day` に D の日時（UTCオフセット付きISO 8601、`market_start` と同じオフセット。例 `2025-01-16T00:00:00+09:00`）をバインドします。日時は文字列比較になるため、書式とオフセットを出力と揃えてください。

## 候補プールの条件

- 双方が D 時点で在籍中（`arrival_dt <= D < departure_dt`）
- `occupation` が一致
- `region` が一致するか、求人が `allows_remote = 1`
- 給与フロア `max(100, offered_salary - 100) <= desired_salary`（シミュレータの紹介プールと同一）
- 同じ `(user_id, item_id)` が `events` にない（未紹介）

## 2種類の出力

| 出力 | 用途 | 列 |
| --- | --- | --- |
| `candidates` | 実験・コンペ参加者向け（観測のみ） | `day_dt`、`user_id`、`item_id`、マスター属性（`seeker_skill` = `user.user_skill`、`required_skill`、`desired_salary`、`offered_salary`、`wants_remote`、`allows_remote`）、`salary_shortfall`、`remote_mismatch`、`other_applications`、`competing_candidates` |
| `candidates_latent` | 運営側のモデル評価向け | `candidates` の全列 + `skill_match` / `skill_shortfall` / `overqualification`、`user_motivation` / `item_motivation`、反実仮 `p_*_eventual` / `p_candidate_*` / `p_employer_*` |

`candidates` には潜在由来のスキル特徴量・モチベーション・確率を含めません。スコアモデルの入力にはマスター属性（`FEATURE_FIELDS` の `seeker_skill` など）が必要なため、これらは含めます。マスターの数値スキルは便宜上の代理値です。`candidates_latent` のスキル由来3列も同じ代理値から計算した値で、真のスキルそのものではありません。キーは `(day_dt, user_id, item_id)` で、未紹介ペアなので `event_id` はありません。

### スナップショット状態値の定義

`other_applications` は求職者から見て「D までに応募し、D 時点で終端していない案件数」、`competing_candidates` は求人から見て同条件の求職者数です。両者とも `events` から `oubo_dt <= D`、`kettei_dt` が D 以内にない、`outcome_dt` が D 以内にない行を数えます。取消の観測時刻 `outcome_dt` が追加された（issue `#32`）ため、書類選考落ち（NG）や辞退・掲載期限切れで消えた案件も D 時点で正しく除外され、シミュレータ内部の保留中件数と**正確に一致**します（従来の上限近似ではなくなりました）。`status` / `auto` は終端の有無を判定するには不要で、学習特徴量としても使用禁止です。

## 日次クエリ（`candidates`）

```sql
SELECT
    :day AS day_dt,
    u.user_id AS user_id,
    i.item_id AS item_id,
    u.user_skill AS seeker_skill,
    i.required_skill AS required_skill,
    u.desired_salary AS desired_salary,
    i.offered_salary AS offered_salary,
    u.wants_remote AS wants_remote,
    i.allows_remote AS allows_remote,
    MAX((u.desired_salary - i.offered_salary) / u.desired_salary, 0) AS salary_shortfall,
    u.wants_remote * (1 - i.allows_remote) AS remote_mismatch,
    (SELECT COUNT(*) FROM events AS o
      WHERE o.user_id = u.user_id
        AND o.oubo_dt IS NOT NULL AND o.oubo_dt <= :day
        AND (o.kettei_dt IS NULL OR o.kettei_dt > :day)
        AND (o.outcome_dt IS NULL OR o.outcome_dt > :day)) AS other_applications,
    (SELECT COUNT(*) FROM events AS c
      WHERE c.item_id = i.item_id
        AND c.oubo_dt IS NOT NULL AND c.oubo_dt <= :day
        AND (c.kettei_dt IS NULL OR c.kettei_dt > :day)
        AND (c.outcome_dt IS NULL OR c.outcome_dt > :day)) AS competing_candidates
FROM user AS u
JOIN item AS i ON i.occupation = u.occupation
WHERE u.arrival_dt <= :day AND (u.departure_dt IS NULL OR u.departure_dt > :day)
  AND i.arrival_dt <= :day AND (i.departure_dt IS NULL OR i.departure_dt > :day)
  AND (i.region = u.region OR i.allows_remote = 1)
  AND MAX(100, i.offered_salary - 100) <= u.desired_salary
  AND NOT EXISTS (SELECT 1 FROM events AS e
                  WHERE e.user_id = u.user_id AND e.item_id = i.item_id)
```

Python での実行例（市場開始 `2024-01-01T00:00:00+09:00`、10日目は `2024-01-11T00:00:00+09:00`）:

```python
import sqlite3
from contextlib import closing
from synthetic_funnel.market_candidates import candidate_rows

with closing(sqlite3.connect("runs/.../output/dataset.sqlite.db")) as connection:
    rows = candidate_rows(connection, "2024-01-11T00:00:00+09:00")
print(len(rows), rows[0])
```

`candidate_rows` は上の SQL に `:day` をバインドして辞書のリストを返します。`synthetic_funnel.market_candidates.DAILY_CANDIDATES_SQL` が SQL の唯一の定義で、このページの SQL と同一です（テストで整合を確認します）。設定例（`seeker_arrivals=4`、`job_arrivals=30`、`referrals=1`、`seed=11`、30日）の10日目では149件です。先頭行の値は次のとおりです。

| day_dt | user_id | item_id | seeker_skill | required_skill | desired_salary | offered_salary | wants_remote | allows_remote | salary_shortfall | remote_mismatch | other_applications | competing_candidates |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `2024-01-11T00:00:00+09:00` | `user-000000001` | `item-0240` | `0.5874` | `0.8426` | `817.7091` | `680.1946` | `1` | `1` | `0.1682` | `0` | `0` | `0` |

## `candidates_latent` の2段構成

### 段1: SQL でペアと観測特徴量を抽出

```sql
SELECT
    :day AS day_dt,
    u.user_id AS user_id,
    i.item_id AS item_id,
    u.user_skill AS seeker_skill,
    i.required_skill AS required_skill,
    u.desired_salary AS desired_salary,
    i.offered_salary AS offered_salary,
    u.wants_remote AS wants_remote,
    i.allows_remote AS allows_remote,
    MAX((u.desired_salary - i.offered_salary) / u.desired_salary, 0) AS salary_shortfall,
    u.wants_remote * (1 - i.allows_remote) AS remote_mismatch,
    (SELECT COUNT(*) FROM events AS o
      WHERE o.user_id = u.user_id
        AND o.oubo_dt IS NOT NULL AND o.oubo_dt <= :day
        AND (o.kettei_dt IS NULL OR o.kettei_dt > :day)
        AND (o.outcome_dt IS NULL OR o.outcome_dt > :day)) AS other_applications,
    (SELECT COUNT(*) FROM events AS c
      WHERE c.item_id = i.item_id
        AND c.oubo_dt IS NOT NULL AND c.oubo_dt <= :day
        AND (c.kettei_dt IS NULL OR c.kettei_dt > :day)
        AND (c.outcome_dt IS NULL OR c.outcome_dt > :day)) AS competing_candidates,
    1 - ABS(u.user_skill - i.required_skill) AS skill_match,
    MAX(i.required_skill - u.user_skill, 0) AS skill_shortfall,
    MAX(u.user_skill - i.required_skill, 0) AS overqualification,
    ul.user_motivation AS user_motivation,
    il.item_motivation AS item_motivation
FROM user AS u
JOIN item AS i ON i.occupation = u.occupation
JOIN user_latent AS ul USING (user_id)
JOIN item_latent AS il USING (item_id)
WHERE u.arrival_dt <= :day AND (u.departure_dt IS NULL OR u.departure_dt > :day)
  AND i.arrival_dt <= :day AND (i.departure_dt IS NULL OR i.departure_dt > :day)
  AND (i.region = u.region OR i.allows_remote = 1)
  AND MAX(100, i.offered_salary - 100) <= u.desired_salary
  AND NOT EXISTS (SELECT 1 FROM events AS e
                  WHERE e.user_id = u.user_id AND e.item_id = i.item_id)
```

### 段2: mechanism で反実仮確率を付与

`p_*` は SQL だけでは計算できません。`attach_latent_truth` が `FEATURE_FIELDS` を組み立て、`manifest.config.seed` と同じ seed の `ProbabilityConfig` からアンカー切片を求め、`mechanism.probability_arrays` を呼び出して `p_candidate_*` / `p_employer_*` / `p_*_eventual` を付けます。スナップショットの同時進行件数は `outcome_dt` により正確に再構成できますが、まだ紹介されていないペアに対する `p_*` はその時点の特徴量から推定した値です。

```python
from synthetic_funnel.market_candidates import attach_latent_truth, candidate_rows

latent = candidate_rows(connection, day, latent=True)
truth = attach_latent_truth(latent, seed=manifest["config"]["seed"])
# truth[0] には p_candidate_* / p_employer_* / p_*_eventual が入る
```

例: `skill_match=0.7448`、`skill_shortfall=0.2552`、`overqualification=0`、`user_motivation=0.5491`、`item_motivation=0.3732` のペアで `p_application_eventual=0.0133`、`p_screening_eventual=0.1260`、`p_decision_eventual=0.0995` です。`p_candidate_* × p_employer_* = p_*_eventual` が成り立ちます。

## パラメータ無しビューのパターン

`:day` をバインドできないツール向けに、1行の `batch_day` テーブルから日付を読むビューも使えます。日を切り替えるときは `batch_day` を更新します。ビュー本体は上記の `candidates` クエリの `:day` を `(SELECT day_dt FROM batch_day)` に置き換えたものです。

```sql
CREATE TABLE IF NOT EXISTS batch_day (day_dt TEXT)
```

```sql
CREATE VIEW IF NOT EXISTS daily_candidates AS
SELECT
    (SELECT day_dt FROM batch_day) AS day_dt,
    u.user_id AS user_id,
    i.item_id AS item_id,
    u.user_skill AS seeker_skill,
    i.required_skill AS required_skill,
    u.desired_salary AS desired_salary,
    i.offered_salary AS offered_salary,
    u.wants_remote AS wants_remote,
    i.allows_remote AS allows_remote,
    MAX((u.desired_salary - i.offered_salary) / u.desired_salary, 0) AS salary_shortfall,
    u.wants_remote * (1 - i.allows_remote) AS remote_mismatch,
    (SELECT COUNT(*) FROM events AS o
      WHERE o.user_id = u.user_id
        AND o.oubo_dt IS NOT NULL AND o.oubo_dt <= (SELECT day_dt FROM batch_day)
        AND (o.kettei_dt IS NULL OR o.kettei_dt > (SELECT day_dt FROM batch_day))
        AND (o.outcome_dt IS NULL OR o.outcome_dt > (SELECT day_dt FROM batch_day))) AS other_applications,
    (SELECT COUNT(*) FROM events AS c
      WHERE c.item_id = i.item_id
        AND c.oubo_dt IS NOT NULL AND c.oubo_dt <= (SELECT day_dt FROM batch_day)
        AND (c.kettei_dt IS NULL OR c.kettei_dt > (SELECT day_dt FROM batch_day))
        AND (c.outcome_dt IS NULL OR c.outcome_dt > (SELECT day_dt FROM batch_day))) AS competing_candidates
FROM user AS u
JOIN item AS i ON i.occupation = u.occupation
WHERE u.arrival_dt <= (SELECT day_dt FROM batch_day) AND (u.departure_dt IS NULL OR u.departure_dt > (SELECT day_dt FROM batch_day))
  AND i.arrival_dt <= (SELECT day_dt FROM batch_day) AND (i.departure_dt IS NULL OR i.departure_dt > (SELECT day_dt FROM batch_day))
  AND (i.region = u.region OR i.allows_remote = 1)
  AND MAX(100, i.offered_salary - 100) <= u.desired_salary
  AND NOT EXISTS (SELECT 1 FROM events AS e
                  WHERE e.user_id = u.user_id AND e.item_id = i.item_id)
```

```python
connection.execute("DELETE FROM batch_day")
connection.execute("INSERT INTO batch_day (day_dt) VALUES (?)", (day,))
rows = connection.execute("SELECT user_id, item_id FROM daily_candidates").fetchall()
```

`market_candidates.BATCH_DAY_TABLE_SQL` と `DAILY_CANDIDATES_VIEW_SQL` に同じ定義があります。

## 検証

`test/test_daily_candidates.py` が、レシピの結果がプール条件の独立再構成と一致すること、D 日の実際の紹介が（D より前の `events` だけを見た）プールに含まれること、スナップショット件数と `p_*` の整合を確認します。

[出力データ仕様](DATA_FORMAT.md) / [継続市場のシミュレーション](MARKET.md)
