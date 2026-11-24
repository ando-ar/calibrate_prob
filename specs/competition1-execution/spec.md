# competition1 実行フェーズ仕様（spec）

> ステータス: **草案（実行フェーズ spec）**。Spec Kit の成果物テンプレート（spec.md / tasks.md / checklists/）の
> 「型」だけを借用したドキュメント・スキャフォールドであり、`specify` CLI は導入していない
> （`docs/drafts/SPECKIT_PROPOSAL.md` §6 パイロット計画の適用）。
> 対象: ando-ar/calibrate_prob、issue #33（competition1 の開始）／#47（解法多様化の仕掛け）。
> 位置づけ: 本 spec は「コンペの実行（開催）フェーズ」を定義する。**シミュレータ本体は変更しない**。
> 正本の仕様は `synthetic_funnel/docs/COMPETITION.md`、運営ルールは
> `competitions/competition1/organizer/RULES.md`、凍結 config は
> `competitions/competition1/organizer/frozen_config.json`。本 spec はこれらを「実行手順」として統合する。

## 0. 目的

competition1 を実際に開催するための実行フェーズ仕様を定める。具体的には (1) 凍結 config（#46 後の
`segment_drift` 込み）でのデータ再生成と data version（manifest ハッシュ）の確定、(2) #47 の解法多様化
仕掛け（チーム役割割当・反収束ルール・温度/ペルソナ・副次リーダーボード・**チーム別データビュー**）の
適用、(3) チーム別 participant パッケージの組み立て、(4) 提出受付・検証・評価・集計・結果公表までを、
依存順に実施可能な形で定義する。

参加者（＝ユーザーが運用する少数の AI チーム）は、配布された観測データのみで日次ランキングを構築し、
rank のみを提出する。運営は各提出を同じ凍結データ・同じ評価器で採点し、主指標
`macro_daily_replay_fee_yen` で順位を決める。解法多様化の仕掛けは**探索過程と配布物の差**に作用し、
最終成果物の形式（rank のみ）と勝敗の契約（主指標）は変えない。

## 1. スコープと不変条件

### 1.1 本 spec の範囲

| 範囲 | 内容 |
| --- | --- |
| **含む** | データ再生成（data version 確定）、チーム別データビューの導出、participant パッケージ組み立て、提出受付・検証、評価器実行、seed 集計、副次リーダーボード、実験記録の更新、既知リスクの確認 |
| **含まない** | シミュレータ本体（`synthetic_funnel/src/`、`mechanism.py` 等）の変更、凍結 config の市場パラメータ変更、評価器の採点ロジック変更、fee の潜在化（#45 で不採用確定） |
| **境界** | 主指標・提出形式・data version の定義は `COMPETITION.md` / `RULES.md` / `frozen_config.json` が正であり、本 spec はこれらを変更しない |

### 1.2 不変条件（本 spec が壊さない契約）

1. **主指標は `macro_daily_replay_fee_yen` のまま**（リプレイ観測実績手数料の日次 macro 平均）。副指標
   `macro_daily_logged_fee_yen` / `macro_daily_counterfactual_fee_yen` は診断のみ。
2. **提出は rank のみ**（`day_dt, user_id, item_id, rank`）。確率列・期待手数料列は診断用で採点しない。
3. **data version = 配布 `manifest.json` 全体の SHA-256**。参加者配布 `manifest.json` は運営側
   `manifest.json` のバイト同一コピーであり、運営と参加者の data version は一致する（`RULES.md` §2.2）。
   **チーム別データビューはこの不変条件を壊してはならない**（§3.3）。
4. **fee は一律 33%・完全既知**（`candidates.fee_if_decided_yen`）。fee 推定は順位に効かせない（#45 確定）。
5. **評価 seed は 71〜75 の 5 本**、評価期間 `[150, 180)`（30 日・複数日）、`H = 3 暦月`、
   `observation_end_exclusive = 280`。単一 seed の差を普遍的な優劣と呼ばない（平均±標準偏差で比較）。
6. **`max_training_rows = 300,000`**（参加者側の自己申告＋レビュー制約。運営評価器は強制しない）。

## 2. チーム構成（#47 の誘導側仕掛け）

