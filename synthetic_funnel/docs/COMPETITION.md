# コンペ仕様（合成データ版）: 期待手数料の最大化

> ステータス: 確定版（issue #33、ユーザー確定事項 2026-11-18 反映）。仕様の契約は変わらず、issue #35 でリプレイ機能と評価器 CLI を実装済み（実装状況は §5.2 / §5.5 / §11 を参照）。後続の実装 issue はここを契約として読む。
> 前提タスク: issue #34（手数料データの追加）は実装済み。§4 の fee 列は定義済みで、手数料は確定的な観測属性として扱う。
> 並行タスク: issue #32（NG 観測列）は待たない。将来対応は §9 に記載する。

## 0. 目的とこの仕様の中核

コンペの目的は既存コンペと同じく**期待する手数料の最大化**である。参加者は合成市場の日次候補集合に対するランキング（または選択）を提出し、運営側はその方策のもとで市場が生んだ観測実績手数料を比較する。校正モデルは「使ってもよい解法」のひとつにすぎず、提出物に確率やスコアは含まれない（§6）。

### 0.1 既存コンペの限界と合成データによる一段階の前進

既存の商用ログでは、評価母集団が「過去に実際に紹介されたペア」に限られる。未紹介ペアはログに存在しないため、別の紹介方策を選んだときの候補集合の変化や、競合・活動終了・企業の審査能力の変化をオフラインで再現できない。既存プロトコルはこれを `evaluation_scope=logged_cohort_only_not_counterfactual_revenue` と記録している。重点抽出（stage サンプリング）と `inverse_sampling` は学習用の正例密度と損失の補正であり、この評価母集団の欠けを埋めるものではなかった（§10 の過去コンペの知見を参照）。

合成データでは事情が変わる。

- `candidates` は D 時点で在籍し紹介条件を満たす**未紹介ペアを含む完全な候補集合**である（[日次バッチ推論候補](DAILY_CANDIDATES.md)）。
- `candidates_latent` は同じキーに対して反実仮の `p_*_eventual` と潜在特徴を持つ。
- 市場はシミュレータなので、参加者のランキングを紹介方策として**市場を再実行（リプレイ）**できる。未紹介ペアが選ばれても実際に選考を進め、観測された `kettei_dt` と `fee_yen` が得られる。

したがって主指標は「ログに載ったペアの実績」ではなく「参加者の方策のもとで市場が生んだ観測実績手数料」になる。反実仮の期待値そのもの（`p × fee`）を主指標にするのではない。合成機構によって**観測実績の母集団そのものを方策依存に拡張した**点が、このコンペの中核である。

### 0.2 参照する既存資料

- 合成データ: [継続市場のシミュレーション](MARKET.md)、[出力データ仕様](DATA_FORMAT.md)、[日次バッチ推論候補](DAILY_CANDIDATES.md)、[紹介履歴の確率モデル](PROBABILITY_MODEL.md)
- 既存コンペのプロトコル: `product_prototype/docs/DATA_AND_EVALUATION.md`、`product_prototype/COMPETITION_LESSONS.md`、`product_prototype/docs/EXPERIMENTS.md`
- 実験記録の慣習: `calibration_pipeline/experiments/example_oubo`

## 1. 役割と公開範囲

| 役割 | 行うこと |
| --- | --- |
| 運営 | 市場生成と manifest の固定、候補集合の配布、リプレイの実行、評価器の実行、結果の記録 |
| 参加者 | 配布された観測データのみで日次ランキング／選択を構築し、提出ファイルを作る |

配布物と非公開物は次のとおりとする。

| 成果物 | 参加者 | 運営 | 備考 |
| --- | --- | --- | --- |
| `user` / `item` / `daily` | 配布（D_train まで） | 全期間 | 観測列のみ |
| `events` | 配布（D_train までの紹介） | 全期間 | `model_*_score` は観測スコアとして配布可。潜在由来の列は無い |
| `manifest.json` | 配布 | 保有 | seed・config・各ファイルの SHA-256 |
| `candidates`（評価日） | 配布 | 配布 | 完全候補集合。§4 の fee 列を含む |
| `candidates_latent` | **非配布** | 保有 | 反実仮 `p_*`、スキル由来潜在値、モチベーション |
| `user_latent` / `item_latent` / `events_latent` | **非配布** | 保有 | 潜在値そのもの |
| リプレイ結果・真値テーブル | 非配布 | 保有 | 評価時にのみ集計 |

