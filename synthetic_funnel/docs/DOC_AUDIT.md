# ドキュメントと実装の整合監査（refs #20）

コードを正（source of truth）として、利用者向けドキュメントの記述を実装と突き合わせ、
乖離を修正した監査の記録です。監査対象は `synthetic_funnel/docs/`、`synthetic_funnel/README.md`、
リポジトリ `README.md`、`calibration_pipeline/` のドキュメント群、
`competitions/competition1/`（RULES.md / participants / frozen_config.json）、`starter_kit/README.md`。

分類:

- **A**: ドキュメントが誤り・コードが正 → ドキュメントを修正
- **B**: ドキュメントが曖昧だがコードが妥当な解釈 → ドキュメントを明確化
- **C**: コードにない挙動の主張、または確定仕様（COMPETITION.md / frozen_config.json）との矛盾 → 勝手に変更せず未決点として報告

## 検出結果一覧

| # | ドキュメント（節） | 記述（claim） | コードの真実（code truth） | 分類 | 対応 |
| --- | --- | --- | --- | --- | --- |
| 1 | MARKET.md「CLIからデータセットを保存する」 | manifest に「生成時間」が入る | manifest は実行時計測値・タイムスタンプを意図的に除外（`market_export.py:190-193` のコメント、`manifest` dict に該当フィールドなし） | A | 「生成時間」を削除し「出力ファイルのSHA-256」に修正 |
| 2 | QUICKSTART.md「manifestで生成条件を確認する」 | 同上（「生成時間」が入る） | 同上 | A | 同上 |
| 3 | DATA_FORMAT.md「manifest.json」 | manifest フィールド表が `training` と `truth_scope` を欠く | `market_export.py:201,206` で `training`（warmup学習の設定・結果）と `truth_scope`（反実仮の注記）を常に出力 | A | 2行を追加 |
| 4 | DATA_FORMAT.md「ウォームアップ期間とスコア」／「manifest.json」`training_cutoff` | 「`training_cutoff` より前の紹介のスコア列はNULL」 | スコア付与の境界は `day >= warmup`（= `warmup_end`、`market_training.py:171`）。`training_cutoff` は最後のwarmup紹介日時（`warmup_end` の約1日前、`market_training.py:154`）で、それ自身もスコアNULL | B | `warmup_end` 境界に修正し、`training_cutoff` の意味と null の場合を明記 |
| 5 | DAILY_CANDIDATES.md「候補プールの条件」 | 給与フロア `<=` が「シミュレータの紹介プールと同一」 | 組み込み抽選は `bisect_left` による厳密な `<`（`market.py:593`、`test_market.py:121` が `>` を検証）。SQL レシピは `<=`（`market_candidates.py:55,99`）。等号境界のみの差で、給与は連続値のため生成データ上は一致 | B | 境界の差を明記 |
| 6 | COMPETITION.md「§4 手数料の定義」 | 「`events` の決定行に対して次を追加する」と `offered_salary` / `fee_rate` を掲げる | `fee_rate` は `item`、`offered_salary` は `item` の既存列、`events` に追加されるのは `fee_yen` のみ（`market_export.py:40-56,122-123`） | B | 列の配置を明記 |
| 7 | MARKET.md「市場のルール」の `MarketConfig` 表 | `training_window_days` / `training_referrals_per_day` / `negative_reservoir_size` を欠く | `MarketConfig` の実フィールド（`market.py:71-73`）。API.md では列挙済み | B | 1行追加 |

## 未決点（分類 C・要判断）

### C-1. `replay_positive_capture` の定義が実装と一致しない（解決済み）

- **場所**: COMPETITION.md §5.3 の指標表（「`replay_positive_capture` | リプレイ市場で観測された決定のうち、選択行に残った割合」）
- **コードの真実**: `market_evaluation.py:604` で `replay_positive_capture = replay_positives / base_decided`。
  - `replay_positives`（`market_evaluation.py:540-558`）は、リプレイで枠割当により選択・紹介されたペアのうち、期限内（`H`）に決定した件数。
  - `base_decided`（`market_evaluation.py:563-575`）は、**ベース市場（配布 `events` ログ）**で評価日内に決定した件数。
- **乖離**: ドキュメントの分母は「リプレイ市場で観測された決定」だが、実装の分母は「ベース市場（ログ）の決定」。つまり実際は「ベース市場の決定に対する、選択行の決定の捕捉率」として計算されている。リプレイ市場の評価日内の紹介は選択行のみのため、文言どおりだと常に 1 になる。
- **対応（決定済み・2026-11-24 運営承認）**: **option b（実装を変更）** を採用。`replay_positive_capture` を「選択ペアのうちリプレイ市場で決定に達した割合 = `replay_selected_positives / 選択行数`」と再定義する。1ユーザー1日5件の紹介上限により、これはリプレイ市場のユーザー単位 **Precision@5**（従来コンペの `precision_at_k` と同種）に一致する。
  - `market_evaluation.py` の計算を `replay_positives / len(selected)` に変更（選択行が無い場合は `None`）。使用されなくなった `base_decided` のデッドコードと関連変数を除去。
  - COMPETITION.md §5.3 の定義を Precision@5 の意味論へ更新。
  - 本件は解決済み（commit `fix: define replay_positive_capture as precision over selected pairs`、refs #20）。

## 検証結果

- `cd synthetic_funnel && uv run --locked pytest -q` → **106 passed**（106 件）
- `uv run --locked --group docs sphinx-build -b html -W docs dist/docs` → 警告なし（ビルド成功）
- `cd calibration_pipeline && uv run --locked --group docs python dev/build_docs.py` → 警告なし（ビルド成功）
- calibration_pipeline の Python ソースは変更していない（ドキュメントのみのため、Ruff・型検査は対象外）

## 変更しなかった確認済み項目

監査したがコードと一致しており、変更不要と判断した主な項目:

- CLI フラグと既定値（`--run-dir` / `--output` / `--config` / `--format` / `--seed` / `--seeds` / `--jobs` / `--seeker-arrivals` / `--job-arrivals` / `--progress`）
- 設定フィールド名・既定値（`seed=71`、`days=1825`、`warmup=365`、`application_days=20`、`posting_days=40`、`referrals=5`、`training_window_days=[1,30,365]` など）
- 列名・型・NULL 意味論（`events` / `events_latent` / `user` / `item` / `daily`）、主キー・外部キー
- 手数料列（`item.fee_rate`=0.33、`events.fee_yen = offered_salary × fee_rate` を決定行のみ、未決定は NULL）
- warmup 学習の意味論（warmup 前はスコアNULL、`warmup >= days` で全NULL）
- リプレイ／凍結（`freeze` / `evaluate --snapshot`、`warm_state` のバイト同一性）
- 確率モデル（`base_probabilities=(0.01,0.10,0.07)`、`probability_half_widths`、`skill_effects`、gap ペナルティ、`p_employer_application=1`）
- 順序付け規則（`stage_product_times_fee`、`clip_epsilon=1e-9`、同点はキー昇順）
- calibration_pipeline の列契約・seed・CatBoost 固定パラメータ・run ID 規則
