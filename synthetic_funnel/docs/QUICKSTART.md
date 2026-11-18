# クイックスタート

Python 3.11以上と `uv` を使います。PowerShellでリポジトリ内の `synthetic_funnel/` に移動してから実行してください。

## 1. 実行ディレクトリを作る

以下では90日分の市場を生成します。1日あたりの求職者と求人の流入は8件ずつです。

```powershell
uv sync --locked

$run = "runs/quickstart-$(Get-Date -Format 'yyyyMMdd-HHmmssfff')"
New-Item -ItemType Directory -Path "$run/input" | Out-Null

@'
{
  "market_start": "2025-01-01T00:00:00+09:00",
  "days": 90,
  "seed": 42,
  "seeker_arrivals": 8,
  "job_arrivals": 8
}
'@ | Set-Content -Encoding ascii "$run/input/config.json"
```

## 2. 市場を生成する

```powershell
uv run --locked synthetic-funnel --run-dir $run --progress
Get-ChildItem "$run/output" -Name
```

`--run-dir` は `input/config.json` を読み、同じ場所に `output/` を作ります。入力例では以下の構成になります。

```text
runs/quickstart-<日時>/
├── input/
│   └── config.json
└── output/
    ├── dataset.sqlite.db
    └── manifest.json
```

出力先が存在すると停止します。同じ条件で再実行するには、新しい実行ディレクトリに `input/config.json` を置きます。`--format parquet` または `--format csv` で形式を指定できます。`--seeker-arrivals` と `--job-arrivals` で1日あたりの流入数を上書きできます。市場のルールとスコアの学習方法は[継続市場のシミュレーション](MARKET.md)を参照してください。

### 複数 seed の並列生成（issue #40）

評価用に複数 seed を作る場合は `--seeds` を使います。seed ごとに別プロセスが走り、
`<出力先>/seed-<seed>/` に完全なデータセットが作られます。並列数は既定で CPU コア数
（seed 数が少なければその数）で、`--jobs` で上限を変更できます。

```powershell
uv run --locked synthetic-funnel --config "$run/input/config.json" --output "$run/seeds" --seeds 71,72,73,74,75 --jobs 5
```

各 seed の出力は同じ seed の単独実行（`--seed 71`）とバイト単位で同一です。乱数状態・
warmup モデル・出力先はプロセス間で共有されません。評価 seed はストレージ制約から
3〜5 本が推奨です（[コンペ仕様](COMPETITION.md) §3.4 / §5.5）。

## 3. SQLiteで紹介件数と観測済み結果を確認する

```powershell
@'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

db = Path(sys.argv[1]).resolve()
with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as connection:
    totals = connection.execute("""
        SELECT (SELECT COUNT(*) FROM events),
               (SELECT COUNT(oubo_dt) FROM events),
               (SELECT COUNT(shorui_tsuuka_dt) FROM events),
               (SELECT COUNT(kettei_dt) FROM events),
               (SELECT COUNT(*) FROM user),
               (SELECT COUNT(*) FROM item)
    """).fetchone()
    print(totals)
'@ | uv run --locked python - "$run/output/dataset.sqlite.db"
```

順に紹介、応募、書類通過、決定、参加求職者、掲載求人の件数が表示されます。紹介件数は流入数と生成日数で決まり、後段の件数は市場の退出と観測締切によって減ります。

## スコアとマスターを確認する

`events` の `model_apply_score` などのスコア列は、市場内ウォームアップ学習のモデルが付与します。既定の `warmup` は365日のため、90日などの短い市場ではスコア列がすべてNULLになります。スコアを含めて試すには、`"days": 500` のように warmup より長く生成します。warmup より前の紹介はスコアがNULLのまま出力に含まれます。

求職者と求人の属性は `user` と `item` のマスターにあります。`events` には属性列がなく、スキル由来の `skill_match` は `events_latent` にあるため、次のように結合して確認します。

```powershell
@'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

db = Path(sys.argv[1]).resolve()
with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as connection:
    rows = connection.execute("""
        SELECT e.event_id, u.user_skill, i.required_skill, l.skill_match
        FROM events AS e
        JOIN events_latent AS l USING (event_id)
        JOIN user AS u USING (user_id)
        JOIN item AS i USING (item_id)
        LIMIT 5
    """).fetchall()
    for row in rows:
        print(row)
'@ | uv run --locked python - "$run/output/dataset.sqlite.db"
```

`events_latent` には退出や観測締切を無視した真の確率と潜在到達日時が入ります。観測された到達率が真の確率より低くなるのは、市場の退出と締切の影響です。[出力データ仕様](DATA_FORMAT.md)

## manifestで生成条件を確認する

`manifest.json` には市場の設定、`market_start` と観測終了日時、`training_cutoff` と `warmup_end`、テーブルごとの件数、生成時間、出力ファイルのSHA-256が入ります。同じ条件を再現するときは、設定・seedと使用したコード・依存環境を揃えます。

任意のパスに出すときは `--output` と `--config` を使えます。

```powershell
uv run --locked synthetic-funnel --config "$run/input/config.json" --output "$run/parquet" --format parquet
```
