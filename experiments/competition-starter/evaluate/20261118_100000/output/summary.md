# competition-starter / evaluate / 20261118_100000

## 仮説

市場内 warmup（365日）で学習した3段階スコアを評価日候補へ適用し、
`p_decision_baseline × fee_if_decided_yen` の降順で並べた順序が
コンペのリファレンスベースラインになる（COMPETITION.md §7）。
参加者の工夫（抽出戦略・校正・別モデル）はこの主指標を上回るべきである。

## 設定

- 評価器: `synthetic-competition evaluate` / `evaluate-fallback`（issue #35 実装、
  `synthetic_funnel 0.12.0`、`SCHEMA_VERSION=1`）。evaluator CLI が利用可能になったため
   trial-run を実施した。
- data version（実際に再生成された manifest ハッシュ）:
  seed71 = `355ae51c71fc0ad6c6750d859210874a8b13802cb65bfe94e64431de3b8c6282`、
  seed72 = `1947438f8a7415d978b13e46d5148e4951e529cbe234491ba5c869dcab1ea718`。
  ドキュメント記載の `3f55683a09fcda57486d89a8d6534bb21d992a7586d162673539f05353123b75`
  とは一致しない（下記「発見したバグ」1 を参照）。
- generator version: `0.12.0`。コード commit: `f81b2ba91555fd5673f458c3e13cc48fbc260eb0`。
- seed: market=71 / 72、participant=0、sql=0。
- データセット: proof（days=730, arrivals=20/20, warmup=365, 評価オフセット 400/401/402）。
  本番 config は days=1825, arrivals=50/50, 評価オフセット `[730,760)`（未生成）。
- 学習行数と抽出戦略: warmup 紹介 322,807 行（seed71）/ 319,776 行（seed72）から
  3段階を balanced 再学習。各モデル 616/68/18 行（正例 308/34/9, seed71）。
  潜在列・将来の結果は不使用。`max_training_rows=300000` を記録（本 proof では未達）。
- 枠・順序付け・手数料率: 一人一日5件、アクティブユーザーあたり日次5件、
  `stage_product_times_fee`、`fee_rate=0.33`、確率は `[1e-9, 1-1e-9]` に clip。
- 比較対象ベースライン run: なし（この run 自体がベースライン）。
- **評価範囲（evaluation scope）**: `primary = replay_policy_dependent_observed_fee`
  （方策依存のリプレイ観測手数料、主指標）、`logged = logged_cohort_only_not_counterfactual_revenue`、
  `counterfactual = organizer_only_candidates_latent_not_for_ranking`、
  `undecided_policy = undecided_rows_excluded_not_zero`、`fallback = false`。
  期間は評価日 3 日（2026-02-05/06/07）、`H = 3_calendar_months`、
  `horizon_end = 2026-05-07T00:00:00+09:00`、`observation_end_exclusive = 2027-01-01T00:00:00+09:00`。
  反実仮指標は運営専用 `candidates_latent` を `--candidates-latent` で渡して算出した
  （参加者には配布しない列）。

## 結果

提出: `submission.parquet` 1,237 行（seed71: 390 / 401 / 446）、1,158 行（seed72: 405 / 382 / 371）。

### 主指標と副指標（seed71、metrics.json の値をそのまま記載）

| 指標 | 値 |
| --- | ---: |
| `macro_daily_replay_fee_yen`（主） | `0.0` |
| `total_replay_fee_yen` | `0.0` |
| `replay_selected_positives` | `0` |
| `replay_positive_capture` | `null` |
| `macro_daily_logged_fee_yen` | `0.0` |
| `macro_daily_counterfactual_fee_yen` | `4898.550498188357` |

日次内訳（`daily.csv`）:

| day_dt | candidates | selected | replay_fee_yen | logged_fee_yen | counterfactual_fee_yen |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-02-05T00:00:00+09:00 | 390 | 237 | 0 | 0 | 4650.9337820873925 |
| 2026-02-06T00:00:00+09:00 | 401 | 246 | 0 | 0 | 4768.58849076996 |
| 2026-02-07T00:00:00+09:00 | 446 | 269 | 0 | 0 | 5276.129221707718 |

枠割当監査: 選択 237 / 246 / 269 = 752 行。
リプレイでスキップ 503 行（`no_seeker` 200 / `seen` 303）、実紹介 249 行。

`metrics.json` は `output/metrics.json` にそのまま保存した（完全な JSON は同ファイルを参照）。
評価器が生成した要約は `output/evaluator_summary_seed71.md` に保存した。

