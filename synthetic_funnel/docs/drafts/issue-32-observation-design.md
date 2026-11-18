# issue #32 設計草案: NG・辞退・取消を観測列として記録する

> ステータス: **草案（ユーザー確認待ち）**。本ドキュメントは設計のみを扱い、本番コード（`src/`）の変更は行わない。
> 対象: ando-ar/calibrate_prob issue #32。並行作業中の #31（7テーブル構成、`events_latent` への派生特徴移動、日次候補 SQL レシピ）および #27（fixture 再生成）と衝突しない前提で記述する。
> 未決定事項は末尾 §6 に集約した。回答後に確定版へ更新する。

## 0. 出発点: 現行の挙動と設計上の前提

### 0.1 観測列は陽性遷移のみ

`events` の日時列は `shoukai_dt`（紹介）/`oubo_dt`（応募）/`shorui_tsuuka_dt`（書類通過）/`kettei_dt`（決定＝採用）の4列で、いずれも陽性遷移の到達時刻のみを記録する。取消されたケースは `finish()` で `cases` から削除されるだけで、`events` には何も書かれない。そのため「取消済み」と「進行中（観測打切り時点で未到達）」は同じ NULL パターンになり区別できない。

### 0.2 `finish()` の呼び出し箇所（全網羅）

`src/synthetic_funnel/market.py` の `finish()` は以下の箇所から呼ばれる。#32 ではここに「理由」と「時刻」を渡すだけで、カスケード構造自体は変更しない。

| # | 箇所（`market.py`） | トリガー | 現在の理由の概念 |
| --- | --- | --- | --- |
| 1 | `process()` 冒頭 `sid not in live_s or jid not in live_j` | 相手が既に市場から退出していた（安全網） | 相手の退出由来 |
| 2 | `process()` stage 0 `live_s[sid]['until']<=t` | 求職者の応募可能期間満了（応募前に退出） | 求職者の自然退出 |
| 3 | `process()` stage 1 ゲート失敗 | 書類選考落ち（両側ゲートの不成立） | 企業側NG / 求職者側辞退 |
| 4 | `process()` stage 2 ゲート失敗 | 決定フェーズ失敗（面接などを含む） | 企業側NG / 求職者側辞退 |
| 5 | `process()` 決定成功時の `for other in s['pending']|j['pending']` | 採用成立によるカスケード | **自動辞退**（求職者の他案件）／**自動NG**（求人の他候補） |
| 6 | 日次ループの掲載期限処理 | 求人の `posting_days` 満了 | 掲載期限切れ |
| 7 | 日次ループの求職者期限処理（stage 0 のみ） | 求職者の `application_days` 満了 | 求職者の自然退出 |

**重要な実装注意点**: 5 のカスケード集合 `s['pending'] | j['pending']` には**採用が成立したケース自身（`eid`）も含まれる**。`finish()` に無条件で理由を記録すると、採用済み（`kettei_dt` 設定済み）の行に負の終端結果が付いてしまう。実装ではカスケード時に `other == eid` をスキップするか、`finish()` 側で `kettei_dt` が finite なら負の結果を書かないガードを入れること。

### 0.3 1ケース＝高々1つの終端結果（互斥）

ケースは `finish()` されると復活せず、採用（`kettei_dt`）と取消は排他的である。したがって終端結果は「0個または1個」であり、複数の日時列を同時に持つ必要はない。この性質が §1 の列設計を単純化する。

### 0.4 `events_latent` は別概念

`events_latent.failure_stage` / `failure_reason`（`not_applied` / `candidate_withdrawal` / `employer_rejection`）は、紹介時点に専用乱数ストリームで計算される**反実仮**の funnel 結果で、市場の退出・掲載期限・カスケードを無視する。#32 が新設するのは**実績（observed）の終端結果**であり、`events_latent` には一切書き込まない（§2.4 / §3.5）。

---

## 1. 列設計

### 1.1 選択肢の比較

