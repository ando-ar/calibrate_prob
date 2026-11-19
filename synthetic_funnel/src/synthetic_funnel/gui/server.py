"""Local web UI for generating and inspecting synthetic funnel datasets."""

import csv
import html
import json
import sqlite3
import threading
import webbrowser
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from urllib.parse import parse_qs, urlparse

from ..export import FORMATS, SQLITE_FILENAME
from .runner import CommandResult, Runner, cli_command, subprocess_runner

SAMPLE_ROWS = 100
STAGE_COLUMNS = ("oubo_dt", "shorui_tsuuka_dt", "kettei_dt")
STAGE_LABELS = ("応募", "書類通過", "決定")
TABS = ("market", "results")
FORM_KEYS = {
    "market": ("seed", "seeker_arrivals", "job_arrivals", "days", "warmup", "application_days",
               "posting_days", "referrals", "training_window_days", "training_referrals_per_day",
               "negative_reservoir_size", "market_start", "format", "advanced_config"),
}
DEFAULT_VALUES = {
    "market": {
        "seed": "71", "seeker_arrivals": "8", "job_arrivals": "8", "days": "90", "warmup": "365",
        "application_days": "20", "posting_days": "40", "referrals": "5",
        "training_window_days": "", "training_referrals_per_day": "", "negative_reservoir_size": "",
        "market_start": "2025-01-01T00:00:00+09:00", "format": "sqlite", "advanced_config": "{}",
    },
}
# (label, minimum, maximum). None means no bound.
MARKET_INT_FIELDS = {
    "seed": ("seed", None, None), "seeker_arrivals": ("seeker_arrivals", 0, None),
    "job_arrivals": ("job_arrivals", 0, None),
    "days": ("days", 1, None), "warmup": ("warmup", 0, None),
    "application_days": ("application_days", 1, None), "posting_days": ("posting_days", 1, None),
    "referrals": ("referrals", 0, 5),
}
# Optional warmup-training fields: blank means "use the MarketConfig default".
MARKET_OPTIONAL_INT_FIELDS = {
    "training_referrals_per_day": ("warmup学習の1日あたり紹介数", 1),
    "negative_reservoir_size": ("負例プールのサイズ", 1),
}
TRAINING_WINDOW_HINT = "training_window_days は1以上の整数をカンマ区切りで3つ指定してください"
MARKET_COUNT_LABELS = {
    "user": "求職者（user）", "item": "求人（item）", "events": "紹介（events）",
    "events_latent": "events_latent（反実仮）", "user_latent": "求職者の意欲（user_latent）",
    "item_latent": "求人の意欲（item_latent）", "daily": "市場日（daily）",
}
ERROR_HINTS = (
    ("isoformat", "日時は ISO 8601 形式で指定してください（例: 2024-01-01T00:00:00+09:00）。"),
    ("must be a JSON object", "configはJSONオブジェクトで指定してください。"),
    ("market_start", "market_start を ISO 8601 形式の文字列で指定してください。"),
    ("Removed config keys", "廃止された設定キーが含まれています。MarketConfig の項目だけを指定してください。"),
    ("Arrivals must be nonnegative integers", "流入数は0以上の整数で指定してください。"),
    ("Durations must be positive integers", "日数は1以上の整数で指定してください。"),
    ("warmup must be a nonnegative integer", "warmup は0以上の整数で指定してください。"),
    ("Daily referrals", "referrals は0〜5の整数で指定してください。"),
    ("training_window_days requires three positive integers", TRAINING_WINDOW_HINT + "。"),
    ("Training density and reservoir size must be positive integers",
     "warmup学習の紹介数と負例プールサイズは1以上の整数で指定してください。"),
)


@dataclass(frozen=True)
class GuiState:
    """Fixed locations and command runner for one GUI server."""

    base: Path
    runner: Runner

    @property
    def runs_dir(self) -> Path:
        return self.base / "runs" / "gui"


