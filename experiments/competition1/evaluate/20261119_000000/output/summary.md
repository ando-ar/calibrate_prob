# competition1 / evaluate / 20261119_000000（ベースライン）

## 仮説

warmup（`[0, 90)`）の紹介履歴だけで学習した 3 段階ロジスティックモデルの積
`p_decision_baseline = clip(p_apply) × clip(p_screen) × clip(p_decision)` に
`fee_if_decided_yen` を掛けた順序が基準。参加者解法は `macro_daily_replay_fee_yen` で
これを上回るべき。

## 設定

- data version（manifest ハッシュ）: `80e41d134d7bd8c48790655fd1a578af4f4bdc2d70f4fe2a4e77428c9f5678c4`
  （**proof config** 50/50 × 280日 × seed 71。本番は 200/200 × 5 seed、`frozen_config.json` 参照）
- seed: market=71
- 学習行数と抽出戦略: warmup 均衡リフィットのみ（312,878 warmup events、潜在列不使用）
- 枠・順序付け・手数料率: 5件/人/日・5件/アクティブユーザー/日・`stage_product_times_fee`・0.33
- 評価: 30 日（offset `[150, 180)`）フルリプレイ。H = 3 暦月

## 結果

- 主指標 `macro_daily_replay_fee_yen`: **159.997**（決定 3 件、非縮退）
- `total_replay_fee_yen`: 479.99 / `replay_selected_positives`: 3 / `replay_positive_capture`: 3.0
- 副指標 `macro_daily_logged_fee_yen`: 230.99 / `macro_daily_counterfactual_fee_yen`: 77,501.10
- 生成 events: 1,105,674 / warmup events: 312,878 / submission rows: 673,550
- ウォームアップ学習: `trained`

## 考察

proof config（50/50 × 1 seed）でも 30 日窓で決定 3 件が観測され、主指標が非縮退
（既存 competition-starter の 20/20 × 3 日は 0 件で縮退していた）。本番 200/200 では
1 seed あたり決定 ~9-10 件、5 seed 集計で ~45-50 件となり、安定した順位付けが可能
（`docs/MARKET_DENSITY.md`）。

## 次にやること

- 本番（200/200 × 5 seed、seed 71-75）のベースラインを `build_release.py --seeds 71,72,73,74,75`
  で生成し、新しい run_id で記録する（seed ごとの manifest ハッシュは
  `organizer/metrics/data_versions.json`）。
- 参加者 run を `train/`・`select/`・`evaluate/` に記録する（`experiments/competition1/README.md` 参照）。
