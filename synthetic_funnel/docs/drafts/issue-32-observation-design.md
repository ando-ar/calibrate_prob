# issue #32 設計（確定版）: NG・辞退・取消を観測列として記録する

> ステータス: **確定版**（ユーザー回答 2026-11-18 反映）。本ドキュメントは設計のみを扱い、本番コード（`src/`）の変更は行わない。実装は別タスク。
> 対象: ando-ar/calibrate_prob issue #32。並行作業中の #31（7テーブル構成、`events_latent` への派生特徴移動、日次候補 SQL レシピ）および #27（fixture 再生成）と衝突しない前提で記述する。
> ユーザー確定により草案から2点修正した: ① `auto` フラグ列を追加（全3列）、② 段階別理由値を廃して単一 `ng` / `withdrawn` に統一。確定内容は §6。

## 0. 出発点: 現行の挙動と設計上の前提

### 0.1 観測列は陽性遷移のみ

`events` の日時列は `shoukai_dt`（紹介）/`oubo_dt`（応募）/`shorui_tsuuka_dt`（書類通過）/`kettei_dt`（決定＝採用）の4列で、いずれも陽性遷移の到達時刻のみを記録する。取消されたケースは `finish()` で `cases` から削除されるだけで、`events` には何も書かれない。そのため「取消済み」と「進行中（観測打切り時点で未到達）」は同じ NULL パターンになり区別できない。

### 0.2 `finish()` の呼び出し箇所（全網羅）

`src/synthetic_funnel/market.py` の `finish()` は以下の箇所から呼ばれる。#32 ではここに「理由」と「時刻」を渡すだけで、カスケード構造自体は変更しない。

| # | 箇所（`market.py`） | トリガー | 記録する `status` / `auto` |
| --- | --- | --- | --- |
| 1 | `process()` 冒頭 `sid not in live_s or jid not in live_j` | 相手が既に市場から退出していた（安全網） | `cancelled_market_end` / 0 |
| 2 | `process()` stage 0 `live_s[sid]['until']<=t` | 求職者の応募可能期間満了（応募前に退出） | `cancelled_seeker_exit` / 0 |
| 3 | `process()` stage 1 ゲート失敗 | 書類選考落ち（両側ゲートの不成立） | `ng` / 0 または `withdrawn` / 0（§3.3） |
| 4 | `process()` stage 2 ゲート失敗 | 決定フェーズ失敗（面接などを含む） | `ng` / 0 または `withdrawn` / 0（§3.3） |
| 5 | `process()` 決定成功時の `for other in s['pending']|j['pending']` | 採用成立によるカスケード | `withdrawn` / 1（求職者の他案件）、`ng` / 1（求人の他候補） |
| 6 | 日次ループの掲載期限処理 | 求人の `posting_days` 満了 | `cancelled_posting_expiry` / 0 |
| 7 | 日次ループの求職者期限処理（stage 0 のみ） | 求職者の `application_days` 満了 | `cancelled_seeker_exit` / 0 |

**重要な実装注意点**: 5 のカスケード集合 `s['pending'] | j['pending']` には**採用が成立したケース自身（`eid`）も含まれる**。`finish()` に無条件で理由を記録すると、採用済み（`kettei_dt` 設定済み）の行に負の終端結果が付いてしまう。実装ではカスケード時に `other == eid` をスキップするか、`finish()` 側で `kettei_dt` が finite なら負の結果を書かないガードを入れること（ユーザー確定事項）。

### 0.3 1ケース＝高々1つの終端結果（互斥）

ケースは `finish()` されると復活せず、採用（`kettei_dt`）と取消は排他的である。したがって終端結果は「0個または1個」であり、複数の日時列を同時に持つ必要はない。

### 0.4 `events_latent` は別概念

`events_latent.failure_stage` / `failure_reason`（`not_applied` / `candidate_withdrawal` / `employer_rejection`）は、紹介時点に専用乱数ストリームで計算される**反実仮**の funnel 結果で、市場の退出・掲載期限・カスケードを無視する。#32 が新設するのは**実績（observed）の終端結果**であり、`events_latent` には一切書き込まない（§2.4 / §3.5）。

