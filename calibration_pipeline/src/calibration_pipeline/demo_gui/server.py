"""Local demo web UI for the calibration pipeline."""

from __future__ import annotations

import html
import json
import shutil
import sys
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template
from urllib.parse import parse_qs, urlparse

import polars as pl

from calibration_pipeline.demo_gui.runner import (
    CommandResult,
    Runner,
    cli_command,
    subprocess_runner,
)
from calibration_pipeline.source import DEFAULT_SQL_SEED, MAX_SQL_SEED

DEMO_EXPERIMENTS = "experiments/demo_gui"
MODEL_PLACEHOLDER = "sqlite_oubo_demo"
CSV_ROW_LIMIT = 100


@dataclass(frozen=True)
class DemoState:
    """Fixed locations and command runner for one demo server."""

    base: Path
    runner: Runner

    @property
    def examples(self) -> Path:
        return self.base / "examples" / "sqlite"

    @property
    def experiments_dir(self) -> Path:
        return self.base / DEMO_EXPERIMENTS


def resolve_base() -> Path:
    """Pick the project directory that holds examples and experiments."""
    current = Path.cwd()
    if (current / "examples" / "sqlite").is_dir():
        return current
    return Path(__file__).resolve().parents[3]


def successful_runs(
    experiments_dir: Path, stage: str, artifact: str
) -> list[tuple[str, str]]:
    """List (model, run) pairs whose run metadata reports success and has the artifact."""
    results: list[tuple[str, str]] = []
    if not experiments_dir.is_dir():
        return results
    for model_dir in sorted(path for path in experiments_dir.iterdir() if path.is_dir()):
        stage_dir = model_dir / stage
        if not stage_dir.is_dir():
            continue
        for run_dir in sorted(stage_dir.iterdir()):
            if not _run_succeeded(run_dir, artifact):
                continue
            results.append((model_dir.name, run_dir.name))
    return results


def _run_succeeded(run_dir: Path, artifact: str) -> bool:
    output = run_dir / "output"
    if artifact == "calibrated_score":
        if not (output.is_dir() and any(output.glob("calibrated_score.*"))):
            return False
    elif not (output / artifact).is_file():
        return False
    try:
        status = json.loads((run_dir / "run_metadata.json").read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError):
        return False
    return status == "succeeded"