| 案 | 構成 | 利点 | 難点 |
| --- | --- | --- | --- |
| (a) 理由別の日時列 | `ng_dt` / `withdrawn_dt` / `cancelled_dt` | 3つのバケットが `COUNT(ng_dt)` 等で即座に数えられる。issue の例に最も近い。 | 互斥なのに複数列が空になりがち。さらに「screening/decision の別」「auto カスケードの別」を表現するには結局 `reason` 列が必要で、結局 4 列以上になる。 |
| **(b) status + 単一日時列（推奨）** | `status` TEXT enum + `outcome_dt` TEXT | 互斥性と 1:1 に対応。2列で完結し、`events_latent` の `failure_stage` + `failure_reason` と同じパターン。`GROUP BY status` で全理由の分布が取れ、粗い分類も SQL の写像（§1.3）で導出できる。 | `ng` 系の合計は `status LIKE 'ng%'` 等の写像が必要（ビューを用意すれば利用者は意識しない）。 |
| (c) status + 理由別日時列 | `status` + `ng_dt` / `withdrawn_dt` / `cancelled_dt` | 粗分類と詳細理由の両方が列で見える。 | 同じ時刻が `status` と日時列に重複し、不整合（例: `status='ng_auto'` なのに `withdrawn_dt` が非NULL）が発生しうる。互斥性と矛盾する。 |

### 1.2 推奨: (b) `status` + `outcome_dt`

`events` に次の2列を追加する（7テーブル構成・マスター分離の方針は不変）。

| 列 | 型 | NULL の意味 | 内容 |
| --- | --- | --- | --- |
| `status` | TEXT | 終端結果が未観測（進行中／採用／観測打切りのいずれか） | 観測された終端結果の理由。§2.2 の enum。採用行では NULL のまま。 |
| `outcome_dt` | TEXT | `status` と同じ（終端結果なし） | `finish()` が呼ばれた時刻を `market_start` からのオフセットで持つ ISO 8601 文字列。 |

不変条件（テストで検証すべき項目）:

1. `status IS NULL ⟺ outcome_dt IS NULL`。
2. `status IS NOT NULL` の行では `kettei_dt IS NULL`（採用と取消は排他）。
3. `outcome_dt` は到達済みの最後の段階日時以上、かつ `observation_end_exclusive` 未満。
4. 1行に `status` は高々1値。複数理由の同時発生はない。

`NULL` の意味を利用者に明示する。`status IS NULL` は「失敗しなかった」ではなく「**観測された終端結果がない**」であり、採用済み（`kettei_dt` 非NULL）と進行中／観測打切り（典型的には `kettei_dt` NULL）が混在する。採用かどうかは従来どおり `kettei_dt` で判定する。

### 1.3 粗い分類の導出（ビュー例）

```sql
CREATE VIEW event_outcomes AS
SELECT e.*,
       CASE
         WHEN e.status IS NULL THEN NULL
         WHEN e.status LIKE 'ng%' THEN 'ng'
         WHEN e.status LIKE 'withdrawn%' THEN 'withdrawn'
         WHEN e.status LIKE 'cancelled%' OR e.status = 'not_applied' THEN 'cancelled'
       END AS outcome_group
FROM events AS e;
```

`not_applied` は応募前の中断なので `cancelled` 側に含める（§2.2）。保存するのは詳細な `status` のみで、粗分類は派生値として扱う。

### 1.4 `daily` テーブルについて

`daily` の列（`applications` / `screenings` / `decisions`）は現行のまま変更しない。取消件数の日次集計は `events.outcome_dt` から SQL で取得でき、#32 の完了条件に `daily` の拡張は含まれない。必要なら別 issue で `ngs` / `withdrawals` / `cancellations` を追加する（任意のフォローアップ）。

---

## 2. 理由分類体系

### 2.1 OBSERVED / 未観測の判断方針

「実プラットフォームの ATS に記録され、日次スナップショットの正確化に寄与するか」を基準にする。