参加者は**ユーザーが運用する 5 つの AI チーム**（Team A〜E）とする。各チームに解法クラスを 1 つ割り当て、
探索を独立・盲目的に分岐させる（`docs/drafts/SOLUTION_DIVERSITY.md` §5.1）。

### 2.1 チーム役割割当（ソフト）

| チーム | 解法クラス（役割） | データビュー（§3.3） |
| --- | --- | --- |
| Team A | 校正重視（確率の較正を優先し、p×fee を丁寧に推定） | 直近ウィンドウ重視 |
| Team B | ランキング重視（学習-to-rank で順序を直接最適化） | 全ウィンドウ |
| Team C | セグメント探索（職種/地域セグメント別モデル） | セグメント標本 |
| Team D | 単純モデル至上（線形・ロジスティック回帰に限定） | 全ウィンドウ＋行数 15 万上限 |
| Team E | アンサンブル型（複数モデルの組合せ） | 全ウィンドウ・30 万行 |

- 役割は**ソフト**（システムプロンプト／運営配布のチーム説明文で与える）。各チームは**解き終わるまで
  相互参照を禁止**する（独立性の設計。`SOLUTION_DIVERSITY.md` §2.1 の structural coupling 回避）。
- チーム数は 3〜5 を許容するが、本番既定は 5（解法クラス数と対応）。チームを減らす場合は役割と
  データビューを対で削る（§6 Q&A 参照）。

### 2.2 反収束ルール（最小のハードルール 1 本）

- **「全チームが同一モデル族で揃うことを禁止」**。各チームは割り当てられた解法クラスに沿う解法を
  自己申告し、同じモデル族（例: 全チーム GBDT、全チームロジスティック回帰）へ収束してはならない。
- 任意で「アンサンブル禁止 or 推奨」の明示をチーム説明文に加える（Team E のみアンサンブル可、他は
  単一モデル族を原則とする）。

### 2.3 バックエンド・温度・ペルソナ

- **同一バックエンド（deepseek-v4.1-flash）** を全チーム共通で使う（トークン削減・運用コスト抑制）。
  異なるバックエンドはオプション（`SOLUTION_DIVERSITY.md` §5.2）。
- チームごとに **temperature とペルソナ／思考スタイルプロンプト**を変える（例: 保守的/探索的、
  データ指向/モデル指向）。生成設定とペルソナ指示は `run_metadata.json` 相当に記録する。

## 3. データ配布（含むチーム別データビュー）

### 3.1 凍結データと data version

- 各 seed の生成は `synthetic_funnel` の市場生成（`write_market_dataset`）のみを使い、`manifest.json` に
  config・seed・`generator_version`・`training_cutoff`・`warmup_end`・`observation_end_exclusive`・各ファイル
  SHA-256 を記録する。**#46 で `segment_drift` が config に加わったため、data version（manifest ハッシュ）は
  旧 proof 値（`competition1-proof`）から変わる**（`RULES.md` §1 注記）。
- 配布物と非配布物は `COMPETITION.md` §1 / `RULES.md` §8 のとおり。潜在テーブル
  （`user_latent` / `item_latent` / `events_latent` / `candidates_latent`）は運営専用・非配布。

### 3.2 チーム別データビューの原則

#47 のユーザー確定（2026-11-24）により、**チーム別データビューを competition1 の仕掛けに含める**
（`SOLUTION_DIVERSITY.md` §7 #5）。原則は次の 3 点。

1. **生成機構は不変**。データビューは「同一凍結データセット」から導出する配布物の差であり、データ生成
   を変えない（シミュレータ変更なし）。
2. **data version は全チーム共通**。凍結データ（`dataset.sqlite.db` + `manifest.json`）はバイト同一で全チームに
   配布し、チームごとの差は「ビュー」として付加する。manifest ハッシュ（＝data version）は不変。
3. **評価対象は全チーム同一**。評価日の完全候補集合 `candidates_eval.parquet` と評価器は全チーム共通で、
   提出の採点は同じ凍結データ・同じ真値に対して行う（順位の比較可能性を保つ）。

### 3.3 チーム別データビューの規則（設計）

ビューが制約するのは**学習に使える `events` 行（参加者学習期間 `[90, 150)`）と学習行数上限**のみであり、
`user` / `item` / `daily` マスタ、評価候補、ベースライン提出は全チーム共通とする。

