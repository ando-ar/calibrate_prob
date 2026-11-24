# competition1 実行フェーズ タスク（tasks）

> [`spec.md`](spec.md) の実行フェーズ仕様を、依存順のタスクへ分解したもの。Spec Kit の `/speckit.tasks` 相当の
> 分解（Setup → Foundational → ユーザーストーリー別 → Polish）を、本リポジトリの様式で記す。
> 各タスクの完了条件は spec.md と [`checklists/quality.md`](checklists/quality.md) に照合して閉じる。
> **シミュレータ本体・評価器・凍結 config は変更しない。** 実行は Orca コーディネーターが worktree へ分割派遣する。

## 依存関係の概要

```text
T1 データ再生成 ──> T2 チームビュー導出 ──> T3 パッケージ組み立て ──> T4 提出受付・検証
                                                                        └──> T5 評価器実行 ──> T6 集計・副次ボード
T7 実験記録更新（T1〜T6 と並行して各段の成果物を記録）
T8 既知リスク確認（T1〜T6 の各段の出口で確認）
```

- T2 と T3 は T1 の凍結データ（data version）に依存する。
- T7 / T8 は横断的であり、各段の成果物が揃い次第随時実施する（最終段で総合確認）。
- 並列可能な箇所: T2 のチーム別ビュー導出（5 チーム独立）、T5 の seed 別評価（ストレージ制約で逐次）。

---

## T1. データ再生成（#46 後 config、data version 確定）

**目的**: 凍結 config（`segment_drift` 込み）で 5 seed を再生成し、data version（manifest ハッシュ）を確定する。
`RULES.md` §1 注記のとおり **manifest ハッシュは旧 proof 値から変わる**。

- [ ] `build_release.py --seeds 71,72,73,74,75` を逐次実行（`--jobs 2-3` 以下）。seed ごとに
      「生成 → participant パッケージ化 → organizer truth 保持 → 評価器実行 → metrics/manifest 保存 → DB/parquet 削除」。
- [ ] 各 seed の `manifest_sha256` を `organizer/metrics/data_versions.json` と `experiments/competition1/`
      の `run_metadata.json` に記録（`RULES.md` §2.3）。
- [ ] `organizer/metrics/baseline_aggregate.json` を新 config で更新（5 seed の平均±標準偏差）。
- [ ] ベースライン主指標が非縮退（決定件数 m ≳ 30 を確認、MARKET_DENSITY.md §6）。
- [ ] 生成環境（`generator_version`・依存）を記録し、再現性を確認（同一 config/seed でハッシュが一致すること）。

**完了条件**: `data_versions.json` に 5 seed のハッシュが記録され、ベースラインが評価可能。

---

## T2. チーム別データビューの導出（`build_release.py` 拡張の設計＋実装）

**目的**: spec.md §3.3 のチームビュー規則を実装する。**設計は本 spec に記載済み**（下記）。実装は「小規模かつ
クリーン」に留め、物資化 parquet と規則 JSON/MD を出力する。

### 実装方針（spec.md §3.3 / §3.4 を直接実装可能な形にした設計）

1. **新規 `organizer/team_config.json`** を追加（凍結 config は触らない）。内容: チーム一覧（A〜E）、役割、
   ビュー規則（述語・行数上限・抽出 seed・職種部分集合）、反収束ルール、温度/ペルソナ指示。
2. **`build_release.py` にチームビュー導出ステップを追加**（`process_seed` の participant パッケージ化の後に、
   または独立関数として）:
   - `derive_team_view(connection, manifest, team_cfg) -> dict` を新設。入力は凍結 `dataset.sqlite.db` と
     `manifest.json`（data version はここで確定済み）。
   - ビュー規則の適用: `events` を `shoukai_dt ∈ [90, 150)` に絞り、Team A はさらに `[120, 150)`、
     Team C は `events ⋈ item`（または `⋈ user`）で `occupation ∈ {エンジニア, 営業}` に絞る。
     Team D は固定 seed の一様抽出で 150,000 行に。Team B/E は `[90, 150)` 全体。
   - 出力: `participants/data/seed-<s>/team-<A..E>/team_view.json`（規則の機械可読表現）と
     `team_view_events.parquet`（物資化、取得件数・選択率を記録）、`team_view.md`（§2 の役割・ルールを展開）。
   - **凍結データ（`dataset.sqlite.db` + `manifest.json`）はバイト同一のまま全チーム共通配布**。ビューは
     「許可する行」を狭めるだけで行内容を改変しない。
3. **テスト**: `derive_team_view` の単体テストを proof 規模（50/50・1 seed）で追加。ビュー述語ごとに
   「許可行数が期待通り」「行内容（値）が凍結データと一致」「抽出が決定論的（固定 seed で再現）」を検証。
   data version が変わらないこと（participant manifest が運営 manifest と同一ハッシュ）をアサート。

- [ ] `organizer/team_config.json` を追加（チーム・役割・ビュー規則・温度/ペルソナ）。
- [ ] `derive_team_view` を実装（`build_release.py` または隣接モジュール）。
- [ ] チーム別 `team_view.json` / `team_view_events.parquet` / `team_view.md` を出力。
- [ ] `assert_no_latent` 相当の検証をビュー成果物にも適用（潜在列・潜在テーブルが漏れないこと）。
- [ ] proof 規模の単体テストを追加し、`uv run pytest -q` が通ること。

**完了条件**: 5 チームのビューが同一凍結データから決定的に導出され、data version 不変・潜在漏れなし・テスト green。

---

## T3. チーム別 participant パッケージの組み立て

**目的**: 凍結データ＋チームビューをチーム別の配布物として組み立てる。