`candidates_latent` と `user_latent` / `item_latent` / `events_latent` は**運営専用**であり、いかなる形式でも参加者へ渡さない。参加者が利用できるのは `candidates` の観測列と D_train までの観測履歴だけである。

## 2. タスク定義

各評価日 D について `candidates` の全行に順序を付け、運営側の枠割当ルールのもとで紹介されるペアから得られる手数料を最大化する。予測対象は確率ではなく**順序（rank）**である。

### 2.1 枠割当ルール（既存プロトコルを踏襲）

1. 同じ `(day_dt, user_id, item_id)` の重複は提出 rank が最良の1行だけを残す。同点はキーの昇順。
2. 一人一日につき上位 `max_per_user_per_day`（既定5）行を残す。ユーザ上限は日ごとにリセットし、partition をまたいで一日合計にかかる。
3. 残りを日ごとに並べ、総枠 `daily_budget_per_active_user × その日の対象 distinct user 数`（既定5）を適用する。絶対枠 `daily_budget` を併用でき、両方ある場合は小さい方を採る。partition 別の総枠も設定できる。
4. 選ばれた行がその日の紹介ペアになる。

評価器がこのルールを適用するため、参加者は枠を自分で適用する必要はない（§5.6）。

### 2.2 校正の位置づけ

校正（確率の較正）は主目的ではない。参加者はロジスティック回帰、GBDT、warmup スコアの再利用、ルールベースなど任意の手法で順序を作ってよい。`p_decision` などの確率列を提出してもよいが、採点には使わず診断用に限る（§6）。

## 3. データプロトコル

### 3.1 フィクスチャ生成と再現性

生成器は `synthetic_funnel` の市場生成のみ（`--run-dir` または `write_market_dataset`）。既定設定は [継続市場のシミュレーション](MARKET.md) の既定値に合わせる。

| 設定 | 既定値 |
| --- | --- |
| `market_start` | `2025-01-01T00:00:00+09:00` |
| `seed` | 71 |
| `days` | 1,825 |
| `warmup` | 365 |
| `seeker_arrivals` / `job_arrivals` | 50 / 50 |
| `application_days` / `posting_days` | 20 / 40 |
| `referrals` | 5 |
| `output_format` | sqlite（配布は parquet 可） |

`manifest.json` に config、generator version、各ファイルの SHA-256、`training_cutoff`、`warmup_end`、`observation_end_exclusive` が入る。運営は配布データの manifest ハッシュを data version として公表し、参加者が再現性を確認できるようにする。生成規則や依存環境が変わると同じ seed でも行が変わるため、data version はハッシュで管理する。

### 3.2 学習期間と評価期間（市場時間）

期間は `market_start` からの経過日数（day offset）で定義し、区間はすべて半開区間とする。

| 区間 | day offset | 内容 |
| --- | --- | --- |
| warmup | `[0, 365)` | ベースラインモデルの学習。この間の `model_*_score` は NULL |
| 参加者学習 | `[365, 730)` | スコア付き紹介履歴。参加者はここから学習データを作る |
| 評価 | `[730, 760)` | 30日分の日次 `candidates`。提出対象 |

評価対象日 D の候補は [日次バッチ推論候補](DAILY_CANDIDATES.md) の SQL レシピ（`:day = D`）で作る。キーは `(day_dt, user_id, item_id)` で、未紹介ペアなので `event_id` は存在しない。ラベルの判定期間は `H` 日とし、既定は3暦月、既存コンペ互換の120日も run config で選択できる。`D_eval_end + H <= observation_end_exclusive` を満たすこと。リプレイは評価終了後も `D_eval_end + H` まで続け、全選択行を成熟させる。

### 3.3 学習データ量の制約

データベースは配布するが、**全データを学習に使うことはできない**。実運用のインフラ制約を再現するための制約である。

| 項目 | 既定値 | 許容範囲 |
| --- | --- | --- |
| `max_training_rows` | 300,000 | 50,000〜500,000（run config に記録） |

