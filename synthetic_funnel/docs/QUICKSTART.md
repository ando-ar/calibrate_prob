# クイックスタート

Python 3.11以上と `uv` を使います。PowerShellでリポジトリ内の `synthetic_funnel/` に移動してから実行してください。

## 1. 実行ディレクトリを作る

以下では500件の紹介を生成します。元モデルの学習を省く設定にして、生成から出力確認までを短時間で試せるようにします。

```powershell
uv sync --locked

$run = "runs/quickstart-$(Get-Date -Format 'yyyyMMdd-HHmmssfff')"
New-Item -ItemType Directory -Path "$run/input" | Out-Null

@'
{
  "rows": 500,
  "seed": 42,
  "reference_rows": 1000,
  "legacy_scores": {"enabled": false}
}
'@ | Set-Content -Encoding ascii "$run/input/config.json"
```

## 2. 履歴を生成する

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

出力先が存在すると停止します。同じ条件で再実行するには、新しい実行ディレクトリに `input/config.json` を置きます。`--format parquet` または `--format csv` で形式を指定できます。

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
        SELECT COUNT(*), COUNT(oubo_dt), COUNT(shorui_tsuuka_dt), COUNT(kettei_dt)
        FROM events
    """).fetchone()
    print(totals)
'@ | uv run --locked python - "$run/output/dataset.sqlite.db"
```

順に紹介、応募、書類通過、決定の件数が表示されます。紹介件数は500件で、後段の件数は標本と観測締切によって変わります。500件の例では決定が0件になることがあります。

## 元モデルスコアを含める

[`runs/example_run/input/config.json`](../runs/example_run/input/config.json) は、元モデルの学習も行う設定例です。出力件数を `--rows` で減らしても、元モデルの学習履歴は `legacy_scores` の設定で別に生成されます。扱いたい件数と、後段で必要な正例数に応じて設定します。[設定とPython API](API.md)

任意のパスに出すときは `--output` と `--config` を使えます。

```powershell
uv run --locked synthetic-funnel --config "$run/input/config.json" --output "$run/parquet" --format parquet
```