def read_optional(path: Path) -> str:
    """Read a text file, or return an empty string when it does not exist."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def default_values(state: DemoState) -> dict[str, str]:
    """Pre-fill every form field from the SQLite example."""
    return {
        "model_name": "",
        "seed": str(DEFAULT_SQL_SEED),
        "train_config": read_optional(state.examples / "train" / "config.toml"),
        "train_sql": read_optional(state.examples / "train" / "train.sql"),
        "fixture_path": str(state.examples / "train" / "sqlite_fixture.db"),
        "inference_config": read_optional(state.examples / "inference" / "config.toml"),
        "inference_sql": read_optional(state.examples / "inference" / "inference.sql"),
        "original_score": str(state.examples / "inference" / "original_score.csv"),
        "train_run": "latest",
    }


def parse_seed(text: str) -> tuple[int, str | None]:
    """Validate the seed form field."""
    stripped = text.strip()
    try:
        value = int(stripped or str(DEFAULT_SQL_SEED))
    except ValueError:
        return 0, f"seed は整数で指定してください: {text!r}"
    if not 0 <= value <= MAX_SQL_SEED:
        return 0, f"seed は 0〜{MAX_SQL_SEED} の整数で指定してください"
    return value, None


@dataclass
class StepResult:
    """One executed step, ready to render."""

    title: str
    command: str
    result: CommandResult
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.result.returncode == 0


def render_step(step: StepResult) -> str:
    """Render one executed step as HTML."""
    status = "成功" if step.succeeded else f"失敗 (exit code {step.result.returncode})"
    captured = ""
    if step.result.stdout:
        captured += f"<h3>標準出力</h3><pre>{html.escape(step.result.stdout)}</pre>"
    if step.result.stderr:
        captured += f"<h3>標準エラー</h3><pre>{html.escape(step.result.stderr)}</pre>"
    captured_block = f"<details><summary>CLIの出力を見る</summary>{captured}</details>" if captured else ""
    return (
        f'<section class="card result {step.title}"><h2>{html.escape(step.title)}の結果</h2>'
        f'<p class="status {"ok" if step.succeeded else "fail"}">{status}</p>'
        f"<p>実行コマンド</p><pre>{html.escape(step.command)}</pre>"
        f"{captured_block}{step.detail}</section>"
    )


def metrics_detail(run_dir: Path) -> str:
    """Render metrics JSON and the calibration plot of a training run."""
    output = run_dir / "output"
    metrics = read_optional(output / "calibration_metrics.json")
    try:
        metrics = json.dumps(json.loads(metrics), ensure_ascii=False, indent=2)
    except ValueError:
        pass
    plot = read_optional(output / "calibration_plot.svg")
    note = ""
    if not plot:
        note = "<p>calibration_plot.svg が見つかりません。</p>"
    return (
        f"<p>runディレクトリ: <code>{html.escape(str(run_dir))}</code></p>"
        f"<h3>calibration_metrics.json</h3><pre>{html.escape(metrics)}</pre>"
        f"<h3>calibration_plot.svg</h3>{plot}{note}"
    )


def csv_table(frame: pl.DataFrame) -> str:
    """Render a data frame as an HTML table, limited to the first rows."""
    limited = frame.head(CSV_ROW_LIMIT)
    header = "".join(f"<th>{html.escape(name)}</th>" for name in limited.columns)
    body = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape('' if value is None else str(value))}</td>" for value in row)
        + "</tr>"
        for row in limited.rows()
    )
    note = f"<p>{limited.height} 行を表示 / 全 {frame.height} 行</p>" if frame.height > limited.height else f"<p>{frame.height} 行</p>"
    return (
        f'{note}<div class="table-wrap"><table><thead><tr>{header}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def inference_detail(run_dir: Path) -> str:
    """Render the calibrated score table of an inference run."""
    output = run_dir / "output"
    metadata_text = read_optional(run_dir / "run_metadata.json")
    try:
        metadata = json.loads(metadata_text)
    except ValueError:
        metadata = {}
    used = metadata.get("train_run_id", "不明")
    csv_path = output / "calibrated_score.csv"
    parquet_path = output / "calibrated_score.parquet"
    try:
        frame = pl.read_csv(csv_path) if csv_path.is_file() else pl.read_parquet(parquet_path)
        table = csv_table(frame)
    except (OSError, ValueError, pl.exceptions.PolarsError) as error:
        table = f"<p>calibrated_score の読み取りに失敗しました: {html.escape(str(error))}</p>"
    return (
        f"<p>使用した学習run: <code>{html.escape(str(used))}</code></p>"
        f"<p>runディレクトリ: <code>{html.escape(str(run_dir))}</code></p>"
        f"<h3>calibrated_score</h3>{table}"
    )


def stage_inputs(staging_dir: Path, files: dict[str, str]) -> dict[str, Path]:
    """Write textarea contents to a staging directory the CLI can copy from."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    return {name: path for name, path in (
        (name, _write_file(staging_dir / name, text)) for name, text in files.items()
    )}