def resolve_base() -> Path:
    """Pick the project directory that holds runs and package sources."""
    current = Path.cwd()
    if (current / "pyproject.toml").is_file() and (current / "src" / "synthetic_funnel").is_dir():
        return current
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class StepResult:
    """One executed step, ready to render."""

    title: str
    command: str
    result: CommandResult
    run_id: str = ""
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.result.returncode == 0


def parse_int(label: str, text: str) -> tuple[int | None, str]:
    """Parse one integer form field."""
    stripped = (text or "").strip()
    try:
        return int(stripped), ""
    except ValueError:
        return None, f"{label}は整数で指定してください: {stripped!r}"


def parse_advanced(form: dict[str, str]) -> tuple[dict | None, str]:
    """Parse the advanced JSON config textarea."""
    text = form.get("advanced_config", "").strip()
    if not text:
        return {}, ""
    try:
        values = json.loads(text)
    except ValueError as error:
        return None, f"上級者向けconfigがJSONとして読めません: {error}"
    if not isinstance(values, dict):
        return None, "上級者向けconfigはJSONオブジェクトで指定してください"
    return values, ""


def collect_integers(values: dict, errors: list[str], form: dict[str, str], fields: dict) -> None:
    """Fill required integer form fields into values, recording validation errors."""
    for name, (label, minimum, maximum) in fields.items():
        value, error = parse_int(label, form.get(name, ""))
        if not error and minimum is not None and value < minimum:
            error = f"{label}は{minimum}以上の整数で指定してください"
        if not error and maximum is not None and value > maximum:
            error = f"{label}は{maximum}以下の整数で指定してください"
        if error:
            errors.append(error)
        else:
            values[name] = value


def collect_optional_integers(values: dict, errors: list[str], form: dict[str, str], fields: dict) -> None:
    """Fill optional positive-integer fields; blank fields are left out so defaults apply."""
    for name, (label, minimum) in fields.items():
        text = (form.get(name, "") or "").strip()
        if not text:
            continue
        value, error = parse_int(label, text)
        if not error and value < minimum:
            error = f"{label}は{minimum}以上の整数で指定してください"
        if error:
            errors.append(error)
        else:
            values[name] = value


def collect_training_window(values: dict, errors: list[str], form: dict[str, str]) -> None:
    """Parse the comma-separated training_window_days field (three positive ints)."""
    text = (form.get("training_window_days", "") or "").strip()
    if not text:
        return
    parts = [part.strip() for part in text.split(",")]
    numbers: list[int] = []
    for part in parts:
        value, error = parse_int("training_window_days", part)
        if error or value < 1:
            errors.append(TRAINING_WINDOW_HINT)
            return
        numbers.append(value)
    if len(numbers) != 3:
        errors.append(TRAINING_WINDOW_HINT)
        return
    values["training_window_days"] = numbers


def parse_market_start(form: dict[str, str], errors: list[str]) -> str | None:
    """Validate that market_start is an ISO 8601 datetime carrying a UTC offset."""
    text = (form.get("market_start", "") or "").strip()
    if not text:
        errors.append("market_start を入力してください")
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        errors.append("market_start は UTC オフセット付きの ISO 8601 日時で指定してください"
                      "（例: 2025-01-01T00:00:00+09:00）")
        return None
    if parsed.utcoffset() is None:
        errors.append("market_start には UTC オフセット（例: +09:00）を含めてください")
        return None
    return text


def build_market_values(form: dict[str, str]) -> tuple[dict | None, str]:
    """Turn market-tab form values into a market config JSON object."""
    values: dict = {}
    errors: list[str] = []
    collect_integers(values, errors, form, MARKET_INT_FIELDS)
    collect_optional_integers(values, errors, form, MARKET_OPTIONAL_INT_FIELDS)
    collect_training_window(values, errors, form)
    market_start = parse_market_start(form, errors)
    if market_start is not None:
        values["market_start"] = market_start
    advanced, error = parse_advanced(form)
    if error:
        errors.append(error)
    values = {**(advanced or {}), **values}
    if errors:
        return None, "\n".join(errors)
    return values, ""


