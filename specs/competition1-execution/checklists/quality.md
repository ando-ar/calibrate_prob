# competition1 実行フェーズ 品質チェックリスト

> Spec Kit の `/speckit.checklist` 相当。提出検証（参加者側）と運営作業（運営側）の 2 種を、実行フェーズの
> 各段の出口で確認する。項目は「確認できたらチェック」し、満たせない項目は [`tasks.md`](../tasks.md) へ追記する。

## A. 提出検証チェックリスト（submission validation）

### A1. スキーマ

- [ ] 必須列 `day_dt` / `user_id` / `item_id` / `rank` が存在し、型が正しい（TEXT / INTEGER）。
- [ ] `rank` が同一 `day_dt` 内で 1 始まり・一意（重複・欠番は評価器が同点処理を適用する前提を確認）。
- [ ] 提出キー `(day_dt, user_id, item_id)` が配布 `candidates_eval.parquet` の部分集合（配布候補に無いペアなし）。
- [ ] `day_dt` が manifest と同じオフセットの ISO 8601（評価日 `[150, 180)` 内）。

### A2. 決定論・再現性

- [ ] 同じ入力・コード・環境で同一バイトの提出が再現できる（`submission_sha256` 一致）。
- [ ] 同点処理がキー昇順（`user_id, item_id`）になっている（提出側の順序付け）。

### A3. メタデータ

- [ ] `submission_metadata.json` を添付している。
- [ ] 必須フィールド: seed、Python/主要パッケージ版、git commit、config ハッシュ、data version、author（チーム名）、`generated_at`。
- [ ] `author` が割り当てチーム名（Team A〜E）と一致している。

### A4. data version

- [ ] 提出の data version が配布 `manifest.json` の SHA-256 と一致している（運営側 `data_versions.json` と照合）。

### A5. チームビュー遵守（自己申告＋レビュー）

- [ ] 学習行数が自チームの `max_training_rows`（Team D は 150,000、他は 300,000）以下であることを申告。
- [ ] 学習に使った `events` が自チームのビュー述語（Team A: `[120,150)` / Team C: 職種部分集合 等）に従うことを申告。
- [ ] 抽出戦略・実際の取得件数・採択率を `run_metadata.json` に記録。
- [ ] （上位入賞者）提出コードレビューで行数上限・ビュー・禁止列の遵守を確認。

### A6. 禁止事項

- [ ] `user_latent` / `item_latent` / `events_latent` / `candidates_latent` を使用していない。
- [ ] `status` / `outcome_dt` / `auto`（紹介後に確定する情報）を学習特徴量に使っていない。
- [ ] 参加者独自の料率・金額定義を使っていない（`fee_if_decided_yen` をそのまま使用）。

## B. 運営作業チェックリスト（organizer operations）

### B1. データ再生成

- [ ] `build_release.py --seeds 71,72,73,74,75` を逐次実行（`--jobs 2-3` 以下）、5 seed を同時保持しない。
- [ ] 各 seed の `manifest_sha256` を `metrics/data_versions.json` と `experiments/competition1/` に記録。
- [ ] `metrics/baseline_aggregate.json` を新 config で更新（平均±標準偏差）。
- [ ] 生成環境・`generator_version`・依存を記録し、再生成でハッシュが一致する（再現性）。
- [ ] DB/parquet を都度削除し、`metrics.json`・manifest ハッシュ・`summary.md` は残す。

### B2. チームビュー導出・パッケージ

- [ ] 全チーム共通の凍結データ（`dataset.sqlite.db` + `manifest.json`）がバイト同一（data version 一致）。
- [ ] ビュー成果物（`team_view.json` / `team_view_events.parquet` / `team_view.md`）が各チームに配置されている。
- [ ] ビュー導出が決定的（固定 seed で再現）。
- [ ] 潜在テーブル・潜在列がビュー成果物に漏れていない（`assert_no_latent` 相当）。
- [ ] ビューは「行を狭める」のみで行内容を改変していない。

### B3. 評価器実行

- [ ] seed ごとに凍結状態・人口再構成（`--population-dir`）を再利用。
- [ ] 全チームを同じ seed・同じ真値・同じ評価器で評価。
- [ ] 評価器が決定論的（同じ入力→同じ `metrics.json`/`daily.csv`/`summary.md`）。
- [ ] `ranked.parquet` を削除（大容量生成物）、小容量の記録を残す。

### B4. 集計・副次ボード

- [ ] 主指標を `mean ± std(ddof=0)` で集計し、`_per_seed` を併記。
- [ ] 副次リーダーボードは表示のみ（順位を変えない）。
- [ ] 順位は主指標のみで決定。ベースライン超過の合否を適用（必要なら `margin`）。
- [ ] 最終評価は選んだ 1 提出に 1 回だけ実行。

### B5. 記録・既知リスク

- [ ] `run_metadata.json` に data version・seed・チーム・ビュー規則・抽出戦略・温度/ペルソナ・git commit・環境を記録。
- [ ] data version の整合（organizer と experiment の一致）。
- [ ] 事後の多様性メトリクス（Vendi Score 等）を算出し記録。
- [ ] 既知リスク（主指標の分散・自己申告依存・決定論）を各段で確認。