### seed72（再現性のための第2シード）

| 指標 | 値 |
| --- | ---: |
| `macro_daily_replay_fee_yen`（主） | `0.0` |
| `total_replay_fee_yen` | `0.0` |
| `replay_selected_positives` | `0` |
| `replay_positive_capture` | `null` |
| `macro_daily_logged_fee_yen` | `0.0` |
| `macro_daily_counterfactual_fee_yen` | `4906.279502470227` |

日次内訳: counterfactual = 5083.553552212294 / 4984.3611927819275 / 4650.923762416459。
選択 308 / 296 / 301 = 905 行、スキップ 560 行（`no_seeker` 136 / `seen` 424）、実紹介 345 行。
完全な JSON は `output/seed72/metrics.json`。

### evaluate-fallback（2 シード、単日 offset 400）

```json
{
  "schema_version": 1,
  "metrics": {
    "macro_daily_replay_fee_yen": 0.0,
    "replay_fee_yen_std": 0.0,
    "macro_daily_replay_fee_yen_per_seed": [0.0, 0.0]
  },
  "evaluation_scope": {
    "primary": "replay_unavailable_single_day_seed_amplified",
    "logged": "logged_cohort_only_not_counterfactual_revenue",
    "counterfactual": "organizer_only_candidates_latent_not_for_ranking",
    "undecided_policy": "undecided_rows_excluded_not_zero",
    "fallback": true
  },
  "evaluation": { "seeds": 2, "recommended_min_seeds": 20, "single_day": true }
}
```

`evaluation_scope` は `replay_unavailable_single_day_seed_amplified` であり、
完全リプレイの主指標とは直接比較しない。シード数 2 は推奨 20 を満たさず、
CLI 実行確認のみを目的とした。完全な JSON は `output/fallback/metrics.json`。

## 決定密度の観察（この trial の主要目的）

| 項目 | seed71 | seed72 |
| --- | ---: | ---: |
| 評価ウィンドウのベース市場紹介件数 | 910 / 916 / 903 = 2,729 | 866 / 921 / 843 = 2,630 |
| 評価ウィンドウのベース市場 `kettei_dt` 件数 | **0**（3日すべて） | **0**（3日すべて） |
| 評価ウィンドウのベース市場観測手数料合計 | 0 JPY | 0 JPY |
| リプレイで選択されたペア数 | 752 | 905 |
| リプレイで実際に紹介されたペア数 | 249 | 345 |
| 評価ウィンドウのリプレイ市場 `kettei_dt` 件数（`replay_selected_positives`） | **0** | **0** |
| リプレイ観測手数料合計 | 0 JPY | 0 JPY |
| 0 決定の評価日 | **3 / 3（100%）** | **3 / 3（100%）** |
| 反実仮手数料（日平均） | 4,898.55 JPY | 4,906.28 JPY |
| 反実仮 − リプレイ観測のギャップ | 4,898.55 JPY/日（全額が未観測） | 4,906.28 JPY/日（全額が未観測） |

補足: 市場全体では seed71 で 661,168 件の `events` のうち決定は 14 件のみ
（手数料合計 2,914.98 JPY）、seed72 でも 659,792 件中 14 件（3,188.05 JPY）。
評価ウィンドウ前にすべての決定が集中し、評価日には 1 件も無い。
`replay_positive_capture` が `null` なのは分母 `base_decided = 0` のためである。

結論: proof 設定（20/20 arrivals, 3日）では**手数料の観測結果がゼロであり、
提出顺序によって主指標が変わらない**。したがってこの proof データでは
「期待手数料の最大化」コンペとして参加者を順位付けできない。
本番設定（50/50 × 1,825日、評価 30 日）でも決定は 14 件/66 万件規模のオーダーであり、
評価ウィンドウ 30 日に十分な決定が得られるか（20 シード以上での増幅が必要か）を
事前に検証する必要がある。反実仮指標は約 4.9k JPY/日と正の値を示すため、
観測ゼロは「候補が無い」のではなく「決定確率が低すぎて実現されない」ことが原因。

## seed 間分散

- 主指標 `macro_daily_replay_fee_yen`: 0.0（seed71） / 0.0（seed72） → 差 0、標準偏差 0。
  これは「分散が小さい」のではなく、両方ともゼロで情報が無い縮退した結果である。
- 副指標（診断用）`macro_daily_counterfactual_fee_yen`: 4,898.550498188357 /
  4,906.279502470227 → 差 +7.729004281870 JPY/日（約 +0.16%）。
