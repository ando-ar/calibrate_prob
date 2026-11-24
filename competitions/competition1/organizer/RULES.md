# competition1 運営ルール（確定）

> 本ファイルは competition1 の運営側確定ルールである。仕様の正本は
> `synthetic_funnel/docs/COMPETITION.md`、市場密度の根拠は
> `synthetic_funnel/docs/MARKET_DENSITY.md`。参加者向け案内は
> [`../participants/README.md`](../participants/README.md)、配布パッケージの生成は
> [`build_release.py`](build_release.py) が担う。
>
> 凍結日: 2026-11-19（issue #42）。§11 の未決点 5 件はすべて下記のとおり確定した。

## 1. 凍結された本番 config

`frozen_config.json` が唯一の正である。概要は次のとおり。

| 項目 | 確定値 | 根拠 |
| --- | --- | --- |
| `market_start` | `2025-01-01T00:00:00+09:00` | 仕様 §3.1 |
| 流入 `seeker_arrivals` / `job_arrivals` | **200 / 200** | density survey: cap が 98%+ 飽和、30日窓 1 seed あたり決定 ~9-10 件 |
| `days` | **280** | warmup 90 + 学習 60 + 評価 30 + H(~90) を余裕で満たす（180 では不可） |
| `warmup` | **90** | 200/200 では 90 日で ~1.7M 紹介・3 段階モデル学習に十分 |
| 参加者学習期間 | `[90, 150)` | スコア付き紹介履歴 60 日（定常状態） |
| 評価期間 | `[150, 180)`（**30 日・複数日**） | リプレイ実装済みのため単日フォールバックは使わない |
| 評価 seed | **71, 72, 73, 74, 75（5 本）** | 集計決定数 ~45-50 件（m ≳ 30） |
| `H` | **3 暦月**（120 日は config オプション） | §11 #2 |
| `application_days` / `posting_days` / `referrals` | 20 / 40 / 5 | 仕様既定 |
| `segment_drift` | 職種・地域のセグメント効果 `occupation_effects` / `region_effects`（各8値・和0）と時間ドリフト `drift_amplitude = 0.5` | issue #46（決定率の穏やかな異質性・非定常性） |
| `output_format` | sqlite（配布は sqlite + parquet） | 仕様 §3.1 |

> **issue #46 による config 更新**: 上記 `segment_drift` を `frozen_config.json` に追加した。決定率の生成機構が変わるため、**データバージョン（manifest SHA-256）と凍結ハッシュは以前の値から変わる**。competition1 の公開前には `build_release.py` による再生成（`organizer/metrics/`・`data_versions.json`・`baseline_aggregate.json` の更新）が必須である。手数料（fee）は一律 33% のままで変更していない。

### 1.1 配置（すべて半開区間）

```text
[0, 90)         warmup … ベースラインモデル学習。model_*_score は NULL
[90, 150)       参加者学習 … スコア付き紹介履歴。参加者はここから学習データを作る
[150, 180)      評価 … 30 日分の日次 candidates。提出対象
[180, ~270)     H = 3 暦月の成熟期間（リプレイのみ）
[280, ∞)        観測終了（observation_end_exclusive）。D_eval_end + H <= 280 を満たす
```

### 1.2 枠割当ルール（§2.1 踏襲、評価器が適用）

1. `(day_dt, user_id, item_id)` の重複は rank 最良の 1 行を残す（同点はキー昇順）。
2. 一人一日 `max_per_user_per_day = 5` 行まで。日ごとにリセットし partition をまたぐ（本コンペは partition なし）。
3. 総枠 `daily_budget_per_active_user = 5 × その日の distinct user 数`。`daily_budget = null`（絶対枠なし）。
4. 選ばれた行がその日の紹介ペアになる。参加者は枠を自分で適用する必要はない。

## 2. データバージョン（manifest ハッシュ）の記録と凍結手順