| 理由 | 判定 | 根拠 |
| --- | --- | --- |
| 書類選考落ち | **観測** | issue の必須要件。 |
| 決定までのフェーズ（面接など）での落ち | **観測** | issue の必須要件。既存 decision ゲートに包含（§3.2）。 |
| 応募後・書類選考前の辞退 | **観測** | issue の必須要件。既存 screening ゲートの求職者側失敗に相当。 |
| 自動辞退（他案件で採用） | **観測** | 必須要件。カスケードに理由を付ける。 |
| 自動NG（求人が他候補を採用） | **観測** | 必須要件。同上。 |
| 掲載期限切れ | **観測** | 「取消理由の分類体系」で明示対象。スナップショットの上限近似の主因でもある。 |
| 求職者の自然退出（応募可能期間満了） | **観測** | 同上。主に stage 0 の未応募ケースで発生。 |
| 相手の退出由来（安全網の #1） | **観測** | 上記いずれかの原因で既に退出した相手の残余ケース。Generic 値に逃がす（§2.3）。 |
| 応募前のゲート不成立（`not_applied`） | **観測** | スナップショットで「応募待ち」が消えたことを正確に表すために必要。 |
| **観測打切り（observation-end censoring）** | **未観測（NULL）** | 取消ではなく打切り。負例にせず右打切りとして扱う（§3.4）。 |

### 2.2 推奨 `status` enum（網羅的・互斥）

| `status` 値 | 粗分類 | トリガー | 対応する `finish()` 呼び出し（§0.2） |
| --- | --- | --- | --- |
| `not_applied` | cancelled | stage 0 のゲート失敗（応募に至らなかった） | #2 の他に stage 0 の抽選失敗 |
| `ng_screening` | ng | stage 1 の**企業側**ゲート不成立（書類選考落ち） | #3 |
| `ng_decision` | ng | stage 2 の**企業側**ゲート不成立（面接以降の不採用を含む） | #4 |
| `ng_auto` | ng | 同じ求人が別候補を採用 → 自動NG | #5（求人の `pending`） |
| `withdrawn_screening` | withdrawn | stage 1 の**求職者側**ゲート不成立（応募後・書類選考前の辞退） | #3 |
| `withdrawn_decision` | withdrawn | stage 2 の**求職者側**ゲート不成立（決定前の辞退） | #4 |
| `withdrawn_auto` | withdrawn | 同じ求職者が別案件で採用 → 自動辞退 | #5（求職者の `pending`） |
| `cancelled_posting_expiry` | cancelled | 求人の掲載期限満了 | #6 |
| `cancelled_seeker_exit` | cancelled | 求職者の応募可能期間満了／自然退出（主に stage 0） | #2, #7 |
| `cancelled_market_exit` | cancelled | 相手が既に退出していた場合の安全網 | #1 |

補足:

- `ng_screening` と `ng_decision` を分けることで、「NG は書類選考落ちに限らない」という要件を、新ステージを追加せずに表現できる（§3.2）。
- `withdrawn_screening` / `withdrawn_decision` を分けることで、「応募後・書類選考前の辞退」も明示的に表現できる。
- `cancelled_market_exit` は通常発火しないはずの安全網であり、ログ／診断用に残す。#1 の呼び出しで求職者側か求人側かを判別できる場合は、それぞれ `cancelled_seeker_exit` / `cancelled_posting_expiry` へ寄せてよい。
- 観測打切りは enum に含めない（§2.1）。

### 2.3 自動カスケードのクラス別マッピング

決定成功時に `s = live_s.pop(sid)` / `j = live_j.pop(jid)` とした後、現在は `for other in s['pending'] | j['pending']` と統合している。実装ではこれを次のように分離し、範囲演算子 `|` を外す。

```python
for other in s['pending']:
    finish(other, t, 'withdrawn_auto')   # 求職者が他案件で採用された
for other in j['pending']:
    finish(other, t, 'ng_auto')          # 求人が他候補を採用した
```

いずれも `other == eid`（採用成立ケース自身）はスキップする（§0.2 の注意点）。一求人一採用の簡略化前提は変更しない。

### 2.4 反実仮 `events_latent.failure_reason` との対応（別概念）

語彙が似ているため混同を避けるための対応表を docs に載せる。

