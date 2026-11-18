"""Read-only inspection of the demo's fixed synthetic SQLite database."""

from __future__ import annotations

import html
import sqlite3
from contextlib import closing
from pathlib import Path

SAMPLE_ROWS = 5
PREPARE_HELP = (
    'DBが無い場合は、GitHub Actionsの calibration-pipeline-fixture artifact（保持期間14日）を '
    'examples/sqlite/ に展開するか、CLIでは calibration_pipeline/ で '
    'uv run --locked python examples/sqlite/prepare_example.py を実行します。'
    '生成には同じリポジトリの synthetic_funnel/ が必要です。'
)


def open_fixture(path: Path) -> sqlite3.Connection:
    """Open an existing database without creating or modifying it."""
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def fixture_error(path: Path) -> str | None:
    """Return actionable diagnostics when the fixed fixture cannot be read."""
    try:
        with closing(open_fixture(path)) as connection:
            connection.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchall()
    except (OSError, sqlite3.Error) as error:
        return f"固定fixture DBを読み取れません: {path} ({error})。パスと読み取り権限を確認してください。{PREPARE_HELP}"
    return None


def _table(headers: list[str], rows: list[tuple]) -> str:
    header = "".join(f"<th>{html.escape(value)}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(
            f"<td>{html.escape('NULL' if value is None else str(value))}</td>"
            for value in row
        ) + "</tr>" for row in rows
    )
    return f'<div class="table-wrap"><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>'


def inspect_fixture(path: Path) -> str:
    """Render table/view names, declared column types, counts and bounded samples."""
    try:
        with closing(open_fixture(path)) as connection:
            connection.execute("BEGIN")
            objects = connection.execute(
                "SELECT name, type FROM sqlite_schema "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            blocks = []
            for name, kind in objects:
                identifier = '"' + name.replace('"', '""') + '"'
                columns = connection.execute(f"PRAGMA table_info({identifier})").fetchall()
                count = connection.execute(f"SELECT COUNT(*) FROM {identifier}").fetchone()[0]
                rows = connection.execute(f"SELECT * FROM {identifier} LIMIT ?", (SAMPLE_ROWS,)).fetchall()
                blocks.append(
                    f'<details><summary>{html.escape(name)} ({kind}) — {count} 件</summary>'
                    + _table(["列名", "宣言型"], [(col[1], col[2] or "未宣言（ビュー等）") for col in columns])
                    + f"<p>サンプル {len(rows)} 件（最大 {SAMPLE_ROWS} 件、順序未指定）。NULLは欠損値です。</p>"
                    + _table([col[1] for col in columns], rows) + "</details>"
                )
            return "".join(blocks) or f"<p>テーブル・ビューがありません。{PREPARE_HELP}</p>"
    except (OSError, sqlite3.Error) as error:
        return f'<p class="banner">固定fixture DBを読み取れません。パスと読み取り権限を確認してください。{html.escape(str(error))}。{PREPARE_HELP}</p>'