1. 各 seed の生成は `synthetic_funnel` の市場生成（`write_market_dataset` / `synthetic-funnel --config ... --seed <s>`）だけを使う。`manifest.json` には config・seed・`generator_version`・`training_cutoff`・`warmup_end`・`observation_end_exclusive`・各ファイル SHA-256 が入り、実行時計測値（生成時間・タイムスタンプ）を含まないため、同一 config/seed/依存環境ならバイト単位で再現する（issue #38）。
2. **data version = 配布 `manifest.json` 全体の SHA-256**。参加者配布物の `manifest.json` は運営側 `manifest.json` のバイト同一コピーなので、運営と参加者の data version は一致する。
3. 凍結手順: `build_release.py` が seed ごとに `manifest.json` を生成し、その SHA-256 を
   `organizer/metrics/` と `experiments/competition1/` の `run_metadata.json`（`data_version.manifest_sha256`）へ記録する。ハッシュは下記コマンドで誰でも再計算できる。

```powershell
cd <repo>/competitions/competition1/organizer
uv run --project ../../../synthetic_funnel python build_release.py --seeds 71,72,73,74,75 --jobs 2
```

4. **運用上の制約**（空き約 17GB）: 5 seed の全データを同時保持しない。seed ごとに
   「生成 → participant パッケージ化 → organizer 側 truth 保持 → 評価器実行 →
   metrics/manifest 保存 → DB/parquet 削除」を逐次実行する。`--jobs` は 2-3 に制限する。
   `metrics.json`・manifest ハッシュ・`summary.md` は削除せず `organizer/` と
   `experiments/` に残す（再現性の担保）。

## 3. 評価プロトコル

### 3.1 主指標と副指標（§5.1）

| 層 | 指標 | 真値 | 役割 |
| --- | --- | --- | --- |
| 主 | `macro_daily_replay_fee_yen` | リプレイで選ばれたペアの観測 `kettei_dt` と `fee_yen` | 順位付け・合否 |
| 副 | `macro_daily_logged_fee_yen` | ベース市場で実際に紹介されたペアの観測実績 | 既存プロトコル互換診断 |
| 副 | `macro_daily_counterfactual_fee_yen` | `candidates_latent` の `p_decision_eventual × fee_if_decided_yen` | 反実仮の期待値診断 |

決定に至らなかった行は「未決定」として扱い、0 として合計しない（§4）。確率列
（`p_*` / `expected_fee_yen`）は採点に使わない診断値である。

### 3.2 リプレイ手順（§5.2）

1. D_train（offset 150）時点の市場状態を凍結する（`synthetic-competition freeze` で
   `d_train_market_state.pkl` を運営専用に保存。同一 config/seed の day 0 からの再シミュレーションと
   ビット同一）。
2. 評価日ごとに §2.1 の枠割当を適用し、その日の紹介ペアを決める。
3. 凍結状態から市場を再実行し、選んだペアだけを紹介する（`--population-dir` に運営側
   `user_latent` / `item_latent` を指定。省略時は `organizer/` を自動検出）。
4. 各紹介の `oubo_dt` / `shorui_tsuuka_dt` / `kettei_dt` と決定行の `fee_yen` を記録。
   未到達・観測打切りは負例ではなく打切りとして扱う。
5. `D_eval_end + H`（offset ~270）まで実行し、全選択行を成熟させる。
6. 日次合計を macro 平均（partition なしのため日平均）し、主指標とする。

評価器 CLI（運営専用）:

```text
synthetic-competition evaluate --manifest <organizer dir> --submission <submission.parquet> \
  --candidates-latent <organizer dir> --population-dir <organizer dir> --output <out dir>
```

出力は `metrics.json` / `daily.csv` / `ranked.parquet` / `summary.md`。同じ入力から
同じ出力が得られる（決定論的）。

### 3.3 seed 集計

複数 seed の集計は **平均 ± 標準偏差**（`evaluate_fallback` と同様の `mean ± std(ddof=0)`）で
記録し、`macro_daily_replay_fee_yen_per_seed` を併記する。単一 seed の差を普遍的な優劣と
呼ばない（§5.4）。

## 4. 提出ルール

### 4.1 スキーマ（§6）

| 列 | 型 | 必須 | 内容 |
| --- | --- | --- | --- |
| `day_dt` | TEXT | ○ | 評価日（manifest と同じオフセットの ISO 8601） |
| `user_id` | TEXT | ○ | 求職者 ID |
| `item_id` | TEXT | ○ | 求人 ID |
| `rank` | INTEGER | ○ | その日内の 1 始まり順序。同一日内で一意 |
| `selected` | INTEGER | × | 参考値。評価器が枠割当を再計算するため信頼しない |
| `p_decision` / `p_application` / `p_screening` | REAL | × | 診断用。採点しない |
| `expected_fee_yen` | REAL | × | 診断用。採点しない |

