# Competition starter kit — synthetic funnel (issue #33)

このフォルダが参加者向けの配布単位（QUICKSTART）です。仕様の正本は
[`synthetic_funnel/docs/COMPETITION.md`](../synthetic_funnel/docs/COMPETITION.md) であり、
ここでは「実際に手を動かす手順」だけをまとめています。仕様書を書き換えずにキットを
丸ごと配布できるよう、クイックスタートは `docs/` ではなくこのフォルダに置いています。

## 0. 配布物と非配布物

| ファイル | 内容 | 参加者 |
| --- | --- | --- |
| `config.json` | proof 用 market config、本番 config、run config、ベースライン定義 | ○ |
| `generate_dataset.py` | 運営側: データ生成 + 参加者用 DB の抽出 | ○（再現用） |
| `build_baseline.py` | §7 ベースライン提出を配布データから再生成 | ○ |
| `data/proof/participant/dataset.sqlite.db` | 観測テーブルのみ（`user` / `item` / `events` / `daily`） | ○ |
| `data/proof/participant/manifest.json` | config・seed・`warmup_end`・各ファイル SHA-256 | ○ |
| `data/proof/participant/candidates_eval.parquet` | 評価日の `candidates`（観測列のみ） | ○ |
| `data/proof/participant/submission.parquet` / `.csv` | §7 ベースライン提出 | ○ |
| `data/proof/participant/baseline_model.json` | warmup 学習済みモデル係数（監査用） | ○ |
| `data/proof/organiser/` | 完全な7テーブル（潜在値を含む） | **×（運営専用）** |

`user_latent` / `item_latent` / `events_latent` と `candidates_latent` は参加者用 DB から
削除済みです。変換確率・意欲・潜在スキルは配布しません（§1）。

`data/` 配下の sqlite / parquet は `.gitignore` 済みの生成物で、Git にはコミットしません。
参加者には成果物として別途配布されます。

## 1. データセット config（proof と本番）

| 項目 | 本番（COMPETITION.md §3.1 既定） | この proof キット |
| --- | --- | --- |
| `market_start` | `2025-01-01T00:00:00+09:00` | 同じ |
| `seed` | 71 | 同じ |
| `days` | 1,825 | 730 |
| `warmup` | 365 | 同じ |
| `seeker_arrivals` / `job_arrivals` | 50 / 50 | 20 / 20 |
| `application_days` / `posting_days` | 20 / 40 | 同じ |
| `referrals` | 5 | 同じ |
| `H` | 3暦月 | 同じ |
| 評価オフセット | `[730, 760)`（30日） | `[400, 402]`（3日、デモ用） |
| `max_training_rows` | 300,000 | 同じ（この规模では制約に達しない） |

本番既定（50/50 × 1,825日）は約750万件の `events` になり、DB は数 GB 規模で
キットの配布・生成には不向きなので、運営が後日生成します。proof は
661,168 件（warmup 322,807 件）で、`warmup` を 365 日保ったまま
3段階すべての warmup モデルが学習できる最小规模（20/20）を選びました。
`D_eval_end + H <= observation_end_exclusive`（402 + 約90 < 730）を満たします。

本番 config は `config.json` の `production_reference` に明示してあります。
データ version は配布 `manifest.json` の SHA-256 です（この proof では
`e973aaaac7668ec6ce8099b0be74215aa12d15ddaf1f74991433af0f9d941ea1`）。
manifest は生成時間などの実行時計測値を含まないため、同一 config/seed なら
バイト単位で再現します（issue #38）。

## 2. 環境と再生成

```powershell
# リポジトリ直下で実行
uv run --project synthetic_funnel python starter_kit/generate_dataset.py --force
uv run --project synthetic_funnel python starter_kit/build_baseline.py
```

`build_baseline.py` は配布 DB から §7 ベースラインを再生成し、
`submission.parquet` / `submission.csv` / `submission_metadata.json` を上書きします。
同じ config・seed・データ version なら提出 parquet はビット単位で同一です
（診断メタデータの `generated_at` を除く）。

### 複数 seed の生成（issue #40）

評価ではストレージ制約（空き約25GB）から seed を **3〜5 本**に抑制し、代わりに流入を
100〜200/200 に増やします。紹介上限（5件/日）の消化率は流入 150/150 付近で約97%に
飽和し、それ以上の流入は決定件数をほぼ増やさないため、seed 数より 1 seed あたりの
流入に予算を使うのが合理的です（仕様 §3.4 / §5.5 / §11 #4）。

seed ごとに独立プロセスで生成するには、フラットな market config JSON に対して次を実行します。

```powershell
uv run --locked synthetic-funnel --config <market-config.json> --output data/seeds --seeds 71,72,73 --jobs 3
```

`data/seeds/seed-<seed>/` に完全なデータセットが作られ、同じ seed の単独実行
（`--seed 71`）とバイト単位で同一です。並列数は既定で CPU コア数、`--jobs` で上限を
指定します。流入の上書きは `--seeker-arrivals 200 --job-arrivals 200` を使います。

## 3. 候補と学習履歴の読み込み（行数制約つき）

`candidates` は観測列だけです。日 D の完全候補集合は `candidate_rows()`（§3.2 /
DAILY_CANDIDATES.md の SQL レシピ）で取得します。学習には D_train までの
`events` を使いますが、**学習行数の合計は `max_training_rows`（既定 300,000）を
超えません**。分類器と金額モデルの行数を別々に記録し、抽出戦略・取得件数・
採択率を `config.toml` と `run_metadata.json` に残してください（§3.3）。