| 観測 `status`（`events`） | 反実仮 `failure_reason`（`events_latent`） | 違い |
| --- | --- | --- |
| `ng_screening` / `ng_decision` | `employer_rejection` | 観測は実際にその時点で中止；反実仮は「市場が媒介を続けた場合」の funnel 結果。 |
| `withdrawn_screening` / `withdrawn_decision` | `candidate_withdrawal`（stage 0 は `not_applied`） | 同上。 |
| `ng_auto` / `withdrawn_auto` | **対応なし（`failure_stage` が NULL でもあり得る）** | カスケードは市場固有のイベントなので反実仮には存在しない。重要な相違点。 |
| `cancelled_posting_expiry` / `cancelled_seeker_exit` / `cancelled_market_exit` | **対応なし** | 退出・掲載期限は反実仮では無視される。 |
| NULL（採用） | NULL（全段階通過） | 採用は `kettei_dt` で表現し、両テーブルとも理由は NULL。 |

---

## 3. 確率意味づけとの相互作用

### 3.1 `kettei_dt` は「採用のみ」のまま（推奨）

`pd` は紹介時点で計算された「決定ゲートに成功する確率」であり、`kettei_dt` が記録されるのは両側が合意して採用が成立したときだけである。#32 で NG を観測しても、この意味は変えない。NG は `kettei_dt` の代わりに `status = ng_*` として記録する。利点は次。

- 既存の校正 SQL / warmup 学習 / GUI の正例ラベル（`kettei_dt` 非NULL）がそのまま動く。
- 「決定に至る確率」という `pd` の目標定義（通過者内 7%）と、観測される採用率の対応が壊れない。
- `kettei_dt` と `status` の排他性が不変条件として明示できる（§1.2）。

### 3.2 NG は既存3ステージ内のゲート失敗として記録（推奨）

issue の「決定までのフェーズ（面接など）での落ちも含む」は、ステージを増やさずに次のように解釈する。

- stage 1（書類選考）の企業側失敗 → `ng_screening`
- stage 2（決定）の企業側失敗 → `ng_decision`。ここに「書類通過後の面接など、採用に至るまでの全フェーズ」を包含する。

新たな面接ステージを追加すると、`ProbabilityConfig` / `mechanism` / `events_latent` / warmup 学習 / 校正対象がすべて変わる大変更になり、#31・#27 とも衝突する。#32 では行わない（§6 Q2 で確認）。

### 3.3 両側ゲートと乱数序列（再現性を保つ方式）

確率モデルでは stage 1/2 は求職者側・企業側の独立なゲートの積 `p = p_candidate * p_employer` で進む。観測 NG / 辞退を区別するには、ゲート失敗がどちら側によるものかを知る必要がある。そこで、既存の1回の乱数 `u = rng.random()` と行内の `p_candidate_*` / `p_employer_*` から、乱数を追加消費せずに理由を決める。

失敗判定は従来どおり `u >= p`（`p = row['ps']`）。失敗時に次で側を分割する。

```
employer_failed = u < (1 - p_e + p)     # p_e = p_employer_<stage>
理由 = employer_failed ? ng_* : withdrawn_*
```

これは失敗条件付きの `P(employer 側失敗 | 失敗) = (1 - p_e) / (1 - p)` と一致する（反実仮 `counterfactual()` と同じ「employer が通ったなら求職者側、そうでなければ企業側」の優先順位）。**利点**: 市場乱数ストリームも反実仮ストリームも消費量が変わらず、同じ seed の既存の到達日時・`events_latent` 値はビット単位で不変。新列だけが増えるため、#27 の fixture 再生成作業と衝突しない。

参考までに、2回の独立乱数で側を明示的に抽く方式もあるが、その場合は同一 seed の全データが変わり #27 の再生成が必須になるため推奨しない。

### 3.4 校正対象（分母・分子）への影響

| 段階 | 分母 | 分子 | #32 による変化 |
| --- | --- | --- | --- |
| 応募 | 全紹介 | 期限内に `oubo_dt` 到達 | ラベル定義は不変。`not_applied` / `cancelled_seeker_exit` が「確定した負例」として可視化され、右打切り（進行中）と区別できるようになる。 |
| 書類通過 | 期限内に応募済み | 期限内に `shorui_tsuuka_dt` 到達 | 分母は従来どおり「応募済み」。取消は分子に数えず、負例（コンペティング・リスク）として扱う。 |
| 決定 | 期限内に書類通過済み | 期限内に `kettei_dt` 到達（採用） | 同上。`ng_*` / `withdrawn_*` は負例。 |

