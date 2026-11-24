# ベースライン解法

competition1 のベースラインは COMPETITION.md §7 に従う:

1. 市場内 warmup（`[0, 90)`）の紹介履歴だけで応募・書類通過・決定の 3 段階ロジスティックモデルを
   学習する（生成器と同じ `train_warmup_models`、観測データのみ、潜在列・未来の結果は使わない）。
2. 評価日の `candidates` に適用し、`p_decision_baseline = clip(p_apply) × clip(p_screen) ×
   clip(p_decision)` を求め、`p_decision_baseline × fee_if_decided_yen` の降順（同点は
   `(day_dt, user_id, item_id)` 昇順）で並べる。

実装は `competitions/competition1/organizer/build_release.py` が `starter_kit/build_baseline.py`
（issue #33 の proof キット）の `build_submission` を再利用して生成する。
