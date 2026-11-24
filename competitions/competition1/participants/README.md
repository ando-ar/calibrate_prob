# competition1 参加者向け案内

コンペの目的は**期待手数料の最大化**である。各評価日の候補集合
（`candidates`）に順序（rank）を付け、運営側の枠割当ルールのもとで紹介された
ペアから生まれる観測実績手数料（`fee_yen`）を最大化する。仕様の正本は
`../../synthetic_funnel/docs/COMPETITION.md`、運営ルールは
[`../organizer/RULES.md`](../organizer/RULES.md)。校正（確率の較正）は主目的ではなく、
任意の手法で順序を作ってよい（提出に確率列は含めても採点されない）。

## 1. 配布物（`data/seed-<s>/`）

| ファイル | 内容 |
| --- | --- |
| `dataset.sqlite.db` | 観測テーブルのみ: `user` / `item` / `daily` / `events`（観測列のみ） |
| `manifest.json` | config・seed・`warmup_end`・各ファイル SHA-256・data version |
| `candidates_eval.parquet` | 評価日 `[150, 180)` の完全候補集合（観測列のみ。`fee_if_decided_yen` を含む） |
| `config.json` | market config・評価日オフセット・順序付け規則 |
| `submission.parquet` / `submission.csv` | ベースライン提出（§5 の例） |
| `baseline_model.json` | warmup 学習済みモデル係数（監査用） |

> **非配布**: `user_latent` / `item_latent` / `events_latent` / `candidates_latent` は
> 運営専用であり、配布 DB から除去済み。これらへのアクセスは禁止。

## 2. データの読み込み

```python
import json, sqlite3
from datetime import datetime, timedelta
from synthetic_funnel.market_candidates import candidate_rows

DB = "competitions/competition1/participants/data/seed-71/dataset.sqlite.db"
MANIFEST = "competitions/competition1/participants/data/seed-71/manifest.json"
start = datetime.fromisoformat(json.load(open(MANIFEST))["market_start"])

with sqlite3.connect(DB) as con:
    # 評価日の完全候補集合（全行・無重み）。提出の rank 付け対象。
    candidates = candidate_rows(con, (start + timedelta(days=150)).isoformat())
    # 学習履歴は D_train（offset 150）までの events のみ。
    history = con.execute(
        "SELECT * FROM events WHERE shoukai_dt < ? ORDER BY shoukai_dt",
        ((start + timedelta(days=150)).isoformat(),),
    ).fetchall()
```

## 3. 学習行数制限（重要）

**学習に使う紹介行の総数は `max_training_rows = 300,000` を超えないこと**
（分類器と金額モデルの行数を別々に記録し、合計が上限以下）。一様抽出・重点抽出
（stage）・`inverse_sampling` 等の抽出戦略は自由。戦略・実際の取得件数・採択率を
`run_metadata.json` に記録すること。

```python
MAX_TRAINING_ROWS = 300_000
import random
random.Random(42).shuffle(history)
training = history[:MAX_TRAINING_ROWS]
print("acquired", len(training), "of", len(history),
      "selection_rate", len(training) / max(1, len(history)))
```

## 4. 順序付け（最小例）

`p_decision × fee_if_decided_yen` の降順（同点はキー昇順）がベースライン。自前の解法では
学習履歴から決定ラベルを予測し、期待手数料で並べる。

```python
import pyarrow.parquet as pq
cand = pq.read_table(
    "competitions/competition1/participants/data/seed-71/candidates_eval.parquet"
).to_pylist()
ranked = sorted(
    cand,
    key=lambda r: (-(my_p_decision(r) * r["fee_if_decided_yen"]),
                   r["day_dt"], r["user_id"], r["item_id"]),
)
submission = [
    {"day_dt": r["day_dt"], "user_id": r["user_id"], "item_id": r["item_id"], "rank": i}
    for i, r in enumerate(ranked, start=1)
]
```

## 5. 提出形式

| 列 | 型 | 必須 | 内容 |
| --- | --- | --- | --- |
| `day_dt` | TEXT | ○ | 評価日（manifest と同じオフセットの ISO 8601） |
| `user_id` | TEXT | ○ | 求職者 ID |
| `item_id` | TEXT | ○ | 求人 ID |
| `rank` | INTEGER | ○ | その日内の 1 始まり順序。同一日内で一意 |
| `p_decision` / `p_application` / `p_screening` | REAL | × | 診断用（採点しない） |
| `expected_fee_yen` | REAL | × | 診断用（採点しない） |

- ファイル名は `submission.parquet`（正式）または `submission.csv`。
- キーは `(day_dt, user_id, item_id)`。提出行は配布 `candidates` の部分集合でよい
  （未提出行は順位なし＝選ばれない）。
- 枠割当（一人一日 5 件、1 アクティブユーザーあたり日次 5 件）は**評価器が再計算**するため
  参加者が自分で適用する必要はない。
- 提出は決定論的であること（同じ入力・コード・環境で同一バイトが再現できること）。
- `submission_metadata.json` を必ず添付する（seed、Python/主要パッケージ版、git commit、
  config ハッシュ、data version、author、`generated_at`）。

## 6. ベースラインと合格条件

ベースラインは warmup（`[0, 90)`）の紹介履歴だけで学習した 3 段階モデル
（応募・書類通過・決定）の積 `p_decision_baseline × fee_if_decided_yen` で並べた順序
（`submission.parquet` がその実例）。主指標 `macro_daily_replay_fee_yen` でベースラインを
上回った提出のみ有効。単一 seed の差を普遍的な優劣と呼ばない（複数 seed の平均±標準偏差で比較）。

## 7. 禁止事項

- `user_latent` / `item_latent` / `events_latent` / `candidates_latent` へのアクセス。
- 将来 issue #32 で追加される `status` / `outcome_dt` / `auto` を特徴量に使うこと（紹介後に
  確定する情報のため学習禁止）。
- 参加者独自の料率・金額定義を提出に使うこと（比較を不可能にするため）。