- [ ] 全チーム共通の凍結データ（`dataset.sqlite.db`・`manifest.json`・`config.json`・ベースライン
      `submission.parquet`・`baseline_model.json`）を各チームパッケージへ複製（バイト同一）。
- [ ] チーム別ビュー成果物（T2）を `team-<A..E>/` へ配置し、`team_view.md` に役割・反収束ルール・
      温度/ペルソナ・ビュー規則を明記（§2）。
- [ ] 参加者 README（`competitions/competition1/participants/README.md`）へチームビューの節を追記
      （「自チームの `team_view.md` に従うこと」「ビュー遵守は自己申告＋レビュー」）。
- [ ] 配布物の manifest ハッシュが全チーム同一（data version 一致）であることを検証。

**完了条件**: 5 チームそれぞれが、同一 data version の配布物＋自チームのビュー成果物を受け取れる。

---

## T4. 提出受付・検証（submission intake + validation）

**目的**: 参加者提出を受け付け、[`checklists/quality.md`](checklists/quality.md) の検証チェックリストに沿って
有効性を確認する。

- [ ] スキーマ検証: 必須列（`day_dt`/`user_id`/`item_id`/`rank`）、型、同一日内 rank 一意、キーが配布候補の部分集合。
- [ ] 決定論検証: 同じ入力・コード・環境で `submission_sha256` が一致すること。
- [ ] メタデータ検証: `submission_metadata.json` の author（チーム名）・data version・git commit・config ハッシュ。
- [ ] data version 一致: 提出の data version が配布 manifest ハッシュと一致。
- [ ] チームビュー遵守（自己申告）: 学習行数・抽出戦略・ビュー述語の遵守を申告し、記録。
- [ ] 禁止事項: 潜在データ不使用・`status`/`outcome_dt`/`auto` を特徴量に使っていないこと（コードレビュー）。

**完了条件**: 有効な提出のみ T5 へ進み、検証結果（合否・理由）を記録する。

---

## T5. 評価器実行（チーム×seed、freeze/snapshot 再利用）

**目的**: 各チームの提出を 5 seed で評価する。

- [ ] seed ごとに凍結状態・人口再構成（`--population-dir`）を再利用し、再生成を繰り返さない（`RULES.md` §3.2）。
- [ ] 各チーム×各 seed で `synthetic-competition evaluate` を実行（同じ seed・同じ真値・同じ評価器）。
- [ ] 決定論を確認（同じ入力→同じ `metrics.json`/`daily.csv`/`summary.md`）。
- [ ] `metrics.json`・`daily.csv`・`summary.md` を保存、`ranked.parquet` は削除（大容量生成物）。
- [ ] ストレージ制約を守る（seed 逐次、`--jobs 2-3` 以下）。

**完了条件**: 5 チーム×5 seed の評価結果が揃い、各 run が決定論的・再現可能。

---

## T6. 集計と副次リーダーボード

**目的**: seed 平均±標準偏差で集計し、主リーダーボード（順位）と副次リーダーボード（表示のみ）を作る。

- [ ] 主指標 `macro_daily_replay_fee_yen` の `mean ± std(ddof=0)` と `_per_seed` を集計（`RULES.md` §3.3）。
- [ ] 副次リーダーボード（表示のみ）: seed 間 std・ロバスト性（5 seed の min/max）・簡潔性・再現性（spec.md §4.3）。
- [ ] **順位は主指標のみで決定**（副次ボードは順位を変えない）。
- [ ] ベースライン超過の合否（必要なら `margin`）を適用。
- [ ] 最終評価は選んだ 1 提出に対して 1 回だけ実行（holdout 増加時は新しい holdout）。

**完了条件**: 順位と副次ボードが確定し、勝敗が主指標のみで決まっている。

---

## T7. 実験記録の更新

**目的**: `experiments/competition1/` へ各段の run を記録する。

- [ ] `run_metadata.json` に data version・seed・チーム名・ビュー規則・抽出戦略・取得件数・温度/ペルソナ・
      git commit・環境・`submission_sha256`・`baseline_run_id` を記録。
- [ ] `data_versions.json`（organizer 側）と experiment 側の data version を一致させる。
- [ ] 失敗 run も含めて `run_metadata.json` を残し、完了前のファイルを完成扱いしない。
- [ ] 事後の多様性メトリクス（Vendi Score・解法 writeup 埋め込み類似度）を算出し、仕掛けの効果を記録（§5.6 相当）。

**完了条件**: 全 run が規約どおり記録され、data version・ハッシュの整合が取れている。

---

## T8. 既知リスク・チェックリストの確認

**目的**: [`checklists/quality.md`](checklists/quality.md) に沿って運営上の既知リスクを各段の出口で確認する。

- [ ] data version の再現性（生成環境・依存の凍結・ハッシュ一致）。
- [ ] 主指標の分散（決定件数 ~45-50 件、単一 seed の差を優劣としない）。
- [ ] チームビューの遵守（自己申告＋上位入賞者のコードレビュー）。
- [ ] 評価器の決定論（実行ごとの `submission_sha256`・metrics 照合）。
- [ ] 潜在データ非漏洩（`assert_no_latent` 相当、ビュー成果物含む）。
- [ ] ストレージ制約（5 seed 非同時保持・削除漏れなし）。

**完了条件**: 全リスク項目が確認・記録され、残項目があれば本ファイルへ追記（append-only）。

---

## Polish（全体の確認）

- [ ] `uv run pytest -q` が green（既存 116 件＋追加分）。
- [ ] `spec.md` / `tasks.md` / `checklists/quality.md` と `RULES.md` の整合（analyze 相当）。
- [ ] 未決点（spec.md §6.2）が開催までに確定している。
- [ ] コミットメッセージは `docs: competition1 execution-phase spec and tasks (refs #33 #47)` 相当（実装時は実装を追記）。