| チーム | ビュー規則（学習 `events` に対する述語。`shoukai_dt` は紹介日時、`occupation` はマスタ結合） | `max_training_rows` |
| --- | --- | --- |
| Team A（直近ウィンドウ重視） | `shoukai_dt ∈ [120, 150)`（学習期間の直近 30 日のみ） | 300,000 |
| Team B（全ウィンドウ） | `shoukai_dt ∈ [90, 150)`（学習期間全体 60 日） | 300,000 |
| Team C（セグメント標本） | `shoukai_dt ∈ [90, 150)` ∧ `occupation ∈ {エンジニア, 営業}`（8 職種のうち固定 2 職種） | 300,000 |
| Team D（15 万行上限） | `shoukai_dt ∈ [90, 150)`（全体）だが行数上限を 150,000 に制限 | **150,000** |
| Team E（全 30 万行） | `shoukai_dt ∈ [90, 150)`（全体・制限なし） | 300,000 |

- `occupation` は `user` / `item` マスタにのみ存在し `events` に無いため、Team C のビュー導出は
  `events ⋈ user ⋈ item`（または `events ⋈ item`）の結合で職種を付与してからフィルタする。
- Team D の 15 万行は**一様抽出**（固定 seed の `random.Random`）で決定的に選択し、選択率・取得件数を記録する。
  Team C の職種部分集合も `team_config.json` で固定する（既定 `{エンジニア, 営業}`。任意で地域部分集合に差し替え可）。
- ビューは「許可する行」を狭めるだけで、行の中身（列・値）を改変しない。行内容の改変は生成機構の変更になり
  data version を変えるため禁止。

### 3.4 配布形態（data version を壊さない配布）

- **全チーム共通の凍結データ**（`dataset.sqlite.db` + `manifest.json` + `config.json` + ベースライン
  `submission.parquet` + `baseline_model.json`）は従来どおり 1 部生成し、data version を確定する。
- チーム別には次の**ビュー成果物**を participant パッケージへ付加する（`participants/data/seed-<s>/team-<A..E>/`）:
  - `team_view.md` — 役割・ビュー規則・反収束ルール・温度/ペルソナの人間向け説明（§2 の内容を含む）。
  - `team_view.json` — ビュー規則の機械可読な表現（述語・行数上限・抽出 seed・職種部分集合）。
  - `team_view_events.parquet` — ビュー規則を適用した学習 `events` の**便宜的な物資化**（取得件数・選択率を記録）。
- 凍結データの `events` 全表は配布する（data version 不変のため）。ビューの遵守は `max_training_rows` と同じく
  **自己申告＋コードレビュー**で担保する（`RULES.md` §5.2 をチームビューへ拡張）。物資化 parquet は「どの行が
  許可されるか」を参加者が機械的に確認できる補助であり、物理的な行削除は行わない。
- 検討した代替案（チームごとに `events` を物理的に切り詰めた別 DB を配布）は、manifest ハッシュがチームごとに
  分かれて「運営＝参加者の data version 一致」を壊すため不採用（`RULES.md` §2.2）。

## 4. 提出・採点

### 4.1 提出形式（`RULES.md` §4 踏襲）

- `submission.parquet`（正式）または `submission.csv`。列: `day_dt` / `user_id` / `item_id` / `rank`（必須）、
  `selected` / `p_*` / `expected_fee_yen`（任意・診断）。
- キー `(day_dt, user_id, item_id)`。提出行は配布 `candidates` の部分集合でよい。枠割当は評価器が再計算する。
- `submission_metadata.json` を添付（seed、Python/主要パッケージ版、git commit、config ハッシュ、data version、
  author、`generated_at`）。**author にチーム名（Team A〜E）を記録**する。

### 4.2 採点

- 評価器 CLI: `synthetic-competition evaluate --manifest <organizer dir> --submission <f> --candidates-latent <dir>
  --population-dir <dir> --output <out>`（`RULES.md` §3.2）。同じ入力から同じ出力（決定論的）。
- 合否: 主指標が同一 data version のベースラインを上回ること（必要なら `margin`）。単一 seed の差を優劣と呼ばない。
- **チーム別データビューは採点に影響しない**。全チーム同じ評価器・同じ真値で採点する。