重要なのは、**取消を新たな正例にしない**こと、および**反実仮確率 `p_*_eventual` を校正ラベルに使わない**こと。「観測到達率」と「反実仮の真の確率」のギャップが #32 で初めて観測可能になるが、校正ターゲットは引き続き観測到達率（実 ATS 相当）である。観測打切り行は maturity 判定（紹介から3暦月が観測締切以内）で従来どおり除外する。

### 3.5 `events_latent` 不変の主張

- スキーマ変更なし：`failure_stage` / `failure_reason` / `latent_*_dt` / `p_*` はそのまま。
- 値も不変：§3.3 の方式により専用乱数ストリーム（`LATENT_STREAM`）と紹介時点の確率計算は一切変わらない。
- 観測理由は `events` にのみ書き込む。`events_latent` のコメント「実績の選考取消理由は保存しない」は維持する。
- 検証：既存の `test_market_oracle` / `test_market_export` の反実仮アサーションを変更せず通す。

---

## 4. 日次スナップショットの正確化（#31 との関係）

#31 の `DAILY_CANDIDATES.md` / `market_candidates.py` は、`events` の到達日時とマスターの在籍情報から日次スナップショット（`other_applications` / `competing_candidates`）を SQL で再構成する。現状は取消が無観測のため、「その時点で進行中のケース」を「まだ到達していないケース」も含めて数えてしまい**上限近似**になっている。

`status` / `outcome_dt` が加わると、スナップショット时刻 `t` で「進行中（applied かつ未クローズ）」である条件を正確に書ける。

```sql
-- 時刻 t に「応募済みでまだ終端していない」ケースを数える述語（#31 レシピの中核）
-- 対象: e.shoukai_dt <= :t の紹介。求職者側なら user_id、求人側なら item_id で分组する。
SELECT e.user_id, e.item_id,
       CASE WHEN e.oubo_dt <= :t
                 AND (e.kettei_dt IS NULL OR e.kettei_dt > :t)
                 AND (e.outcome_dt IS NULL OR e.outcome_dt > :t)
                 AND (u.departure_dt IS NULL OR u.departure_dt > :t)
                 AND (i.departure_dt IS NULL OR i.departure_dt > :t)
            THEN 1 ELSE 0 END AS in_flight
FROM events AS e
JOIN user AS u USING (user_id)
JOIN item AS i USING (item_id)
WHERE e.shoukai_dt <= :t
-- 実際のレシピでは GROUP BY user_id / item_id して SUM(in_flight) を取る。
```

- `status IS NULL` は採用または右打切りも含むため、採用は `kettei_dt`、打切りは `outcome_dt IS NULL` で区別する（§1.2）。
- 紹介時点の特徴量として保存される `events.other_applications` / `events.competing_candidates`（抽選されたポアソン件数）とは別物で、スナップショットは**実測の同時進行件数**である点は #31 の説明のまま。
- **#31 への影響**: `DAILY_CANDIDATES.md` の「上限近似」という記述と、近似であることを前提にした注記を「`status` / `outcome_dt` により正確に再構成できる」に書き換える。#31 の 7テーブル構成・`events_latent` 派生特徴の方針とは衝突しない（追加は `events` の2列のみ）。

---

## 5. 移行影響