- 上限は「学習に使う紹介行の総数」に適用する。分類器と金額モデルの行数を別々に記録し、合計が上限を超えないようにする。
- 一様抽出、重点抽出（stage）、月・市場の均等化、`inverse_sampling` による行重みなど、抽出戦略は参加者の裁量である。既存コンペの戦略要素がここで復活する。戦略、実際の取得件数、採択率を `input/config.toml` と `run_metadata.json` に記録する。
- 評価側の候補集合は全行・無重み（§5.3）。抽出は学習にのみ効く。

### 3.4 データ量の目安

流入 50/50 の設定では1日あたりの候補は数千〜数万行になる。運営は評価日の `candidates` を**間引かずに全行配布**する。完全候補集合を維持するためである。規模が大きすぎる場合は流入数や `days` を下げた別設定を作り、候補そのもののサンプリングは行わない。

## 4. 手数料の定義（issue #34 依存）

前提タスク #34 が `events` の決定行に対して次を追加する契約を、本仕様は参照する。

| 列 | 型 | 内容 |
| --- | --- | --- |
| `offered_salary` | REAL | 決定時に確定した提示給与。#34 では提示給与ロジックの再設計（ノイズ追加）は行わず、現行の `item.offered_salary` を使う |
| `fee_rate` | REAL | 手数料率。初期値 0.33 だが**列として保持**し、将来の率変更に備える |
| `fee_yen` | REAL | `offered_salary × fee_rate`。決定した event に対してのみ設定する |

参加者側には `candidates` と `candidates_latent` の両方に `fee_rate` と `fee_if_decided_yen = offered_salary × fee_rate` を観測列として含める。料率は全参加者共通の既知値なので、この列は潜在情報ではない。参加者は `fee_if_decided_yen` と自らの順序付けから期待手数料を計算できる。参加者が独自の料率や金額定義を提出に使うことは、比較を不可能にするため禁止する。

決定に至らなかった行の手数料は 0 ではなく「未決定」として扱う。選択ペアの決定実績とその `fee_yen` のみを手数料として合計し、金額不明の行を暗黙の 0 として勝敗を決めない。

## 5. 評価

### 5.1 二層の指標

| 層 | 指標名 | 真値 | 役割 |
| --- | --- | --- | --- |
| 主 | `macro_daily_replay_fee_yen` | リプレイで選ばれたペアの観測 `kettei_dt` と `fee_yen` | 順位付け・合否 |
| 副 | `macro_daily_logged_fee_yen` | ベース市場で実際に紹介されたペアの観測実績 | 既存プロトコル互換の診断 |
| 副 | `macro_daily_counterfactual_fee_yen` | `candidates_latent` の `p_decision_eventual × fee_if_decided_yen` | 反実仮の期待値の診断 |

主指標は反実仮の期待値そのものではなく、参加者の方策で市場を再実行して得た**観測実績**である。副指標は主指標を置き換えない。

### 5.2 リプレイの手順

1. D_train（offset 730）時点の市場状態（在籍 `user` / `item`、紹介履歴、乱数状態）を凍結したベーススナップショットを作る。
2. 評価日ごとに、参加者の提出 rank へ §2.1 の枠割当を適用し、その日の紹介ペアを決める。
3. 凍結状態から市場を再実行し、選んだペアだけを紹介する。同じ seed と同じ潜在パラメータ（モチベーション、各段階確率）を使い、乱数ストリームの消費順を固定する。
4. 各紹介について観測された `oubo_dt` / `shorui_tsuuka_dt` / `kettei_dt` と、決定行の `fee_yen` を記録する。未到達や観測打切りは負例ではなく打切りとして扱う。
5. `D_eval_end + H` まで実行し、全選択行が成熟したことを確認する。
6. 日次・partition ごとに手数料を合計し、macro 平均を主指標とする。

リプレイの実装は後続 issue の契約である。本仕様は入出力と不変条件（同じ seed・同じ提出なら同じ結果であること、候補キーの完全一致、枠ルールの適用）を定義する。