- 提出行数 1,237 / 1,158、選択行数 752 / 905 とデザイン母集団自体は seed で変動するが、
  観測手数料は 0 のまま。単一 seed の差を普遍的な優劣と呼ばないこと（§5.4）。
- evaluate-fallback の 2 シード平均 ± 標準偏差も 0.0 ± 0.0（per-seed [0.0, 0.0]）。

## 所要時間（コーディネータ要件）

外部計測は `time` シェル組み込み（秒）。データ生成のみ生成器内部の
`manifest.generation_seconds`（simulate + warmup 学習 + 書き出しの合計）を使用した。

| フェーズ | seed71 | seed72 |
| --- | ---: | ---: |
| (1) `generate_dataset.py`（内部 `generation_seconds`） | 88.62 s | 88.79 s |
| (2) `build_baseline.py`（外部 wall time） | 6.74 s | 6.07 s |
| (3) `synthetic-competition evaluate`（外部 wall time） | 33.00 s | 30.49 s |
| (4) `synthetic-competition evaluate-fallback`（2 シード、外部 wall time） | 61.33 s（2 シード合計） | 同じく |
| 参考: 運営用 `candidates_latent.parquet` 生成（最適化経路） | 0.78 s | 0.64 s |

生成の内訳（warmup 学習と書き出しのseparate計測）は生成器が個別に計測していないため
`generation_seconds` の合計値のみ。`build_baseline.py` も候補抽出・スコア計算・
提出ファイル作成の内訳は未計測（`uv run` の起動オーバヘッドを含む wall time）。

## 発見したバグ（修正せず報告）

1. **data version が再現しない**: `market_export.py:202` が `generation_seconds`
   （`time.perf_counter()` の実測値）を `manifest.json` に書き込むため、同じ
   config/seed でも再生成するたびに manifest の SHA-256 が変わる。ドキュメントの
   `3f55683a...` に対し実測は `355ae51c...`（seed71）/ `1947438f...`（seed72）。
   COMPETITION.md §3.1 の「ハッシュで data version を管理」が成立しない。
2. **主指標が縮退する**: proof 設定の評価ウィンドウには `kettei_dt` が 0 件で、
   どんな提出でも `macro_daily_replay_fee_yen = 0` になる。ランキング不可能。
   `replay_positive_capture` も分母 0 で `null` となる（クラッシュはしない）。
3. **運営用 DB に候補レシピ用インデックスが無い**: `generate_dataset.py` は
   participant 側だけに `events(user_id, oubo_dt, ...)` 等のインデックスを作成する。
   運営 DB に対して `candidate_rows(..., latent=True)` を直接呼ぶと
   `NOT EXISTS` の相関サブクエリがフルスキャンになり 17 分以上 CPU を消費した
   （同じ結果を候補 parquet + 潜在モチベーションから 0.78 s で生成可能）。
4. **リプレイで選択ペアの大部分がスキップされる**: seed71 で 752 選択中 503（67%）、
   seed72 で 905 中 560（62%）が `no_seeker` / `seen` で紹介されない。
   候補レシピ（`departure_dt` 基準）とリプレイの `live_s` / `seen` の意味が一致しない
   可能性があり、実効的な紹介数をさらに減らしている（要調査、修正はしていない）。

## 再現コマンド

```sh
uv run --project synthetic_funnel python starter_kit/generate_dataset.py --force
uv run --project synthetic_funnel python starter_kit/build_baseline.py
uv run --project synthetic_funnel synthetic-competition evaluate \
  --manifest starter_kit/data/proof/participant \
  --submission starter_kit/data/proof/participant/submission.parquet \
  --candidates starter_kit/data/proof/participant/candidates_eval.parquet \
  --candidates-latent starter_kit/data/proof/organiser/candidates_latent.parquet \
  --output experiments/competition-starter/evaluate/20261118_100000/output
```

## 次にやること

1. 本番データセット（days=1825, 50/50）で同じベースラインを再生成し、
   data version を更新した別 run として記録する。評価ウィンドウ 30 日での
   決定件数を先に見積もり、必要なら §5.5 の 20 シード増幅を適用する。
2. バグ 1（manifest の `generation_seconds`）とバグ 3（運営 DB のインデックス）を
   別 issue で修正する。本 run では src/ を変更していない。
3. 参加者解法が主指標を変えられることを確認できるまで、proof をランキング用途に使わない。
