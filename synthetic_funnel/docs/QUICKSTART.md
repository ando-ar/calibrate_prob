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

## GUIで試す

ブラウザーから条件を入力して生成し、SQLiteの中身や件数を確認できます。`synthetic_funnel/` で以下を実行すると、ブラウザーが自動で開きます（使用中のポートがある場合は別の空きポート）。

```powershell
uv run --locked python -m synthetic_funnel.gui
```

操作手順:

1. 「生成」タブで件数・seed・期間・学習用データ件数・`legacy_scores` の有無・出力形式を入力して実行します。既定値は500件の設定例で、`legacy_scores` は無効（出力が速い）です。
2. 「結果確認」タブで `runs/gui/<タイムスタンプ>/` の一覧からrunを選び、状態・出力先・件数サマリ・テーブルごとの列とサンプル行・manifest・入力configを確認します。閲覧は読み取り専用です。
3. 「市場」タブでは、既存の `legacy_models.json` を指定して継続市場シミュレーションを実行できます。`legacy_models.json` は「生成」タブで `legacy_scores` を有効にしたrunの `output/` にあります。

結果の見方:

- 生成はCLIと同じ `python -m synthetic_funnel --run-dir ...` を別プロセスで実行します。実行結果は `runs/gui/<タイムスタンプ>/input/config.json` と `output/`（データ + `manifest.json`）に保存され、既存の出力を上書きしません。`input/config.json` を新しい実行ディレクトリに置けばCLIでも同じ条件を再現できます。
- 紹介件数は設定した件数で、応募・書類通過・決定の件数は標本と観測締切によって減ります。「結果確認」タブの件数サマリには、観測された到達率と生成時の真の条件付き確率を並べて表示します。
- `events` の日時列は観測締切 `as_of` までに届いた到達だけが入り、`truth` の `p_*` は打ち切りを考えない真の確率です。観測された到達率が真の確率より低くなるのは締切の影響です。
- `legacy_scores` を有効にすると、出力件数とは別に元モデルの学習用データを `reference_rows` 件分生成します。扱いたい件数と、後段で必要な正例数に応じて設定します。
- 生成が失敗したときは、画面にCLIのエラーと日本語の補足が表示されます。件数・期間の順序・日時の形式・`reference_rows` が100以上であることを確認してください。
