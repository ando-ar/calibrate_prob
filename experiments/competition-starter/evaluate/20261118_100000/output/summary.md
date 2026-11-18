# competition-starter / evaluate / 20261118_100000

## 仮説

市場内 warmup（365日）で学習した3段階スコアを評価日候補へ適用し、
`p_decision_baseline × fee_if_decided_yen` の降順で並べた順序が
コンペのリファレンスベースラインになる（COMPETITION.md §7）。
参加者の工夫（抽出戦略・校正・別モデル）はこの主指標を上回るべきである。

## 設定

- data version（manifest ハッシュ）: `3f55683a09fcda57486d89a8d6534bb21d992a7586d162673539f05353123b75`
- generator version: `0.12.0`
- seed: market=71, participant=0, sql=0
- データセット: proof（days=730, arrivals=20/20, warmup=365, 評価オフセット 400/401/402）
- 本番 config: days=1825, arrivals=50/50, 評価オフセット `[730,760)`（運営が後日生成）
- 学習行数と抽出戦略: warmup 紹介 322,807 行から3段階を balanced 再学習。
  各モデル 616/68/18 行（正例 308/34/9）。潜在列・将来の結果は不使用。
  `max_training_rows=300000` を run config に記録（本 proof では未達）。
- 枠・順序付け・手数料率: 一人一日5件、アクティブユーザーあたり日次5件、
  `stage_product_times_fee`、`fee_rate=0.33`、確率は `[1e-9, 1-1e-9]` に clip。
- 比較対象ベースライン run: なし（この run 自体がベースライン）。

## 結果

- 提出: `submission.parquet` 1,237 行（3日: 390 / 401 / 446）。スキーマ §6 準拠。
- 主指標 `macro_daily_replay_fee_yen`: <評価器（issue #35）待ち>
- ベースライン差: <評価器待ち>
- 副指標（logged / counterfactual）: <評価器待ち>
- 複数 seed の平均±標準偏差: <未実施>

## 考察

<評価器が実装された後で記入。単一 seed の差を普遍的な優劣と結論づけないこと。>

## 次にやること

1. issue #35（リプレイ＋評価器）が利用可能になったら `synthetic-competition evaluate` を
   実行し、`output/metrics.json` / `daily.csv` を生成する。
2. 上の「結果」「考察」を埋め、`status` を `succeeded` に更新する。
3. 本番データセット（days=1825, 50/50）で同じベースラインを再生成し、
   data version を更新した別 run として記録する。
