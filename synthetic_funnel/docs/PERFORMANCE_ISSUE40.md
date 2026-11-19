# issue #40 シミュレーション性能レビュー

測定環境: このリポジトリの `uv` venv（CPython 3.14.3、numpy 2.x）、Windows。
代表的設定: `MarketConfig(days=180, warmup=90, seeker_arrivals=100, job_arrivals=100)`。
評価ベンチは `seeker_arrivals=job_arrivals=30`、評価3日×提出2通で測定。

## cProfile 内訳（days=180 / arrivals 100/100）

| 区分 | before (cumulative) | after (cumulative) |
| --- | ---: | ---: |
| `simulate` 合計 | 31.97 s | 14.66 s |
| 関数呼び出し総数 | 95.9 M | 24.6 M |
| `random.randrange` + `_randbelow_with_getrandbits`（紹介抽選） | 8.97 s / 8.77 M呼び出し | 1.72 s / 1.65 M呼び出し |
| `add_introduction`（保留集合の走査） | 4.33 s / 1.60 M呼び出し | 1.16 s / 1.60 M呼び出し |
| 日次プール再構築（`sort` / `max` / lambda / `searchsorted`） | 約 2.7 s | 増分更新（`pool_add` 0.10 s + `bisect_left` 0.16 s） |
| `probability_arrays` + `features` / `numeric_profiles` | 約 0.85 s | 約 0.80 s（変更なし） |
| `counterfactual`（潜在ストリーム） | 0.06 s | 0.06 s（変更なし） |

### 適用した最適化

1. **未紹介残りからの直接サンプリング**: 各求職者のeligibleプールから `seen` を差し引いた列表を作り、`rng.randrange(len(unseen))` を紹介件数（最大 `referrals`）だけ消費する、swap-remove方式に変更。200回の拒否ループを廃止し、抽選回数を 8.77 M → 1.65 M に削減。
2. **日次プールの増分維持**: `(職種, 地域)` ごとの `(最低提示給与, jid)` リストを `bisect.insort` で到着時・退出時に更新。リモート求人は全域に登録。毎日の全 live job スキャンとソートを廃止。
3. **行単位ループのベクトル化/ O(1) 化**: `other_applications` / `competing_candidates` を actor ごとの `active` カウンタで維持し、紹介1行ごとの保留セット走査を削減。確率ゲートと潜在列は既存の numpy ベクトル実装を維持。

乱数ストリームは市場ストリームのみ順序が変わる（許容された変更）。潜在ストリーム `SeedSequence([seed, b'latent'])` と学習ストリーム `b'train'` は独立のまま。詳細は [MARKET.md](MARKET.md) の「生成 RNG 規律の変更」を参照。

## wall-clock

| 項目 | before | after | 変化 |
| --- | ---: | ---: | ---: |
| 生成 1回目 (100/100) | 11.99 s | 9.09 s | −24% |
| 生成 2回目 (100/100) | 11.48 s | 8.73 s | −24% |
| 評価 提出1 (30/30) | 3.30 s | 2.13 s | −35% |
| 評価 提出2 (30/30) | 3.15 s | 2.06 s | −35% |

## D_train スナップショット再利用

`freeze` で完全な市場状態（アクター、live/seen、保留ケースとヒープ、アンカー、両RNG状態、紹介プール）を1度だけ pickle 保存し、`evaluate --snapshot` で読み込む。

| 設定 | 凍結 (1回) | 提出2通 fresh 合計 | 提出2通 reuse 合計 | 改善 |
| --- | ---: | ---: | ---: | ---: |
| 評価日 60–62（warm 60/180） | 0.57 s / 14 MB | 4.20 s | 3.48 s | −17% |
| 評価日 100–102（warm 99/180） | 0.92 s / 27 MB | 4.89 s | 3.51 s | −28% |

読み込んだスナップショットからのリプレイは、day 0 から再シミュレーションしたリプレイと行・指標ともにバイト単位で一致する（`test/test_issue40_performance.py` が検証）。凍結コストは提出が1回増えるたびに回収できる。