---

## 1. 列設計（確定）

### 1.1 選択肢の比較と採用案

| 案 | 構成 | 判定 |
| --- | --- | --- |
| (a) 理由別の日時列 | `ng_dt` / `withdrawn_dt` / `cancelled_dt` | 不採用。互斥なのに複数列が空になり、理由の細分化には結局 `reason` 列が必要。 |
| **(b) status + 日時列（採用）** | `status` TEXT + `outcome_dt` TEXT + `auto` INTEGER の**3列** | 採用。2列の基本形に `auto` フラグを足すだけで自動連鎖を区別でき、enum を増やさずに済む。`events_latent` の `failure_stage` + `failure_reason` と同じ発想。 |
| (c) status + 理由別日時列 | `status` + `ng_dt` / `withdrawn_dt` / `cancelled_dt` | 不採用。同じ時刻が重複し不整合が発生しうる。 |

ユーザー回答により (b) をベースに、`auto` フラグ列を1つ追加して**全3列**とする（§6 Q1 / Q5）。

### 1.2 `events` に追加する列

7テーブル構成・マスター分離の方針は不変。`events` に次の3列を追加する。

| 列 | 型 | NULL / 値の意味 | 内容 |
| --- | --- | --- | --- |
| `status` | TEXT | NULL = 終端結果が未観測（進行中／採用／観測打切りのいずれか） | 観測された終端結果。§2.2 の6値のいずれか。採用行では NULL のまま。 |
| `outcome_dt` | TEXT | `status` と同じ（終端結果なし） | `finish()` が呼ばれた時刻を `market_start` からのオフセットで持つ ISO 8601 文字列。 |
| `auto` | INTEGER | 0/1（`status = 'ng'` または `'withdrawn'` のときのみ意味を持つ） | 1 = 採用成立による自動連鎖（自動NG／自動辞退）、0 = それ以外。`status` が他の値または NULL の行では常に 0 で、利用者は無視する。 |

不変条件（テストで検証すべき項目）:

1. `status IS NULL ⟺ outcome_dt IS NULL`。このとき `auto = 0`。
2. `status IS NOT NULL` の行では `kettei_dt IS NULL`（採用と取消は排他）。
3. `outcome_dt` は到達済みの最後の段階日時以上、かつ `observation_end_exclusive` 未満。
4. `auto = 1` は `status IN ('ng', 'withdrawn')` の行にのみ現れる。
5. 1行に `status` は高々1値。複数理由の同時発生はない。

`status IS NULL` は「失敗しなかった」ではなく「**観測された終端結果がない**」であり、採用済み（`kettei_dt` 非NULL）と進行中／観測打切り（典型的には `kettei_dt` NULL）が混在する。採用かどうかは従来どおり `kettei_dt` で判定する。

### 1.3 粗い分類の導出（ビュー例）

```sql
CREATE VIEW event_outcomes AS
SELECT e.*,
       CASE
         WHEN e.status IS NULL THEN NULL
         WHEN e.status = 'ng' THEN 'ng'
         WHEN e.status = 'withdrawn' THEN 'withdrawn'
         WHEN e.status IN ('not_applied', 'cancelled_posting_expiry',
                           'cancelled_seeker_exit', 'cancelled_market_end') THEN 'cancelled'
       END AS outcome_group
FROM events AS e;
```

`not_applied` は応募前の中断なので `cancelled` 側に含める。保存するのは詳細な `status`（6値）と `auto` のみで、粗分類は派生値として扱う。

### 1.4 `daily` テーブルについて

`daily` の列（`applications` / `screenings` / `decisions`）は現行のまま変更しない。取消件数の日次集計は `events.outcome_dt` から SQL で取得でき、#32 の完了条件に `daily` の拡張は含まれない。必要なら別 issue で `ngs` / `withdrawals` / `cancellations` を追加する（任意のフォローアップ）。

---

## 2. 理由分類体系（確定）

### 2.1 OBSERVED / 未観測の判断方針

「実プラットフォームの ATS に記録され、日次スナップショットの正確化に寄与するか」を基準にする。ユーザー回答により**最大範囲を観測**する。