```python
import sqlite3
from datetime import datetime, timedelta
from synthetic_funnel.market_candidates import candidate_rows

DB = "starter_kit/data/proof/participant/dataset.sqlite.db"
MANIFEST = "starter_kit/data/proof/participant/manifest.json"
import json
start = datetime.fromisoformat(json.load(open(MANIFEST))["market_start"])
eval_day = (start + timedelta(days=400)).isoformat()

with sqlite3.connect(DB) as con:
    candidates = candidate_rows(con, eval_day)        # 全行・無重み（評価側）
    history = con.execute(
        "SELECT * FROM events WHERE shoukai_dt < ? ORDER BY shoukai_dt",
        ((start + timedelta(days=400)).isoformat(),),
    ).fetchall()

MAX_TRAINING_ROWS = 300_000                            # §3.3
# 例: 一様抽出。戦略は参加者の裁量（重点抽出・inverse_sampling 等も可）。
import random
random.Random(42).shuffle(history)
training = history[:MAX_TRAINING_ROWS]
print("acquired", len(training), "of", len(history),
      "selection_rate", len(training) / max(1, len(history)))
# classifier_rows + amount_model_rows <= MAX_TRAINING_ROWS を必ず満たすこと。
```

## 4. 最小限のランキング例（model score × fee）

`candidate_features()` と warmup モデルを使う最短例は `build_baseline.py` そのものです。
自前の解法では、学習履歴から決定ラベルを予測し、期待手数料で並べます。

```python
import pyarrow.parquet as pq

cand = pq.read_table(
    "starter_kit/data/proof/participant/candidates_eval.parquet"
).to_pylist()
# p_decision: 自分のモデルが候補特徴量から出した決定確率
# fee_if_decided_yen は配布済みの観測列（fee_rate=0.33）
ranked = sorted(
    cand,
    key=lambda r: (-(p_decision(r) * r["fee_if_decided_yen"]),
                   r["day_dt"], r["user_id"], r["item_id"]),
)
submission = [
    {"day_dt": r["day_dt"], "user_id": r["user_id"], "item_id": r["item_id"],
     "rank": i, "expected_fee_yen": p_decision(r) * r["fee_if_decided_yen"]}
    for i, r in enumerate(ranked, start=1)
]
```

`build_baseline.py` が実際に使う順序付けは
`p_decision_baseline × fee_if_decided_yen` の降順（同点はキー昇順）です。
確率は `[1e-9, 1-1e-9]` に clip します。

## 5. 提出形式（COMPETITION.md §6）

| 列 | 型 | 必須 | 内容 |
| --- | --- | --- | --- |
| `day_dt` | TEXT | ○ | 評価日（manifest と同じオフセットの ISO 8601） |
| `user_id` | TEXT | ○ | 求職者ID |
| `item_id` | TEXT | ○ | 求人ID |
| `rank` | INTEGER | ○ | その日内の1始まりの順序。同一日内で一意 |
| `p_decision` / `p_application` / `p_screening` | REAL | × | 診断用（採点しない） |
| `expected_fee_yen` | REAL | × | 診断用（採点しない） |

- ファイル名は `submission.parquet`（正式）または `submission.csv`。キーは
  `(day_dt, user_id, item_id)` で、配布候補の部分集合でもよい（未提出行は選ばれない）。
- 枠割当（一人一日5件、1アクティブユーザーあたり日次5件）は評価器が再計算するため
  参加者は自分で適用する必要はありません（§2.1）。
- `submission_metadata.json` を必ず添付してください（seed、Python/主要パッケージ版、
  git commit、config ハッシュ、data version、author、`generated_at`）。

## 6. ベースライン（§7）の導出と注意

warmup（`[0, 365)`）の紹介履歴だけから「応募・書類通過・決定」の3モデルを
学習し、評価日の候補へ適用した順序がベースラインです。`build_baseline.py` は
生成器と同じ `train_warmup_models()` を観測データだけで再実行するため、係数は
配布された warmup モデルと一致します（`baseline_model.json` で監査可）。

仕様 §7 は順序を `p_decision_baseline × fee_if_decided_yen` と書いています。
`model_apply_score` / `model_screen_score` / `model_decision_score` は
**各段階の条件付き確率**（apply、screen|apply、decision|screen）なので、
warmup モデルから得られる唯一の「決定までの周辺確率」は3つの積です。
このキットでは

```
p_decision_baseline = clip(p_apply,eps,1-eps)
                    × clip(p_screen,eps,1-eps)
                    × clip(p_decision,eps,1-eps)
expected_fee_yen     = p_decision_baseline × fee_if_decided_yen
```

と定義しました。`model_decision_score` だけを使うと応募・書類通過を無視するためです。
`config.json` の `ranking_rule` で `decision_times_fee`（決定段階スコアのみ）と
`odds_times_fee`（`p/(1-p) × fee`）にも切り替えられます。どのruleを使ったかは
`submission_metadata.json` と `run_metadata.json` に記録してください。

## 7. 評価と未実装事項

評価器の CLI は仕様 §5.6 のインターフェース契約のみで、まだ実装されていません
（issue #35 でリプレイ＋評価器を実装中）。したがって現時点では
**ベースライン指標（`macro_daily_replay_fee_yen` 等）は未計測**です。
評価器が利用可能になったら `submission.parquet` と `manifest.json` を渡して
`output/metrics.json` を生成し、`experiments/competition-starter/evaluate/` の
`run_metadata.json` と `summary.md` のプレースホルダーを埋めてください。

```text
synthetic-competition evaluate \
  --manifest <data/proof/participant/manifest.json> \
  --truth <運営専用のリプレイ結果> \
  --submission <data/proof/participant/submission.parquet> \
  --config <run config> \
  --output <評価出力ディレクトリ>
```

再現性: 同じ data version・config・コード commit・環境で run し、単一 seed の差を
普遍的な優劣と呼ばないでください（§5.4）。実験記録の規約は §8 /
`experiments/_template/` を参照してください。