### 4.3 副次リーダーボード（表示のみ）

主指標で勝敗を決める契約は変えず、**表示のみ**の副次リーダーボードを併記する（`SOLUTION_DIVERSITY.md` §5.1 #4）。

| 列 | 内容 |
| --- | --- |
| seed 間標準偏差 | `macro_daily_replay_fee_yen` の `mean ± std(ddof=0)`（`RULES.md` §3.3 既存） |
| ロバスト性 | 5 seed 中の最低値／最大値（seed 間の振れ幅の可視化） |
| 簡潔性 | 提出コードの行数・モデル族数（自己申告＋レビュー） |
| 再現性 | 提出の決定論・`submission_sha256` 一致の確認結果 |

- 副次リーダーボードは**順位を変えない**（主指標のみで順位）。多様性メトリクス（Vendi Score・解法 writeup の
  埋め込み類似度）は事後に運営側で算出し、仕掛けの効果検証に使う（§5.6）。

## 5. 運用手順

運用手順の詳細（依存順・各ステップの入出力・検証）は [`tasks.md`](tasks.md) に記す。本節は全体像を示す。

| 段階 | 内容 | 成果物 |
| --- | --- | --- |
| 1. データ再生成 | `build_release.py --seeds 71,72,73,74,75`（#46 後 config、逐次実行＋都度削除） | 各 seed の manifest ハッシュ、`data_versions.json`、`baseline_aggregate.json` |
| 2. チームビュー導出 | `team_config.json` からチーム別ビューを導出（物資化 parquet + 規則 JSON/MD） | チーム別ビュー成果物 |
| 3. パッケージ組み立て | 凍結データ＋チーム別ビューを participant パッケージへ組み立て | チーム別配布物 |
| 4. 提出受付・検証 | 提出スキーマ・決定論・data version・チームビュー遵守を検証 | 検証結果 |
| 5. 評価器実行 | チーム×seed の評価（freeze/snapshot 再利用、逐次実行） | `metrics.json` / `daily.csv` / `summary.md` |
| 6. 集計・副次ボード | seed 平均±標準偏差、副次リーダーボード（表示のみ） | リーダーボード |
| 7. 実験記録更新 | `experiments/competition1/` へ run 記録、data version 整合 | `run_metadata.json` |
| 8. 既知リスク確認 | 既知リスク・フォールバックの確認（`checklists/quality.md`） | 確認結果 |

### 5.1 再生成の制約（実行タスク 1）

- 空き約 17GB のため、5 seed を同時保持しない。seed ごとに「生成 → participant パッケージ化 → organizer truth
  保持 → 評価器実行 → metrics/manifest 保存 → DB/parquet 削除」を逐次実行（`--jobs 2-3` 以下）。
- `metrics.json`・manifest ハッシュ・`summary.md` は削除せず残す（再現性担保）。
- 1 seed あたり約 10 分。**本 spec の作成時点では再生成しない**（実行タスク 1 は開催時に実行）。

### 5.2 フリーズ／スナップショットの再利用

- D_train（offset 150）の凍結状態は「同一 config/seed の day 0 からの再シミュレーション」で再現し、巨大
  スナップショットを保存しない（`COMPETITION.md` §5.2）。**同一 seed の複数チーム評価では凍結状態・人口再構成
  （`--population-dir`）を再利用**し、seed ごとの再生成を繰り返さない。
- 各チームの提出は同じ seed・同じ真値・同じ評価器で評価する（方策だけが異なる）。

### 5.3 検証期間と最終評価

- 検証期間のボードは参考値。最終評価は**選んだ 1 提出に対して 1 回だけ**実行する（`RULES.md` §7 ラウンド構造）。
- 最終結果を見てから候補を増やした場合は、新しい holdout を用意する（`COMPETITION.md` §5.4）。

### 5.4 実験記録

- `experiments/competition1/` の規約（`COMPETITION.md` §8）に従い、`<stage>/<run_id>/`（`yyyyMMdd_HHmmss`）へ記録。
- `run_metadata.json` に data version（manifest ハッシュ）・seed・チーム名・ビュー規則・抽出戦略・取得件数・
  温度/ペルソナ・git commit・環境を記録する。