| 理由 | 判定 | 根拠 |
| --- | --- | --- |
| 書類選考落ち | **観測**（`ng`） | 必須要件。 |
| 決定までのフェーズ（面接など）での落ち | **観測**（`ng`） | 必須要件。段階は分けず、詳細は `shorui_tsuuka_dt` から導出（§2.2）。 |
| 応募後・書類選考前の辞退 | **観測**（`withdrawn`） | 必須要件。同上。 |
| 自動辞退（他案件で採用） | **観測**（`withdrawn` + `auto=1`） | 必須要件をフラグで表現。 |
| 自動NG（求人が他候補を採用） | **観測**（`ng` + `auto=1`） | 同上。 |
| 掲載期限切れ | **観測**（`cancelled_posting_expiry`） | 取消理由の分類体系で明示対象。 |
| 求職者の自然退出（応募可能期間満了） | **観測**（`cancelled_seeker_exit`） | 同上。主に stage 0 の未応募ケースで発生。 |
| 相手の退出由来（安全網の #1） | **観測**（`cancelled_market_end`） | 残余ケース用の generic 値。 |
| 応募前のゲート不成立 | **観測**（`not_applied`） | スナップショットで「応募待ち」が消えたことを正確に表すために必要。 |
| **観測打切り（observation-end censoring）** | **未観測（NULL）** | 取消ではなく打切り。負例にせず右打切りとして扱う（§3.4）。 |

### 2.2 確定 `status` enum（6値・互斥）

| `status` 値 | 粗分類 | トリガー | 対応する `finish()` 呼び出し（§0.2） |
| --- | --- | --- | --- |
| `not_applied` | cancelled | stage 0 のゲート失敗（応募に至らなかった） | #2 の他に stage 0 の抽選失敗 |
| `ng` | ng | 書類選考または決定フェーズの**企業側**不成立（両側ゲートの失敗のうち企業側） | #3, #4（`auto=0`）／ #5 の求人側（`auto=1`） |
| `withdrawn` | withdrawn | 書類選考または決定フェーズの**求職者側**不成立／他案件での採用 | #3, #4（`auto=0`）／ #5 の求職者側（`auto=1`） |
| `cancelled_posting_expiry` | cancelled | 求人の掲載期限満了 | #6 |
| `cancelled_seeker_exit` | cancelled | 求職者の応募可能期間満了／自然退出（主に stage 0） | #2, #7 |
| `cancelled_market_end` | cancelled | 相手が既に退出していた場合の安全網 | #1 |

**段階の導出（ユーザー確定）**: `ng` / `withdrawn` の発生段階は保存せず、利用者が `shorui_tsuuka_dt` の有無から計算する。ドキュメントに次を明記する。

- `status = 'ng'` かつ `shorui_tsuuka_dt IS NULL` → 書類選考ゲートでの NG。
- `status = 'ng'` かつ `shorui_tsuuka_dt IS NOT NULL`（かつ `kettei_dt IS NULL`）→ 書類通過後、決定までのフェーズ（面接などを含む）での NG。
- `withdrawn` も同様に解釈する（`shorui_tsuuka_dt IS NULL` なら応募後・書類選考前の辞退）。
- `auto = 1` の行では自動連鎖が主情報であり、段階の有無は副次的な情報として扱う。

### 2.3 自動カスケードの記録（auto フラグ）

決定成功時に `s = live_s.pop(sid)` / `j = live_j.pop(jid)` とした後、現在は `for other in s['pending'] | j['pending']` と統合している。実装ではこれを次のように分離し、範囲演算子 `|` を外す。

```python
for other in s['pending']:
    finish(other, t, 'withdrawn', auto=1)   # 求職者が他案件で採用された → 自動辞退
for other in j['pending']:
    finish(other, t, 'ng', auto=1)          # 求人が他候補を採用した → 自動NG
```

いずれも `other == eid`（採用成立ケース自身）はスキップする（§0.2）。一求人一採用の簡略化前提は変更しない。ゲート失敗による通常の `ng` / `withdrawn` は `auto = 0` で記録する。`ng_auto` / `withdrawn_auto` のような専用理由値は設けない（ユーザー確定）。