def output_format(form: dict[str, str]) -> str:
    """Resolve the requested output format."""
    fmt = form.get("format", "sqlite")
    return fmt if fmt in FORMATS else "sqlite"


def format_options() -> dict[str, str]:
    """Render the output format choices."""
    return {fmt: fmt for fmt in FORMATS}


def new_run_dir(runs_dir: Path) -> Path:
    """Create a fresh timestamped run directory under the GUI runs directory."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S%f")
    run_dir = runs_dir / stamp
    index = 1
    while run_dir.exists():
        run_dir = runs_dir / f"{stamp}-{index}"
        index += 1
    return run_dir


def error_hint(stderr: str) -> str:
    """Return a Japanese hint for a known CLI failure pattern."""
    for needle, hint in ERROR_HINTS:
        if needle in stderr:
            return hint
    return ""


def run_generation(state: GuiState, form: dict[str, str]) -> StepResult:
    """Write input/config.json into a new run directory and run the market CLI."""
    title = "市場データ生成"
    values, error = build_market_values(form)
    if error:
        detail = f'<p class="hint">{html.escape(error)}</p>'
        return StepResult(title, "（実行せずに中断）", CommandResult([], 2, "", ""), detail=detail)
    fmt = output_format(form)
    run_dir = new_run_dir(state.runs_dir)
    (run_dir / "input").mkdir(parents=True)
    (run_dir / "input" / "config.json").write_text(
        json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Arrivals are passed as CLI overrides: they take precedence over whatever
    # is stored in input/config.json (which still keeps a reproducible copy).
    argv = ["--run-dir", str(run_dir), "--progress", "--format", fmt,
            "--seeker-arrivals", str(values["seeker_arrivals"]),
            "--job-arrivals", str(values["job_arrivals"])]
    command = cli_command(argv)
    result = state.runner(command)
    detail = ""
    if result.returncode == 0:
        detail = success_detail(run_dir, title)
    else:
        hint = error_hint(result.stderr)
        if hint:
            detail = f'<p class="hint">{html.escape(hint)}</p>'
    return StepResult(title, " ".join(command), result, run_id=run_dir.name, detail=detail)


def open_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing database without creating or modifying it."""
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def sqlite_objects(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """List table and view names in the database."""
    return connection.execute(
        "SELECT name, type FROM sqlite_schema "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()


def render_cell(value: object) -> str:
    """Render one table cell with NULL marked."""
    return html.escape("NULL" if value is None else str(value))


def html_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Render an escaped HTML table."""
    header = "".join(f"<th>{html.escape(str(value))}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{render_cell(value)}</td>" for value in row) + "</tr>" for row in rows
    )
    return f'<div class="table-wrap"><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>'


def sqlite_table_blocks(path: Path) -> str:
    """Render per-table column definitions, counts and bounded samples."""
    blocks = []
    with closing(open_readonly(path)) as connection:
        for name, kind in sqlite_objects(connection):
            identifier = '"' + name.replace('"', '""') + '"'
            columns = connection.execute(f"PRAGMA table_info({identifier})").fetchall()
            count = connection.execute(f"SELECT COUNT(*) FROM {identifier}").fetchone()[0]
            rows = connection.execute(f"SELECT * FROM {identifier} LIMIT {SAMPLE_ROWS}").fetchall()
            blocks.append(
                f'<details><summary>{html.escape(name)}（{html.escape(kind)}） — {count:,} 件</summary>'
                + html_table(["列名", "宣言型"], [(column[1], column[2] or "未宣言") for column in columns])
                + f"<p>サンプル {len(rows)} 件（最大 {SAMPLE_ROWS} 件、順序未指定）。NULLは欠損値です。</p>"
                + html_table([column[1] for column in columns], rows)
                + "</details>"
            )
    return "".join(blocks) or "<p>テーブル・ビューがありません。</p>"


def csv_rows(path: Path) -> tuple[str, int, list[str], list[list[str]]]:
    """Read a CSV table: name, total rows, header and bounded sample rows."""
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream)
        header = next(reader, [])
        rows = []
        total = 0
        for row in reader:
            total += 1
            if len(rows) < SAMPLE_ROWS:
                rows.append(row)
    return path.name, total, header, rows


def parquet_rows(path: Path) -> tuple[str, int, list[str], list[list]]:
    """Read a Parquet table: name, total rows, column names and bounded sample rows."""
    from pyarrow import parquet

    table = parquet.read_table(path)
    sliced = table.slice(0, SAMPLE_ROWS)
    rows = [[column[index].as_py() for column in sliced.columns] for index in range(sliced.num_rows)]
    return path.name, table.num_rows, list(table.column_names), rows


def file_table_blocks(output: Path, suffix: str) -> str:
    """Render per-file samples for CSV or Parquet datasets."""
    readers = {"csv": csv_rows, "parquet": parquet_rows}
    blocks = []
    for path in sorted(output.glob(f"*.{suffix}")):
        try:
            name, count, header, rows = readers[suffix](path)
        except (OSError, ValueError, StopIteration) as error:
            blocks.append(f"<p>{html.escape(path.name)} を読み取れませんでした: {html.escape(str(error))}</p>")
            continue
        blocks.append(
            f"<details><summary>{html.escape(name)} — {count:,} 件</summary>"
            + f"<p>サンプル {len(rows)} 件（最大 {SAMPLE_ROWS} 件）。</p>"
            + html_table(header, rows)
            + "</details>"
        )
    return "".join(blocks) or f"<p>*.{suffix} ファイルがありません。</p>"


def events_file_counts(path: Path, suffix: str) -> tuple[int, list[int]]:
    """Count rows and observed arrivals by scanning one events CSV or Parquet file."""
    if suffix == "csv":
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream)
            header = next(reader, [])
            indexes = [header.index(column) for column in STAGE_COLUMNS]
            total = 0
            arrivals = [0, 0, 0]
            for row in reader:
                total += 1
                for position, index in enumerate(indexes):
                    if index < len(row) and row[index].strip():
                        arrivals[position] += 1
        return total, arrivals
    from pyarrow import parquet

    table = parquet.read_table(path)
    return table.num_rows, [table.num_rows - table.column(column).null_count for column in STAGE_COLUMNS]


def funnel_event_counts(output: Path) -> dict | None:
    """Count referrals and observed arrivals from the events table in any format."""
    sqlite_path = output / SQLITE_FILENAME
    try:
        if sqlite_path.is_file():
            with closing(open_readonly(sqlite_path)) as connection:
                query = (
                    f"SELECT COUNT(*), COUNT({STAGE_COLUMNS[0]}), "
                    f"COUNT({STAGE_COLUMNS[1]}), COUNT({STAGE_COLUMNS[2]}) FROM events"
                )
                total, *arrivals = connection.execute(query).fetchone()
        else:
            for suffix in ("csv", "parquet"):
                path = output / f"events.{suffix}"
                if path.is_file():
                    total, arrivals = events_file_counts(path, suffix)
                    break
            else:
                return None
        return {"total": total, "arrivals": list(arrivals)}
    except (OSError, ValueError, sqlite3.Error, KeyError):
        return None


def read_optional(path: Path) -> str:
    """Read a text file, or return an empty string when it does not exist."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def run_manifest(run_dir: Path) -> dict | None:
    """Load the manifest of a run, or None when missing or broken."""
    try:
        value = json.loads((run_dir / "output" / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def run_input_config(run_dir: Path) -> dict | None:
    """Load the input config of a run, or None when missing or broken."""
    try:
        value = json.loads((run_dir / "input" / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def run_status(run_dir: Path) -> str:
    """Classify a run as 成功 / 失敗 / 未実行 from its output directory."""
    if (run_dir / "output" / "manifest.json").is_file():
        return "成功"
    if (run_dir / "output").is_dir():
        return "失敗"
    return "未実行"


def counts_table(counts: dict) -> str:
    """Render funnel event counts."""
    rows = [("紹介（events の行数）", f"{counts['total']:,}")]
    rows.extend(
        (label, f"{count:,}") for label, count in zip(STAGE_LABELS, counts["arrivals"], strict=True)
    )
    return html_table(("件数", "値"), rows)


def market_counts_table(manifest: dict) -> str:
    """Render every row-count entry recorded in a market manifest (no fixed table list)."""
    row_counts = manifest.get("row_counts") or {}
    if not isinstance(row_counts, dict) or not row_counts:
        return "<p>row_counts がありません。</p>"
    rows = []
    for key in sorted(row_counts):
        count = row_counts[key]
        label = MARKET_COUNT_LABELS.get(key, key)
        value = f"{count:,}" if isinstance(count, int) else html.escape(str(count))
        rows.append((label, value))
    return html_table(("テーブル", "件数"), rows)


def manifest_highlights(manifest: dict) -> str:
    """Render the manifest fields most useful to an operator, kept lightweight."""
    rows: list[tuple[str, str]] = []

    def add(label: str, value: object) -> None:
        rows.append((label, "—" if value in (None, "") else html.escape(str(value))))

    add("モード（mode）", manifest.get("mode"))
    add("市場開始（market_start）", manifest.get("market_start"))
    add("観測終了（observation_end_exclusive）", manifest.get("observation_end_exclusive"))
    training = manifest.get("training")
    if not isinstance(training, dict):
        training = {}
    for key, label in (("method", "学習手法（method）"), ("warmup", "warmup"),
                       ("warmup_end", "warmup_end"), ("training_cutoff", "training_cutoff"),
                       ("status", "学習ステータス（status）")):
        add(label, training.get(key))
    seconds = manifest.get("generation_seconds")
    add("生成時間（generation_seconds）", f"{seconds:.3f} 秒" if isinstance(seconds, (int, float)) else seconds)
    return html_table(("項目", "値"), rows)


def dataset_content(manifest: dict, output: Path) -> str:
    """Render per-table contents for any output format."""
    fmt = manifest.get("output_format", "sqlite")
    if fmt == "sqlite":
        path = output / SQLITE_FILENAME
        if not path.is_file():
            return f"<p>{html.escape(SQLITE_FILENAME)} が見つかりません。</p>"
        return sqlite_table_blocks(path)
    if fmt == "csv":
        return file_table_blocks(output, "csv")
    if fmt == "parquet":
        return file_table_blocks(output, "parquet")
    return f"<p>未知の出力形式: {html.escape(str(fmt))}</p>"


def pre_block(title: str, text: str, empty_note: str = "見つかりません。") -> str:
    """Render a labeled <pre> block for a file's text content."""
    body = html.escape(text) if text else html.escape(empty_note)
    return f"<h3>{html.escape(title)}</h3><pre>{body}</pre>"


def success_detail(run_dir: Path, title: str) -> str:
    """Render the counts and a link shown after a successful generation."""
    manifest = run_manifest(run_dir)
    counts = funnel_event_counts(run_dir / "output")
    link = (
        f'<p><a href="/?tab=results&amp;run={html.escape(run_dir.name, quote=True)}">'
        f"結果確認タブでテーブル内容とmanifestを詳しく見る</a></p>"
    )
    summary = ""
    if manifest is not None:
        summary += counts_table(counts) if counts is not None else "<p>events の件数を読み取れませんでした。</p>"
        summary += market_counts_table(manifest)
        summary += (
            "<p>観測された到達は市場の退出と観測締切の影響を受けます。退出を無視した真の確率は "
            "events_latent の p_*、観測値は events の日時列を確認してください。</p>"
        )
    return f"<p>{html.escape(title)}が完了しました。出力先: <code>{html.escape(str(run_dir / 'output'))}</code></p>{summary}{link}"


def run_entries(state: GuiState) -> list[dict]:
    """List GUI runs with status and headline counts."""
    entries = []
    if state.runs_dir.is_dir():
        for path in sorted((item for item in state.runs_dir.iterdir() if item.is_dir()), reverse=True):
            manifest = run_manifest(path)
            rows = (manifest.get("row_counts") or {}).get("events") if manifest else None
            entries.append({
                "id": path.name, "kind": "市場",
                "status": run_status(path), "rows": f"{rows:,}" if isinstance(rows, int) else "—",
            })
    return entries


def render_run_list(state: GuiState) -> str:
    """Render the table of past runs with links to their details."""
    entries = run_entries(state)
    if not entries:
        return "<p>まだ実行がありません。「市場」タブから実行してください。</p>"
    header = "".join(f"<th>{label}</th>" for label in ("run ID", "種別", "状態", "紹介件数", "操作"))
    body = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape(str(entry[key]))}</td>" for key in ("id", "kind", "status", "rows"))
        + f'<td><a href="/?tab=results&amp;run={html.escape(entry["id"], quote=True)}">詳細</a></td>'
        + "</tr>"
        for entry in entries
    )
    return f'<div class="table-wrap"><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>'


def valid_run_id(run_id: str) -> bool:
    """Check that a run id is a plain directory name."""
    return bool(run_id) and run_id not in (".", "..") and "/" not in run_id and "\\" not in run_id


def render_run_detail(state: GuiState, run_id: str) -> str:
    """Render one run's summary, counts, table contents, manifest and input config."""
    run_dir = state.runs_dir / run_id
    if not valid_run_id(run_id) or not run_dir.is_dir():
        return f'<p class="banner">run が見つかりません: {html.escape(run_id)}</p>'
    status = run_status(run_dir)
    output = run_dir / "output"
    manifest = run_manifest(run_dir)
    sections = [
        f'<p><a href="/?tab=results">実行一覧に戻る</a></p>'
        f"<h2>run {html.escape(run_id)}</h2>"
        f"<p>状態: <strong>{html.escape(status)}</strong> ／ "
        f"出力先: <code>{html.escape(str(output))}</code></p>"
    ]
    if status != "成功":
        sections.append(
            "<p>このrunにはmanifestがありません。入力configを修正して「市場」タブから新しいrunとして実行してください。"
            "失敗の原因は実行結果に表示されたCLIエラーを確認してください。</p>"
        )
    if manifest is not None:
        sections.append(
            f'<section class="card"><h3>manifest ハイライト</h3>{manifest_highlights(manifest)}</section>'
        )
        sections.append('<section class="card"><h3>件数サマリ</h3>')
        counts = funnel_event_counts(output)
        sections.append(counts_table(counts) if counts is not None else "<p>events の件数を読み取れませんでした。</p>")
        sections.append(market_counts_table(manifest))
        sections.append(
            "<p>events の日時列は市場の退出と観測締切の影響を受けた到達だけが入り、events_latent の p_* は"
            "退出を無視した反実仮の確率です。両者が一致しないのは市場の影響です。</p>"
        )
        sections.append("</section>")
        sections.append(f'<section class="card"><h3>テーブル内容</h3>{dataset_content(manifest, output)}</section>')
        sections.append(
            '<section class="card"><h3>manifest.json</h3><pre>'
            f"{html.escape(json.dumps(manifest, ensure_ascii=False, indent=2))}</pre></section>"
        )
    input_config = read_optional(run_dir / "input" / "config.json")
    sections.append(
        f'<section class="card"><h3>input/config.json</h3><pre>{html.escape(input_config) or html.escape("見つかりません。")}</pre>'
        "<p>このconfigを新しいrunディレクトリの input/config.json に置けば、CLIで同じ条件を再現できます。</p></section>"
    )
    return "".join(sections)


def field_text(name: str, label: str, value: str, hint: str = "", input_type: str = "text") -> str:
    """Render one single-line form field."""
    escaped = html.escape(value, quote=True)
    return (
        f'<label class="field"><span>{html.escape(label)}</span>'
        f'<input type="{input_type}" name="{name}" value="{escaped}" placeholder="{html.escape(hint, quote=True)}">'
        f"</label>"
    )


def field_select(name: str, label: str, value: str, options: dict[str, str]) -> str:
    """Render one select form field."""
    choices = "".join(
        f'<option value="{html.escape(key, quote=True)}"'
        + (" selected" if key == value else "")
        + f">{html.escape(option)}</option>"
        for key, option in options.items()
    )
    return (
        f'<label class="field"><span>{html.escape(label)}</span>'
        f'<select name="{name}">{choices}</select></label>'
    )


def field_textarea(name: str, label: str, value: str, rows: int, hint: str = "") -> str:
    """Render one textarea form field."""
    note = f'<span class="hint">{html.escape(hint)}</span>' if hint else ""
    return (
        f'<label class="field"><span>{html.escape(label)}</span>{note}'
        f'<textarea name="{name}" rows="{rows}" spellcheck="false">{html.escape(value)}</textarea>'
        f"</label>"
    )


def render_market_form(values: dict[str, str]) -> str:
    """Render the market-tab form fields."""
    return (
        field_text("seed", "seed", values["seed"], "71", "number")
        + field_text("seeker_arrivals", "1日の求職者流入（seeker_arrivals / --seeker-arrivals）",
                     values["seeker_arrivals"], "8", "number")
        + field_text("job_arrivals", "1日の求人流入（job_arrivals / --job-arrivals）",
                     values["job_arrivals"], "8", "number")
        + '<p class="hint">流入数はCLIの <code>--seeker-arrivals</code> / <code>--job-arrivals</code> '
          'で設定ファイルの値を上書きして渡します。</p>'
        + field_text("days", "生成日数（days）", values["days"], "90", "number")
        + field_text("warmup", "ウォームアップ日数（warmup）", values["warmup"], "365", "number")
        + field_text("application_days", "応募可能日数（application_days）", values["application_days"], "20", "number")
        + field_text("posting_days", "掲載日数（posting_days）", values["posting_days"], "40", "number")
        + field_text("referrals", "1人あたりの1日紹介上限（referrals、0〜5）", values["referrals"], "5", "number")
        + field_text("training_window_days", "学習ウィンドウ日数（training_window_days、省略可）",
                     values["training_window_days"], "1,30,365")
        + field_text("training_referrals_per_day", "warmup学習の1日紹介数（training_referrals_per_day、省略可）",
                     values["training_referrals_per_day"], "10000", "number")
        + field_text("negative_reservoir_size", "負例プールサイズ（negative_reservoir_size、省略可）",
                     values["negative_reservoir_size"], "100000", "number")
        + '<p class="hint">学習設定を空欄にすると MarketConfig の既定値を使います。'
          'warmup が生成日数以上の場合は学習されず全スコアがNULLになります（エラーではありません）。</p>'
        + field_text("market_start", "市場開始（market_start、UTCオフセット付きISO 8601）",
                     values["market_start"], "2025-01-01T00:00:00+09:00")
        + field_select("format", "出力形式", values["format"], format_options())
        + field_textarea("advanced_config", "上級者向けconfig（JSON、省略可）", values["advanced_config"], 4,
                         "MarketConfig の項目のみ上書きできます。フォームの値とCLIフラグが優先されます。")
        + '<p>実行先: <code>runs/gui/&lt;タイムスタンプ&gt;</code>（自動作成。既存の出力は上書きしません）</p>'
    )


def render_page(
    state: GuiState,
    template: Template,
    *,
    tab: str,
    values: dict[str, str],
    market_step: StepResult | None = None,
    results_html: str = "",
    banner: str = "",
) -> str:
    """Render the full page for the active tab."""
    if tab not in TABS:
        tab = "market"
    nav = "".join(
        f'<a href="/?tab={name}" class="{"active" if tab == name else "inactive"}">{label}</a>'
        for name, label in zip(TABS, ("市場", "結果確認"), strict=True)
    )
    return template.substitute(
        nav=nav,
        market_tab_class="active" if tab == "market" else "hidden",
        results_tab_class="active" if tab == "results" else "hidden",
        banner=html.escape(banner),
        market_form=render_market_form(values if tab == "market" else default_values("market")),
        market_result=render_step(market_step) if market_step else "",
        results_content=results_html,
        submit_script=SUBMIT_SCRIPT,
    )


def render_step(step: StepResult) -> str:
    """Render one executed step as HTML."""
    status = "成功" if step.succeeded else f"失敗 (exit code {step.result.returncode})"
    captured = ""
    if step.result.stdout:
        captured += f"<h3>標準出力</h3><pre>{html.escape(step.result.stdout)}</pre>"
    if step.result.stderr:
        captured += f"<h3>標準エラー</h3><pre>{html.escape(step.result.stderr)}</pre>"
    captured_block = f'<details><summary>CLIの出力を見る</summary>{captured}</details>' if captured else ""
    return (
        f'<section class="card result"><h2>{html.escape(step.title)}の結果</h2>'
        f'<p class="status {"ok" if step.succeeded else "fail"}">{status}</p>'
        f"<p>実行コマンド</p><pre>{html.escape(step.command)}</pre>"
        f"{captured_block}{step.detail}</section>"
    )


SUBMIT_SCRIPT = """document.querySelectorAll('form').forEach(function (form) {
  form.addEventListener('submit', function () {
    var button = form.querySelector('button[type="submit"]');
    if (button) { button.disabled = true; button.textContent = '実行中…'; }
  });
});"""


def load_template() -> Template:
    """Load the page template shipped next to this module."""
    return Template(Path(__file__).with_name("template.html").read_text(encoding="utf-8"))


def default_values(tab: str) -> dict[str, str]:
    """Pre-fill form fields with the light-trial defaults."""
    return dict(DEFAULT_VALUES.get(tab, DEFAULT_VALUES["market"]))


def build_handler(state: GuiState, template: Template) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to the GUI state."""

    class GuiHandler(BaseHTTPRequestHandler):
        """Serve the GUI page and run generation on POST."""

        def do_GET(self) -> None:
            query = parse_qs(urlparse(self.path).query)
            tab = query.get("tab", ["market"])[0]
            if tab not in TABS:
                tab = "market"
            results_html = ""
            if tab == "results":
                run_id = query.get("run", [""])[0]
                results_html = render_run_detail(state, run_id) if run_id else render_run_list(state)
            page = render_page(state, template, tab=tab, values=default_values(tab), results_html=results_html)
            self.respond(page, 200)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            form = {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}
            if form.get("action", "") != "market":
                page = render_page(state, template, tab="market", values=default_values("market"),
                                   banner=f"未知のアクション: {form.get('action', '')}")
                self.respond(page, 400)
                return
            tab = "market"
            keys = FORM_KEYS[tab]
            values = {**default_values(tab), **{key: value for key, value in form.items() if key in keys}}
            step = run_generation(state, form)
            page = render_page(state, template, tab=tab, values=values, market_step=step)
            self.respond(page, 200)

        def respond(self, page: str, status: int) -> None:
            payload = page.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return GuiHandler


def create_server(base: Path, runner: Runner, port: int = 8000) -> ThreadingHTTPServer:
    """Create the GUI HTTP server, falling back to a free port."""
    state = GuiState(base=base, runner=runner)
    handler = build_handler(state, load_template())
    try:
        return ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        return ThreadingHTTPServer(("127.0.0.1", 0), handler)


def serve(base: Path) -> None:
    """Start the GUI server and open a browser."""
    server = create_server(base, subprocess_runner)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"synthetic funnel GUI: {url}")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