> 実装状況（issue #35）: `synthetic_funnel.market.replay_market` / `ReplayIntroductions` が完全なリプレイを提供する。D_train の凍結状態は「同じ config/seed で day 0 から再シミュレーション」することで再現し、巨大なスナップショットを保存しない（`make_snapshot` / `resume` は明示スナップショット用として残存）。RNG 規律: 方策は紹介の `rng.randrange` 抽選を置き換え、市場ストリームを消費しない。評価日以降の arrival はデータセットの `user` / `item` / `*_latent` 表から再構成した人口スケジュールを使うため、方策が紹介回数を変えても `user_id` / `item_id` とアクターの対応が崩れない。評価器は `python -m synthetic_funnel.market_evaluation evaluate` または `synthetic-competition evaluate`。

### 5.3 指標の計算

| 指標 | 定義 |
| --- | --- |
| `macro_daily_replay_fee_yen` | 各評価日の選択行の `fee_yen` 合計（決定のみ）を日平均し、partition を等重み平均 |
| `total_replay_fee_yen` | 評価期間全体のリプレイ手数料合計 |
| `replay_selected_positives` | 選択行のうち期間内に決定した件数 |
| `replay_positive_capture` | リプレイ市場で観測された決定のうち、選択行に残った割合 |
| `macro_daily_logged_fee_yen` | ベース市場で実際に紹介されたペアの手数料の日平均（既存互換） |
| `macro_daily_counterfactual_fee_yen` | 提出順位の上位行について `p_decision_eventual × fee_if_decided_yen` を日平均 |

`average_precision` / `roc_auc` / `log_loss` / `brier` / 信頼図は、参加者が確率列を任意提出した場合にのみ計算する診断値であり、合否には使わない。集計は評価日で macro 平均とし、複数 seed では平均と標準偏差を併記する。partition（市場や地域）がある場合は各 partition の日平均を等重みで平均する。

### 5.4 合否基準

- **有効性**: 提出が §6 のスキーマを満たすこと。不足キー・重複 rank・配布候補に無いペアはエラーとする。
- **性能**: 主指標が同一 data version のベースライン提出（§7）の主指標を上回ること。必要なら最小改善幅 `margin` を run config に設定する。
- **再現性**: seed を変えた複数 run で平均改善が正であることを推奨する。単一 seed の差を普遍的な優劣と呼ばない。
- **最終評価**: 検証期間で選んだ1提出に対して1回だけ実行する。最終結果を見てから候補を増やした場合は、新しい holdout を用意する。

### 5.5 フォールバック（条件付き契約）

リプレイ機能が後続実装で用意できない場合に限り、**評価対象は1日だけ**でよい。

- 評価日は offset 730 の1日に固定する。
- 統計的パワーを確保するため market config の seed を増幅する（推奨20シード以上、例 `71`〜`90`）。各 seed の manifest を記録し、集計は seed 平均±標準偏差とする。
- 流入数を増やすなどして候補数を確保してもよい。設定変更は新しい data version として扱う。
- `evaluation_scope` に `replay_unavailable_single_day_seed_amplified` を記録し、リプレイ版の主指標と直接比較しない。

> 実装状況（issue #35）: フォールバックも実装済み（`evaluate_fallback` / `synthetic-competition evaluate-fallback`）。シードごとの `(manifest, submission)` を JSON spec で受け取り、1 日だけ評価して主指標の平均±標準偏差を集計し、上記 `evaluation_scope` を記録する。推奨シード数は 20 以上（§11 #4）。完全リプレイが利用可能な場合はフォールバックを使わないこと。

### 5.6 評価器のインターフェース

評価器は運営側スクリプトとして実装し、参加者は実装しない。インターフェースは次のとおりとする。

```text
synthetic-competition evaluate \
  --manifest <配布データの manifest.json> \
  --truth <運営専用の真値・リプレイ結果> \
  --submission <提出ファイル> \
  --config <run config> \
  --output <出力ディレクトリ>
```

出力は `metrics.json`、`daily.csv`、`ranked.parquet`、`plots/`（日次手数料推移、累積手数料、capture 曲線。確率列が提出された場合のみ信頼図）とする。同じ入力から同じ出力が得られること（決定論的であること）を契約とする。