### 2.4 反実仮 `events_latent.failure_reason` との対応（別概念）

語彙が似ているため混同を避けるための対応表を docs に載せる。

| 観測（`events`） | 反実仮 `failure_reason`（`events_latent`） | 違い |
| --- | --- | --- |
| `ng`（`auto=0`） | `employer_rejection` | 観測は実際にその時点で中止；反実仮は「市場が媒介を続けた場合」の funnel 結果。 |
| `withdrawn`（`auto=0`） | `candidate_withdrawal`（stage 0 は `not_applied`） | 同上。 |
| `not_applied` | `not_applied`（stage 0） | 同上。 |
| `ng` / `withdrawn` の `auto=1` | **対応なし（`failure_stage` が NULL でもあり得る）** | カスケードは市場固有のイベントなので反実仮には存在しない。重要な相違点。 |
| `cancelled_posting_expiry` / `cancelled_seeker_exit` / `cancelled_market_end` | **対応なし** | 退出・掲載期限は反実仮では無視される。 |
| NULL（採用） | NULL（全段階通過） | 採用は `kettei_dt` で表現し、両テーブルとも理由は NULL。 |

---

## 3. 確率意味づけとの相互作用（確定）

### 3.1 `kettei_dt` は「採用のみ」のまま

`pd` は紹介時点で計算された「決定ゲートに成功する確率」であり、`kettei_dt` が記録されるのは両側が合意して採用が成立したときだけである。#32 で NG を観測しても、この意味は変えない。NG は `kettei_dt` の代わりに `status = 'ng'` として記録する。利点は次。

- 既存の校正 SQL / warmup 学習 / GUI の正例ラベル（`kettei_dt` 非NULL）がそのまま動く。
- 「決定に至る確率」という `pd` の目標定義（通過者内 7%）と、観測される採用率の対応が壊れない。
- `kettei_dt` と `status` の排他性が不変条件として明示できる（§1.2）。

### 3.2 段階を増やさず単一 `ng` / `withdrawn` で記録

ユーザー確定により、段階別の理由値（`ng_screening` / `ng_decision` / `withdrawn_screening` / `withdrawn_decision`）は廃止し、単一の `ng` / `withdrawn` に統一する。issue の「決定までのフェーズ（面接など）での落ちも含む」は、既存3ステージの decision ゲートに包含し、発生段階は `shorui_tsuuka_dt` の有無から利用者が導出する（§2.2）。`ProbabilityConfig` / `mechanism` / `events_latent` / warmup 学習 / 校正対象の定義は変更しない。

### 3.3 両側ゲートと乱数序列（ビット完全一致）

確率モデルでは stage 1/2 は求職者側・企業側の独立なゲートの積 `p = p_candidate * p_employer` で進む。観測 NG / 辞退を区別するには、ゲート失敗がどちら側によるものかを知る必要がある。そこで、既存の1回の乱数 `u = rng.random()` と行内の `p_candidate_*` / `p_employer_*` から、乱数を追加消費せずに理由を決める。

失敗判定は従来どおり `u >= p`（`p = row['ps']`）。失敗時に次で側を分割する。

```
employer_failed = u < (1 - p_e + p)     # p_e = p_employer_<stage>
status = 'ng' if employer_failed else 'withdrawn'   # auto = 0
```

これは失敗条件付きの `P(employer 側失敗 | 失敗) = (1 - p_e) / (1 - p)` と一致する（反実仮 `counterfactual()` と同じ「employer が通ったなら求職者側、そうでなければ企業側」の優先順位）。**利点**: 市場乱数ストリームも反実仮ストリームも消費量が変わらず、同じ seed の既存の到達日時・`events_latent` 値はビット単位で不変。新列（3つ）だけが増えるため、#27 の fixture 再生成作業と衝突しない（ユーザー確定事項）。

参考までに、2回の独立乱数で側を明示的に抽く方式もあるが、その場合は同一 seed の全データが変わり #27 の再生成が必須になるため採用しない。

### 3.4 校正対象（分母・分子）への影響