| 対象 | 影響と作業 |
| --- | --- |
| **`synthetic_funnel` のテスト** | `test_market.py`: 全 `finish()` 呼び出しで理由が付くこと、採用ケースに負の `status` が付かないこと、カスケードで `ng_auto` / `withdrawn_auto` が出ることを追加。`test_market_export.py`: `EVENTS_TABLE` の列一覧と3フォーマット整合、不変条件 §1.2、`events_latent` 値不変を検証。`test_gui.py` / `test_market_oracle.py` / `test_market_sampling.py`: 既存アサーションは据え置きで通るはず。 |
| **docs** | `DATA_FORMAT.md`: `events` 列表に2列追加、NULL 意味、§2.4 の対応表、観測値／潜在値の区別表を更新。`MARKET.md`: 取消ルールと理由一覧、7テーブル概要を更新。`PROBABILITY_MODEL.md`: 両側ゲートから観測理由を導く旨を追記（確率の目標値は不変）。`DAILY_CANDIDATES.md`（#31）: §4 のとおり上限近似→正確に更新。 |
| **GUI** | `gui/server.py` の `STAGE_COLUMNS` / `STAGE_LABELS` に表示列を追加するか、サンプルテーブルに `status` / `outcome_dt` 列を追加。`MARKET_COUNT_LABELS` は新テーブルがないため不変。`template.html` の「観測値は退出・掲載期限の影響を受ける」注記に取消観測の旨を追記。 |
| **校正 SQL 例** | `calibration_pipeline/examples/sqlite/schema.sql` の `scored_referrals` ビューに2列を追加。`train.sql` のラベル式（`oubo_dt BETWEEN ...`）は不変だが、必要なら「取消は負例」であることをコメントで明記。書類通過・決定の SQL レシピ（#31）の分母に取消を組み込む場合は §3.4 に従う。 |
| **`market_training.py`（学習）** | 学習ラベルは従来どおり「観測到達日時が期限内か」のみ。`status` / `outcome_dt` は**説明特徴に絶対に使わない**（紹介後に確定する情報でリークになる）。取消行は `~reached` の負例に自然に含まれ、これまで「進行中」と混ざっていた分が確定負例になるだけで、ラベル規律は変わらない。manifest の `training.label` 文字列は据え置き。 |
| **fixture（#27）** | §3.3 の方式なら同一 seed の既存値は不変なので再生成は不要（新列が増えるだけ）。もし §3.3 を採らず2乱数方式に変更する場合は、#27 の担当と調整して fixture を作り直す。 |
| **`events_latent` 不変 claim** | §3.5 のとおり。スキーマも値も変えない。 |

---

## 6. 未決定事項（ユーザー確認待ち・最大5項目）

> 各行の先頭が推奨案。回答待ちの間、本文の記述は推奨案を前提にした**草案**として扱う。

- **Q1. 列設計**: **(b) `status` TEXT enum + `outcome_dt` TEXT の2列（推奨）** ／ (a) `ng_dt` / `withdrawn_dt` / `cancelled_dt` ／ (c) status + 理由別日時列。互斥性と `events_latent` との一貫性から (b) を推奨。
- **Q2. NG の段階粒度**: **既存3ステージのまま、screening / decision のゲート失敗として記録し、面接などは decision ゲートに包含（推奨）** ／ 面接フェーズのステージを新設。後者は確率モデル・反実仮・学習・校正が総取り替えになるため非推奨。
- **Q3. 観測範囲**: **掲載期限切れ・求職者自然退出・stage0 の `not_applied` も観測し、観測打切りは NULL（未観測）にする（推奨）** ／ 掲載期限や not_applied は未観測のままにする。スナップショット正確化のため全て観測する案を推奨。
- **Q4. `kettei_dt` と校正ターゲット**: **`kettei_dt` は「採用のみ」のまま、観測取消は「決定に至らない」コンペティング負例として扱い、3か月到達ラベル定義は変更しない（推奨）** ／ `kettei_dt` に取消を含める／校正対象を反実仮確率に変更。後者は既存の確率意味づけを壊すため非推奨。
- **Q5. 自動カスケードの理由粒度**: **`ng_auto` / `withdrawn_auto` を独立した `status` 値として記録（推奨）** ／ 通常の `ng_*` / `withdrawn_*` に統合して別フラグで区別。要件「理由付きで記録」を直接満たす独立値を推奨。

---

## 7. 確定後の実装順序（参考・今回の作業範囲外）

1. `DTYPE` に `status`（文字列は別配列 or 整数コード→文字列マッピング）と `outcome_dt` を追加。
2. `finish(eid, t, reason)` に理由引数を追加し、全呼び出し箇所（§0.2）で理由を渡す。カスケードは §2.3 のように分離し、採用ケース自身をスキップ。
3. stage 1/2 の失敗理由を §3.3 の分割で決定（乱数消費増なし）。
4. `market_export.py` の `EVENTS_TABLE` に2列追加、`_event_records` で datetime 変換して出力。
5. docs / GUI / SQL 例 / テストを §5 に沿って更新し、CI を通す。