- ファイル名は `submission.parquet`（正式）または `submission.csv`。CSV の空欄は NULL 相当。
- キーは `(day_dt, user_id, item_id)`。提出行は配布 `candidates` の部分集合でよい
  （未提出行は順位なし＝選ばれない）。
- `submission_metadata.json` を添付する（seed、Python/主要パッケージ版、git commit、
  config ハッシュ、data version、author、`generated_at`）。

### 4.2 決定論・同点処理

- 提出は決定論的であること。同じ入力・コード・環境で同一バイトの提出が再現できることを要求する。
- 同点処理（評価器が枠割当で適用）: 重複キーは rank 最良、同 rank はキー `(user_id, item_id)`
  昇順。提出時の順序付けは参加者側で同点をキー昇順にすること。

### 4.3 リーダーボード形式

```text
rank | author | macro_daily_replay_fee_yen (mean±std over seeds) | total_replay_fee_yen
     | replay_selected_positives | macro_daily_counterfactual_fee_yen | data_version | submit_at
```

検証期間のボードは参考値、最終評価は選んだ 1 提出に対して 1 回だけ実行する。ベースライン
（§7）を上回った提出のみ有効（必要なら `margin` を config に設定）。

## 5. 参加者の制約

### 5.1 学習行数上限

- `max_training_rows = 300,000`（§3.3）。分類器と金額モデルの行数を別々に記録し、
  **合計**が上限を超えないこと。
- 抽出戦略（一様・重点（stage）・`inverse_sampling` 等）は参加者の裁量。戦略・実際の
  取得件数・採択率を `input/config.toml` と `run_metadata.json` に記録する。

### 5.2 遵守形式（自己申告 + コード確認）

- 自己申告: `run_metadata.json` に抽出戦略・取得件数・採択率を記録。
- 運営確認: 上位入賞者について提出コードのレビューを行い、学習行数上限・禁止事項（潜在
  データ不使用・§9 の学習禁止列不使用）を確認する。

## 6. 禁止事項（潜在データへのアクセス）

`user_latent` / `item_latent` / `events_latent` / `candidates_latent` は**運営専用**であり、
いかなる形式でも参加者へ配布しない（§1）。参加者が利用できるのは `candidates` の観測列と
D_train までの観測履歴だけである。参加者配布パッケージは `build_release.py` が潜在テーブルを
除去し、除去済みであることをアサートする（§「配布物」参照）。

## 7. ラウンド・締切（プレースホルダー）

> 実日付はコンペ開催時に確定する。以下は構造のプレースホルダー。

| ラウンド | 内容 | 締切 |
| --- | --- | --- |
| 配布 | participant パッケージ公開（data version を同時公表） | T0 |
| 検証 | 参加者が検証提出・スコア確認（複数回可） | T0 + 2 週間 |
| 最終提出 | 選んだ 1 提出を確定 | T0 + 4 週間 |
| 最終評価 | 運営が holdout で 1 回だけ評価 | T0 + 5 週間 |
| 結果公表 | リーダーボード確定 | T0 + 6 週間 |

## 8. 配布物と非配布物

| 成果物 | 参加者 | 運営 | 備考 |
| --- | --- | --- | --- |
| `user` / `item` / `daily` | 配布（D_train まで） | 全期間 | 観測列のみ |
| `events` | 配布（D_train までの紹介） | 全期間 | `model_*_score` は観測スコア。潜在由来列なし |
| `manifest.json` | 配布 | 保有 | seed・config・各ファイル SHA-256 |
| `candidates`（評価日） | 配布 | 配布 | 完全候補集合。`fee_if_decided_yen` を含む |
| `candidates_latent` | **非配布** | 保有 | 反実仮 `p_*`・潜在値 |
| `user_latent` / `item_latent` / `events_latent` | **非配布** | 保有 | 潜在値そのもの |
| リプレイ結果・真値 | 非配布 | 保有 | 評価時にのみ集計 |