| 段階 | 分母 | 分子 | #32 による変化 |
| --- | --- | --- | --- |
| 応募 | 全紹介 | 期限内に `oubo_dt` 到達 | ラベル定義は不変。`not_applied` / `cancelled_seeker_exit` が「確定した負例」として可視化され、右打切り（進行中）と区別できるようになる。 |
| 書類通過 | 期限内に応募済み | 期限内に `shorui_tsuuka_dt` 到達 | 分母は従来どおり「応募済み」。取消は分子に数えず、負例（コンペティング・リスク）として扱う。 |
| 決定 | 期限内に書類通過済み | 期限内に `kettei_dt` 到達（採用） | 同上。`ng` / `withdrawn`（`auto` の値を問わず）は負例。 |

重要なのは、**取消を新たな正例にしない**こと、および**反実仮確率 `p_*_eventual` を校正ラベルに使わない**こと。「観測到達率」と「反実仮の真の確率」のギャップが #32 で初めて観測可能になるが、校正ターゲットは引き続き観測到達率（実 ATS 相当）である。観測打切り行は maturity 判定（紹介から3暦月が観測締切以内）で従来どおり除外する。`status` / `outcome_dt` / `auto` は**学習特徴量として使用禁止**（ユーザー確定）。

### 3.5 `events_latent` 不変の主張

- スキーマ変更なし：`failure_stage` / `failure_reason` / `latent_*_dt` / `p_*` はそのまま。
- 値も不変：§3.3 の方式により専用乱数ストリーム（`LATENT_STREAM`）と紹介時点の確率計算は一切変わらない。
- 観測理由は `events` にのみ書き込む。`events_latent` のコメント「実績の選考取消理由は保存しない」は維持する。
- 検証：既存の `test_market_oracle` / `test_market_export` の反実仮アサーションを変更せず通す。

---

## 4. 日次スナップショットの正確化（#31 との関係）

#31 の `DAILY_CANDIDATES.md` / `market_candidates.py` は、`events` の到達日時とマスターの在籍情報から日次スナップショット（`other_applications` / `competing_candidates`）を SQL で再構成する。現状は取消が無観測のため、「その時点で進行中のケース」を「まだ到達していないケース」も含めて数えてしまい**上限近似**になっている。