> 実装状況（issue #35）: 実装 CLI は `synthetic-competition evaluate --manifest <dataset dir> --submission <file> --output <dir> [--config <json>] [--candidates <file>] [--candidates-latent <file>]`（または `python -m synthetic_funnel.market_evaluation evaluate ...`）。出力は `metrics.json` / `daily.csv` / `ranked.parquet` / `summary.md`。`--config` で `H`（既定3暦月、`120_days` も選択可）、枠パラメータ、`partition_field` を上書きできる。`max_training_rows`（既定300,000）は参加者側の制約であり、運営の評価器では強制しない。フォールバックは `evaluate-fallback --spec <runs.json> --output <dir>`。

## 6. 提出形式

| 列 | 型 | 必須 | 内容 |
| --- | --- | --- | --- |
| `day_dt` | TEXT | ○ | 評価日。manifest と同じオフセットの ISO 8601 |
| `user_id` | TEXT | ○ | 求職者ID |
| `item_id` | TEXT | ○ | 求人ID |
| `rank` | INTEGER | ○ | その日内の1始まりの順序。同一日内で一意 |
| `selected` | INTEGER | × | 参考値。評価器が枠割当を再計算するため信頼しない |
| `p_decision` / `p_application` / `p_screening` | REAL | × | 診断用。採点しない |
| `expected_fee_yen` | REAL | × | 診断用。採点しない |

- ファイル名は `submission.parquet`（正式）または `submission.csv`。CSV の空欄は NULL に相当する。
- キーは `(day_dt, user_id, item_id)` で、提出行は配布 `candidates` の部分集合でよい。未提出行は順位が付かない行として扱われ、枠割当では選ばれない。
- 提出には `submission_metadata.json` を添付し、seed、実行環境（Python バージョンと主要パッケージ）、コードの git commit、config ハッシュ、data version（manifest ハッシュ）、author、`generated_at` を記録する。

## 7. ベースライン

ベースラインは、市場内 warmup で学習したスコア（[継続市場のシミュレーション](MARKET.md) の「市場内ウォームアップ学習」を参照）を評価日の `candidates` へ適用した順序である。

- **生成**: 市場開始から `warmup` 日（既定365）の紹介履歴で応募・書類通過・決定の3モデルを学習する。学習済みモデルは運営が starter kit として配布し、全参加者が同じものを使える。
- **順序付け**: 既定では `p_decision_baseline × fee_if_decided_yen` の降順。確率は `[1e-9, 1-1e-9]` に clip する。既存コンペと同じ `p/(1-p) × fee` のオッズ版も config で比較できる。
- **意味**: 「ウォームアップ学習済みスコアを超えたか」が最初の到達点である。主指標（リプレイ観測実績手数料）でベースラインを上回ることが、抽出戦略や校正などの工夫が実益に結びついたことを示す。単一 seed の差は診断値として扱う。

## 8. 実験記録の枠組み

`calibration_pipeline/experiments/example_oubo` の慣習（`<name>/<stage>/<run_id>/{input,output,run_metadata.json}`、run_id は `yyyyMMdd_HHmmss`、入力ファイルの SHA-256 を記録）に従う。

```text
experiments/
  _template/                         # コミット済みテンプレート
    input/config.toml
    input/README.md
    output/.gitkeep
    output/summary.md                # 仮説・結果・考察のひな形
    run_metadata.json
  <experiment_name>/
    train/<run_id>/
    select/<run_id>/
    evaluate/<run_id>/
      input/
        config.toml
        solution/                    # 参加者解法（コードと学習手順）
        submission.parquet           # evaluate のみ。Git 管理外
      output/
        metrics.json
        daily.csv
        plots/
        summary.md
      run_metadata.json
```

`run_metadata.json` のフィールドは次のとおりとする。

| フィールド | 内容 |
| --- | --- |
| `experiment_name`, `stage`, `run_id` | `stage` は `train` / `select` / `evaluate` |
| `status`, `started_at`, `finished_at` | 実行状態とタイムスタンプ（ISO 8601、オフセット付き） |
| `author`, `hypothesis`, `conclusion` | 記録者、仮説、結論。`conclusion` は `output/summary.md` のひな形から始める |
| `data_version` | 配布データ manifest の SHA-256 と generator version |
| `seeds` | market seed、participant seed、sql seed など |
| `config` | run config 全体（`H`、枠、`fee_rate`、`max_training_rows`、抽出戦略、順序付け） |
| `input_sha256` | `input/` 内の各ファイルの SHA-256 |
| `submission_sha256`, `baseline_run_id` | evaluate の提出ハッシュと比較対象 run |
| `evaluator_version`, `git_commit` | 評価器の版とコードの commit |
| `environment` | Python バージョンと主要パッケージのバージョン |
| `selection_metric`, `metrics_path` | 選択指標と `output/metrics.json` への参照 |