def _write_file(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def run_train(state: DemoState, form: dict[str, str]) -> StepResult:
    """Run one training step from submitted form values."""
    name = form.get("model_name", "").strip()
    seed_text = form.get("seed", "")
    seed, error = parse_seed(seed_text)
    if not name:
        message = "experiment名（model name）を入力してください。"
        return StepResult("学習", "（実行せずに中断）", CommandResult([], 2, "", message))
    if error:
        return StepResult("学習", "（実行せずに中断）", CommandResult([], 2, "", error))
    fixture = form.get("fixture_path", "").strip()
    if fixture and not Path(fixture).is_file():
        message = f"sqlite fixture が見つかりません: {fixture}"
        return StepResult("学習", "（実行せずに中断）", CommandResult([], 2, "", message))
    staging = state.base / ".demo_staging" / uuid.uuid4().hex
    staged = stage_inputs(staging, {
        "config.toml": form.get("train_config", ""),
        "train.sql": form.get("train_sql", ""),
    })
    argv = [
        "train", "--model-name", name, "--experiments-dir", str(state.experiments_dir),
        "--config", str(staged["config.toml"]), "--sql", str(staged["train.sql"]),
        "--sqlite", "--seed", str(seed),
    ]
    if fixture:
        argv += ["--sqlite-fixture", fixture]
    try:
        result = state.runner(cli_command(argv))
        detail = ""
        if result.returncode == 0:
            runs = successful_runs(state.experiments_dir, "train", "calibration_model.pkl")
            if runs:
                model, run_id = runs[-1]
                detail = metrics_detail(state.experiments_dir / model / "train" / run_id)
        return StepResult("学習", " ".join(argv), result, detail)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def run_inference(state: DemoState, form: dict[str, str]) -> StepResult:
    """Run one inference step from submitted form values."""
    name = form.get("model_name", "").strip()
    train_run = form.get("train_run", "latest").strip()
    seed, error = parse_seed(form.get("seed", ""))
    if not name:
        message = "experiment名（model name）を入力してください。"
        return StepResult("推論", "（実行せずに中断）", CommandResult([], 2, "", message))
    if error:
        return StepResult("推論", "（実行せずに中断）", CommandResult([], 2, "", error))
    original = form.get("original_score", "").strip()
    if not Path(original).is_file():
        message = f"元スコアファイルが見つかりません: {original}"
        return StepResult("推論", "（実行せずに中断）", CommandResult([], 2, "", message))
    fixture = form.get("fixture_path", "").strip()
    if fixture and not Path(fixture).is_file():
        message = f"sqlite fixture が見つかりません: {fixture}"
        return StepResult("推論", "（実行せずに中断）", CommandResult([], 2, "", message))
    if "/" in train_run:
        name, train_run = train_run.split("/", 1)
    staging = state.base / ".demo_staging" / uuid.uuid4().hex
    staged = stage_inputs(staging, {
        "config.toml": form.get("inference_config", ""),
        "inference.sql": form.get("inference_sql", ""),
    })
    argv = [
        "inference", "--model-name", name, "--experiments-dir", str(state.experiments_dir),
        "--config", str(staged["config.toml"]), "--sql", str(staged["inference.sql"]),
        "--original-score", original, "--sqlite", "--seed", str(seed),
    ]
    if fixture:
        argv += ["--sqlite-fixture", fixture]
    if train_run != "latest":
        argv += ["--train-run", train_run]
    try:
        result = state.runner(cli_command(argv))
        detail = ""
        if result.returncode == 0:
            runs = successful_runs(state.experiments_dir, "inference", "calibrated_score")
            if runs:
                model, run_id = runs[-1]
                detail = inference_detail(state.experiments_dir / model / "inference" / run_id)
        return StepResult("推論", " ".join(argv), result, detail)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def run_prepare(state: DemoState) -> StepResult:
    """Regenerate the SQLite example fixture with prepare_example.py."""
    script = state.examples / "prepare_example.py"
    if not script.is_file():
        message = f"fixture生成スクリプトが見つかりません: {script}"
        return StepResult("デモデータ再生成", "（実行せずに中断）", CommandResult([], 2, "", message))
    result = state.runner([sys.executable, str(script)])
    note = ""
    if result.returncode == 0:
        note = "<p>examples/sqlite/ のfixtureと候補CSVを再生成しました。フォームを再読み込みすると内容が更新されます。</p>"
    return StepResult("デモデータ再生成", f"{sys.executable} {script}", result, note)


def build_handler(state: DemoState, template: Template) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to the demo state."""

    class DemoHandler(BaseHTTPRequestHandler):
        """Serve the demo page and run pipeline steps on POST."""

        def do_GET(self) -> None:
            query = parse_qs(urlparse(self.path).query)
            tab = query.get("tab", ["train"])[0]
            if tab not in ("train", "inference"):
                tab = "train"
            page = render_page(state, template, tab=tab, values=default_values(state))
            self.respond(page, 200)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            form = {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}
            action = form.get("action", "")
            values = {**default_values(state), **{
                key: value for key, value in form.items() if key in FORM_KEYS
            }}
            if action == "train":
                step = run_train(state, form)
                page = render_page(state, template, tab="train", values=values, train_step=step)
            elif action == "inference":
                step = run_inference(state, form)
                page = render_page(state, template, tab="inference", values=values, inference_step=step)
            elif action == "prepare":
                step = run_prepare(state)
                page = render_page(state, template, tab="train", values=values, prepare_step=step)
            else:
                message = f"未知のアクション: {action}"
                page = render_page(state, template, tab="train", values=values, banner=message)
            self.respond(page, 200 if action in ("train", "inference", "prepare") else 400)

        def respond(self, page: str, status: int) -> None:
            payload = page.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return DemoHandler


FORM_KEYS = (
    "model_name", "seed", "train_config", "train_sql", "fixture_path",
    "inference_config", "inference_sql", "original_score", "train_run",
)


def field_text(name: str, label: str, value: str, hint: str = "", input_type: str = "text") -> str:
    """Render one single-line form field."""
    escaped = html.escape(value, quote=True)
    return (
        f'<label class="field"><span>{html.escape(label)}</span>'
        f'<input type="{input_type}" name="{name}" value="{escaped}" placeholder="{html.escape(hint, quote=True)}">'
        f"</label>"
    )


def field_textarea(name: str, label: str, value: str, rows: int) -> str:
    """Render one textarea form field."""
    return (
        f'<label class="field"><span>{html.escape(label)}</span>'
        f'<textarea name="{name}" rows="{rows}" spellcheck="false">{html.escape(value)}</textarea>'
        f"</label>"
    )


def train_run_options(state: DemoState, selected: str) -> str:
    """Render the training run select options."""
    runs = successful_runs(state.experiments_dir, "train", "calibration_model.pkl")
    options = ['<option value="latest">latest（最新の成功run）</option>']
    for model, run_id in runs:
        label = f"{model} / {run_id}"
        options.append(
            f'<option value="{html.escape(f"{model}/{run_id}", quote=True)}"'
            + (' selected' if f"{model}/{run_id}" == selected else '')
            + f">{html.escape(label)}</option>"
        )
    return "".join(options)


def render_train_form(state: DemoState, values: dict[str, str]) -> str:
    """Render the training form fields."""
    return (
        field_text("model_name", "experiment名（model name）", values["model_name"], MODEL_PLACEHOLDER)
        + field_text("seed", "seed", values["seed"], str(DEFAULT_SQL_SEED), "number")
        + field_textarea("train_config", "学習config.toml", values["train_config"], 8)
        + field_textarea("train_sql", "train.sql（row_role=train/calibrate/validation を返す）", values["train_sql"], 16)
        + field_text("fixture_path", "sqlite fixture（空欄ならfixtureなしのメモリDB）", values["fixture_path"])
    )


def render_inference_form(state: DemoState, values: dict[str, str]) -> str:
    """Render the inference form fields."""
    return (
        field_text("model_name", "experiment名（model name）", values["model_name"], MODEL_PLACEHOLDER)
        + field_text("seed", "seed", values["seed"], str(DEFAULT_SQL_SEED), "number")
        + field_textarea("inference_config", "推論config.toml（空またはコメントのみ）", values["inference_config"], 4)
        + field_textarea("inference_sql", "inference.sql（calibration_candidates を参照）", values["inference_sql"], 12)
        + field_text("original_score", "元スコアファイル（CSVまたはParquet）", values["original_score"])
        + field_text("fixture_path", "sqlite fixture（空欄ならfixtureなしのメモリDB）", values["fixture_path"])
        + f'<label class="field"><span>使用する学習run</span>'
        f'<select name="train_run">{train_run_options(state, values.get("train_run", "latest"))}</select></label>'
    )


def render_page(
    state: DemoState,
    template: Template,
    *,
    tab: str,
    values: dict[str, str],
    train_step: StepResult | None = None,
    inference_step: StepResult | None = None,
    prepare_step: StepResult | None = None,
    banner: str = "",
) -> str:
    """Render the full demo page for the active tab."""
    train_html = render_train_form(state, values)
    inference_html = render_inference_form(state, values)
    return template.substitute(
        nav=(
            f'<a href="/?tab=train" class="{"active" if tab == "train" else "inactive"}">学習</a>'
            f'<a href="/?tab=inference" class="{"active" if tab == "inference" else "inactive"}">推論</a>'
        ),
        train_tab_class="active" if tab == "train" else "hidden",
        inference_tab_class="active" if tab == "inference" else "hidden",
        banner=html.escape(banner),
        train_form=train_html,
        train_result=render_step(train_step) if train_step else "",
        inference_form=inference_html,
        inference_result=render_step(inference_step) if inference_step else "",
        prepare_result=render_step(prepare_step) if prepare_step else "",
        sync_script=SYNC_SCRIPT,
    )


SYNC_SCRIPT = """document.querySelectorAll('input[name="model_name"], input[name="seed"]').forEach(function (field) {
  field.addEventListener('input', function () {
    document.querySelectorAll('input[name="' + field.name + '"]').forEach(function (other) {
      if (other !== field) other.value = field.value;
    });
  });
});"""


def load_template() -> Template:
    """Load the page template shipped next to this module."""
    return Template((Path(__file__).with_name("template.html")).read_text(encoding="utf-8"))


def create_server(base: Path, runner: Runner, port: int = 8000) -> ThreadingHTTPServer:
    """Create the demo HTTP server, falling back to a free port."""
    state = DemoState(base=base, runner=runner)
    handler = build_handler(state, load_template())
    try:
        return ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        return ThreadingHTTPServer(("127.0.0.1", 0), handler)


def serve(base: Path) -> None:
    """Start the demo server and open a browser."""
    import threading
    import webbrowser

    server = create_server(base, subprocess_runner)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"calibration pipeline demo GUI: {url}")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