`status` / `outcome_dt`（と `auto`）が加わると、スナップショット时刻 `t` で「進行中（applied かつ未クローズ）」である条件を正確に書ける。

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
-- status / auto の値自体は進行中判定に不要（終端の有無だけを見る）。
```

- `status IS NULL` は採用または右打切りも含むため、採用は `kettei_dt`、打切りは `outcome_dt IS NULL` で区別する（§1.2）。
- 紹介時点の特徴量として保存される `events.other_applications` / `events.competing_candidates`（抽選されたポアソン件数）とは別物で、スナップショットは**実測の同時進行件数**である点は #31 の説明のまま。
- **#31 への影響**: `DAILY_CANDIDATES.md` の「上限近似」という記述と、近似であることを前提にした注記を「`status` / `outcome_dt` により正確に再構成できる」に書き換える。#31 の 7テーブル構成・`events_latent` 派生特徴の方針とは衝突しない（追加は `events` の3列のみ）。

---

## 5. 移行影響

| 対象 | 影響と作業 |
| --- | --- |
| **`synthetic_funnel` のテスト** | `test_market.py`: 全 `finish()` 呼び出しで理由が付くこと、採用ケースに負の `status` が付かないこと、カスケードで `ng` + `auto=1` / `withdrawn` + `auto=1` が出ること、ゲート失敗は `auto=0` であることを追加。`test_market_export.py`: `EVENTS_TABLE` の列一覧（3列追加）と3フォーマット整合、不変条件 §1.2、`events_latent` 値不変を検証。`test_gui.py` / `test_market_oracle.py` / `test_market_sampling.py`: 既存アサーションは据え置きで通るはず。 |
| **docs** | `DATA_FORMAT.md`: `events` 表に3列追加、NULL 意味、§2.2 の段階導出ルール、§2.4 の対応表、観測値／潜在値の区別表を更新。`MARKET.md`: 取消ルールと6値の理由一覧、`auto` フラグ、7テーブル概要を更新。`PROBABILITY_MODEL.md`: 両側ゲートから観測理由を導く旨を追記（確率の目標値は不変）。`DAILY_CANDIDATES.md`（#31）: §4 のとおり上限近似→正確に更新。 |
| **GUI** | `gui/server.py` の `STAGE_COLUMNS` / `STAGE_LABELS` に表示列を追加するか、サンプルテーブルに `status` / `outcome_dt` / `auto` 列を追加。`MARKET_COUNT_LABELS` は新テーブルがないため不変。`template.html` の「観測値は退出・掲載期限の影響を受ける」注記に取消観測の旨を追記。 |
| **校正 SQL 例** | `calibration_pipeline/examples/sqlite/schema.sql` の `scored_referrals` ビューに3列を追加。`train.sql` のラベル式（`oubo_dt BETWEEN ...`）は不変だが、必要なら「取消は負例」であることをコメントで明記。書類通過・決定の SQL レシピ（#31）の分母に取消を組み込む場合は §3.4 に従う。 |
| **`market_training.py`（学習）** | 学習ラベルは従来どおり「観測到達日時が期限内か」のみ。`status` / `outcome_dt` / `auto` は**説明特徴に絶対に使わない**（紹介後に確定する情報でリークになる。ユーザー確定事項）。取消行は `~reached` の負例に自然に含まれ、これまで「進行中」と混ざっていた分が確定負例になるだけで、ラベル規律は変わらない。manifest の `training.label` 文字列は据え置き。 |
| **fixture（#27）** | §3.3 の方式なら同一 seed の既存値はビット完全一致なので再生成は不要（新列が増えるだけ）。 |
| **`events_latent` 不変 claim** | §3.5 のとおり。スキーマも値も変えない。 |

---

## 6. 確定事項（ユーザー回答 2026-11-18）

- **Q1 列設計**: (b) を採用。`events` への追加列は **`status` / `outcome_dt` / `auto` の3列**。`auto` は INTEGER 0/1 で、`ng` / `withdrawn` のときのみ意味を持つ。
- **Q2 NG の段階粒度**: 段階は分けない。`ng_screening` / `ng_decision` を廃止して単一の **`ng`** に統一。`withdrawn` も単一値。書類選考ゲートか決定ゲートかは `shorui_tsuuka_dt` の有無から利用者が計算できることを docs に明記（§2.2）。
- **Q3 観測範囲**: 最大範囲を観測で確定。掲載期限・求職者自然退出・`not_applied` も観測し、**観測締切打切りのみ NULL**。
- **Q4 確率意味づけ**: 現行枠組を維持。`kettei_dt` は採用のみ、取消は競合負例、3暦月ラベル不変、`status` / `outcome_dt` / `auto` は学習特徴量禁止。
- **Q5 自動連鎖**: 専用理由値（`ng_auto` / `withdrawn_auto`）を廃止し、**`auto` フラグ**で区別。最終的な `status` 列挙は `not_applied` / `ng` / `withdrawn` / `cancelled_posting_expiry` / `cancelled_seeker_exit` / `cancelled_market_end` の6値（+ `auto` フラグ）。
- その他の確定事項: 同一 RNG 抽選からの NG 側導出によるビット完全一致、cascade で hired ケースの `finish()` スキップ、#31 スナップショットの正確化、`events_latent` 不変。

---

## 7. 実装順序（参考・今回の作業範囲外）

1. `DTYPE` に `status`（文字列は別配列 or 整数コード→文字列マッピング）、`outcome_dt`、`auto`（0/1）を追加。
2. `finish(eid, t, status, auto=0)` に理由とフラグを渡すよう変更し、全呼び出し箇所（§0.2）で設定。カスケードは §2.3 のように分離し、採用ケース自身をスキップ。
3. stage 1/2 の失敗理由を §3.3 の分割で決定（乱数消費増なし、`auto=0`）。
4. `market_export.py` の `EVENTS_TABLE` に3列追加、`_event_records` で datetime 変換して出力。
5. docs / GUI / SQL 例 / テストを §5 に沿って更新し、CI を通す。