運用規則は次のとおり。

- 検証は複数 run で比較し、最終評価は選んだ1提出に対して1回だけ実行する。
- data version・config・コード・実行環境が揃わない run は同じ出力に継ぎ足さず、新しい run として記録する。
- 失敗した run も含めて `run_metadata.json` を残し、完了前のファイルを完成扱いしない。

## 9. 将来対応: issue #32 の観測列

issue #32 は `events` に `status` / `outcome_dt` / `auto` を追加する（確定版設計は `synthetic_funnel/docs/drafts/issue-32-observation-design.md`）。本プロトコルは変更しない。

- 候補キー `(day_dt, user_id, item_id)`、完全候補集合、提出 rank、枠割当、リプレイの契約は不変である。
- 観測到達と打切りの扱いが精緻化される。NG・辞退・取消が観測され、右打切りと確定負例を区別できるようになる。`kettei_dt` は「採用のみ」という意味を保ち、fee は決定行にのみ付く。
- 日次スナップショットの `other_applications` / `competing_candidates` が「上限近似」から正確な再構成へ変わる（[日次バッチ推論候補](DAILY_CANDIDATES.md) の記述が更新される）。候補の列順とキーは変わらない。
- `status` / `outcome_dt` / `auto` は紹介後に確定する情報なので**学習特徴量として使用禁止**とする。参加者へ配布する `events` に列が増えても、これを特徴量に使った run は無効とする。
- 評価の真値（観測実績 fee）と副指標の定義は不変である。データ version が変わるため、manifest ハッシュと `run_metadata.json` の `data_version` を更新する。

## 10. 過去コンペの知見との対応

- 重点抽出は、約4,091万行に入社2,965件（約0.007%）という希少性下で、30万行の一様抽出では期待正例が約22件しか得られないため、決定を全件残して負例枠を通過のみ／応募のみ／未応募へ配分する手法だった。`inverse_sampling` は採択率 `q` の逆数 `1/q` を損失に掛け、人為的に変えた学習分布を母集団へ戻そうとした。
- ただし抽出後の `probability` は母集団の較正済み確率ではない。また `q` はログの紹介方策による選択確率ではなく、未紹介ペアはログに無いため、方策による選択そのものは補正できなかった。
- 合成データでは `candidates` が完全候補集合なので、**評価母集団を偏らせる目的**での重点抽出も、その逆確率補正も不要である。運営側の評価は全候補・無重みで行う。一方、学習では希少な決定を効率よく集める必要は残るため、抽出戦略は参加者の裁量とする（§3.3）。
- 提出は rank のみであり確率の解釈は採点に影響しない。したがって古い「probability の解釈」に関する注意は、学習の設計 freedoms としては残るが、評価の契約には及ばない。

## 11. 未決点（推奨付き）

主要な設計は確定済みである。実装 issue で確認する項目のみ残す。

| # | 項目 | 推奨 |
| --- | --- | --- |
| 1 | リプレイ機能の実装 issue とフォールバック発動の判断 | **決定済み**: 完全リプレイを実装（issue #35）。フォールバックも §5.5 に従って常時利用可能。 |
| 2 | `H` の既定（3暦月か120日か） | 3暦月を既定とし、既存互換の120日は config オプション |
| 3 | `max_training_rows` の既定 | 300,000（50,000〜500,000 を許容） |
| 4 | フォールバック時のシード数 | 20以上 |
| 5 | ベースラインスコアの配布方法 | starter kit としてモデル成果物を配布し、全参加者が同じものを使う |

## 参照

- [継続市場のシミュレーション](MARKET.md)
- [出力データ仕様](DATA_FORMAT.md)
- [日次バッチ推論候補](DAILY_CANDIDATES.md)
- [紹介履歴の確率モデル](PROBABILITY_MODEL.md)