### 5.5 既知リスク

詳細は [`checklists/quality.md`](checklists/quality.md) と [`tasks.md`](tasks.md) タスク 8 に記す。主なもの:

- data version の再現性（生成環境・依存が変わると同一 seed でも行が変わる）→ 環境を凍結して記録。
- 主指標は決定件数が少なく（5 seed で ~45-50 件）分散が大きい → 平均±標準偏差で報告し、単一 seed の差を優劣としない。
- チームビューの遵守は自己申告依存 → 上位入賞者のコードレビューで学習行数・ビュー・禁止列を確認。
- 評価器の決定論（同じ入力→同じ出力）→ 実行ごとに `submission_sha256` と metrics を照合。

## 6. 未決点 Q&A（ユーザー確定の記録）

本 spec は既存のユーザー確定を参照し、実行フェーズに固有の未決点のみを残す。確定済みの判断は**ユーザー確定が正**
であり、以下に参照付きで記録する（`SOLUTION_DIVERSITY.md` §7・`COMPETITION_DIFFICULTY.md` §5 と同様の扱い）。

### 6.1 確定済み（ユーザー確定 2026-11-24）

| # | 項目 | 確定内容 | 参照 |
| --- | --- | --- | --- |
| 1 | チーム数と役割割当 | 3〜5 チーム、各チームに異なる解法クラスを 1 つずつ（既定 5 チーム） | `SOLUTION_DIVERSITY.md` §7 #1 |
| 2 | 誘導の強制力 | 役割はソフト（プロンプト推奨）＋「全チーム同一モデル族の禁止」のハード反収束ルール 1 本 | `SOLUTION_DIVERSITY.md` §7 #2 |
| 3 | バックエンドと温度 | 同一バックエンド（deepseek-v4.1-flash）＋温度/ペルソナ多様化 | `SOLUTION_DIVERSITY.md` §7 #3 |
| 4 | 評価側の扱い | 主指標凍結、多様性・ロバスト性は副次リーダーボードで表示のみ（勝敗不変） | `SOLUTION_DIVERSITY.md` §7 #4 |
| 5 | 情報側（チーム別データビュー） | **今導入（ユーザー確定）**: 同一凍結データからの特徴量/行数/期間の差を配布。`build_release.py` 拡張で実装（シミュレータ不変） | `SOLUTION_DIVERSITY.md` §7 #5 |
| 6 | fee 設計 | 一律 33% のまま（fee 潜在化は不採用） | `COMPETITION_DIFFICULTY.md` §5 #1 |
| 7 | 異質性・ドリフト | 決定率側に occupation/region 異質性＋時間ドリフト（#46、`segment_drift`） | `COMPETITION_DIFFICULTY.md` §5 #4 |

### 6.2 実行フェーズ固有の未決点（確定）

| # | 項目 | **確定（ユーザー判断 2026-11-24）** |
| --- | --- | --- |
| 1 | チーム数 | **5 チーム**（データビュー A〜E と 1:1 対応） |
| 2 | Team C のセグメント指定 | `occupation ∈ {エンジニア, 営業}`（`team_config.json` で固定） |
| 3 | Team D の行数上限 | **150,000**（一様抽出・固定 seed） |
| 4 | 実日付（ラウンド締切 T0〜T0+6 週） | 開催時に確定（`RULES.md` §7 プレースホルダー） |
| 5 | 異なるモデルバックエンドの導入 | オプションのまま。既定は同一バックエンド |

## 参照

- [コンペ仕様](../../synthetic_funnel/docs/COMPETITION.md)
- [competition1 運営ルール](../../competitions/competition1/organizer/RULES.md)
- [competition1 凍結 config](../../competitions/competition1/organizer/frozen_config.json)
- [解法多様化の研究（#47）](../../docs/drafts/SOLUTION_DIVERSITY.md)
- [コンペ難易度分析（#45）](../../docs/drafts/COMPETITION_DIFFICULTY.md)
- [Spec Kit 導入の提案（テンプレート借用の根拠）](../../docs/drafts/SPECKIT_PROPOSAL.md)
- [配布パッケージ生成](../../competitions/competition1/organizer/build_release.py)
