"""Tests for the demo GUI server and its contract with the pipeline CLI."""

import io
import json
import sqlite3
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from contextlib import closing, redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from calibration_pipeline import cli
from calibration_pipeline.demo_gui.runner import CommandResult, cli_command
from calibration_pipeline.demo_gui.server import create_server, successful_runs


def serve_demo(base: Path, runner: Callable[[Sequence[str]], CommandResult]
               ) -> tuple[int, ThreadingHTTPServer]:
    """Start a demo server on a free port in a daemon thread."""
    server = create_server(base, runner, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server.server_address[1], server


def request(port: int, path: str, form: dict[str, str] | None = None) -> tuple[int, str]:
    """Send one request to the demo server and return (status, body)."""
    data = None
    if form is not None:
        body = "&".join(
            f"{key}={urllib.parse.quote(value)}" for key, value in form.items()
        )
        data = body.encode("utf-8")
    target = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data)
    try:
        with urllib.request.urlopen(target) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.read().decode("utf-8")


class RecordingRunner:
    """Stub runner that records commands and returns canned results."""

    def __init__(self, results: list[CommandResult] | None = None,
                 function: Callable[[Sequence[str]], CommandResult] | None = None):
        self.results = results or []
        self.function = function
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> CommandResult:
        self.calls.append(list(command))
        if self.function is not None:
            return self.function(command)
        if self.results:
            return self.results.pop(0)
        return CommandResult(list(command), 0, "ok", "")


def make_train_run(base: Path, model: str, run_id: str, status: str = "succeeded") -> Path:
    """Create one fake successful training run directory."""
    run_dir = base / "experiments" / "demo_gui" / model / "train" / run_id
    (run_dir / "output").mkdir(parents=True)
    (run_dir / "output" / "calibration_model.pkl").write_bytes(b"model")
    (run_dir / "run_metadata.json").write_text(
        json.dumps({"status": status}), encoding="utf-8"
    )
    return run_dir


class DemoGuiTest(unittest.TestCase):
    """Shared fixture: a temp project base and a running demo server."""

    runner: RecordingRunner

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        train_dir = self.base / "examples" / "sqlite" / "train"
        train_dir.mkdir(parents=True)
        self.fixture = train_dir / "sqlite_fixture.db"
        with closing(sqlite3.connect(self.fixture)) as connection, connection:
            connection.execute('CREATE TABLE events (user_id INTEGER, note TEXT)')
            connection.executemany('INSERT INTO events VALUES (?, ?)', [(i, '<script>alert(1)</script>') for i in range(12)])
            connection.execute('CREATE VIEW scored_referrals AS SELECT * FROM events')
        (train_dir / "config.toml").write_text('[model]\nscore="score"\n', encoding="utf-8")
        (train_dir / "train.sql").write_text("-- example train sql\n", encoding="utf-8")
        (self.base / "examples" / "sqlite" / "inference").mkdir(parents=True)
        self.runner: RecordingRunner = RecordingRunner([])
        self.port, self.server = serve_demo(self.base, self.runner)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def restart(self, runner: RecordingRunner) -> None:
        """Restart the demo server with another runner."""
        self.server.shutdown()
        self.server.server_close()
        self.runner = runner
        self.port, self.server = serve_demo(self.base, runner)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path: str) -> tuple[int, str]:
        return request(self.port, path)

    def post(self, form: dict[str, str]) -> tuple[int, str]:
        return request(self.port, "/", form)

    def base_form(self) -> dict[str, str]:
        return {
            "action": "train",
            "model_name": "demo_model",
            "seed": "42",
            "train_config": '[model]\nscore="score"\n',
            "train_sql": "SELECT ...",
            "fixture_path": "",
            "inference_config": "# none",
            "inference_sql": "SELECT ...",
            "original_score": "",
            "train_run": "latest",
        }


class RenderTest(DemoGuiTest):
    def test_page_prefills_from_example_and_lists_both_tabs(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("[model]", body)
        self.assertIn("-- example train sql", body)
        self.assertIn("sqlite_fixture.db", body)
        self.assertIn("学習で何が起きるか", body)
        self.assertIn("推論で何が起きるか", body)
        self.assertIn("latest（最新の成功run）", body)
        self.assertIn('id="train" class="tab active"', body)
        self.assertIn('id="inference" class="tab hidden"', body)
        self.assertEqual(self.runner.calls, [])

    def test_tab_switch_selects_active_tab(self):
        status, body = self.get("/?tab=inference")
        self.assertEqual(status, 200)
        self.assertIn('id="inference" class="tab active"', body)
        self.assertIn('id="train" class="tab hidden"', body)

    def test_unknown_tab_falls_back_to_train(self):
        status, body = self.get("/?tab=nope")
        self.assertEqual(status, 200)
        self.assertIn('id="train" class="tab active"', body)

    def test_train_run_dropdown_lists_successful_runs(self):
        make_train_run(self.base, "demo_model", "20261118_000000")
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("demo_model / 20261118_000000", body)
        self.assertIn('value="demo_model/20261118_000000"', body)


class FormActionTest(DemoGuiTest):
    def test_train_form_builds_cli_command_and_reports_failure(self):
        self.restart(RecordingRunner([CommandResult(["cmd"], 2, "out", "boom")]))
        status, body = self.post(self.base_form())
        self.assertEqual(status, 200)
        self.assertIn("失敗 (exit code 2)", body)
        self.assertIn("boom", body)
        self.assertEqual(len(self.runner.calls), 1)
        command = self.runner.calls[0]
        self.assertEqual(command[:3], cli_command([])[:3])
        self.assertIn("train", command)
        self.assertIn("--model-name", command)
        self.assertIn("demo_model", command)
        self.assertIn("--sqlite", command)
        self.assertIn("--seed", command)
        self.assertIn("42", command)
        self.assertEqual(command[command.index("--sqlite-fixture") + 1], str(self.fixture))
        self.assertIn(str(self.base / "experiments" / "demo_gui"), command)
        staging = self.base / ".demo_staging"
        self.assertEqual(list(staging.iterdir()) if staging.exists() else [], [])

    def test_train_with_fixture_passes_fixture_argument(self):
        fixture = self.base / "fixture.db"
        fixture.write_bytes(b"db")
        self.restart(RecordingRunner([CommandResult(["cmd"], 2, "", "err")]))
        self.post(self.base_form() | {"fixture_path": str(fixture)})
        command = self.runner.calls[0]
        self.assertIn("--sqlite-fixture", command)
        self.assertEqual(command[command.index("--sqlite-fixture") + 1], str(self.fixture))
        self.assertNotIn(str(fixture), command)

    def test_train_requires_model_name(self):
        status, body = self.post(self.base_form() | {"model_name": " "})
        self.assertEqual(status, 200)
        self.assertIn("experiment名", body)
        self.assertEqual(self.runner.calls, [])

    def test_train_rejects_out_of_range_seed(self):
        status, body = self.post(self.base_form() | {"seed": "-1"})
        self.assertEqual(status, 200)
        self.assertIn("seed は", body)
        self.assertEqual(self.runner.calls, [])

    def test_train_requires_existing_fixture(self):
        self.fixture.unlink()
        status, body = self.post(self.base_form() | {"fixture_path": str(self.base / "missing.db")})
        self.assertEqual(status, 200)
        self.assertIn("固定fixture DBを読み取れません", body)
        self.assertEqual(self.runner.calls, [])

    def test_successful_train_renders_metrics_and_plot(self):
        run_dir = make_train_run(self.base, "demo_model", "20261118_010000")
        (run_dir / "output" / "calibration_metrics.json").write_text(
            json.dumps({"method": "catboost_isotonic", "rows": {"train": 40}}), encoding="utf-8"
        )
        (run_dir / "output" / "calibration_plot.svg").write_text("<svg></svg>", encoding="utf-8")
        self.restart(RecordingRunner([CommandResult(["cmd"], 0, "saved", "")]))
        status, body = self.post(self.base_form())
        self.assertEqual(status, 200)
        self.assertIn("成功", body)
        self.assertIn("catboost_isotonic", body)
        self.assertIn("<svg></svg>", body)
        self.assertIn(str(run_dir), body)

    def test_inference_form_builds_cli_command_and_renders_table(self):
        original = self.base / "original_score.csv"
        original.write_text("user_id,item_id\n10000000,20000000\n", encoding="utf-8")
        run_dir = self.base / "experiments" / "demo_gui" / "demo_model" / "inference" / "20261118_020000"
        (run_dir / "output").mkdir(parents=True)
        (run_dir / "output" / "calibrated_score.csv").write_text(
            "user_id,item_id,probability\n10000000,20000000,0.5\n", encoding="utf-8"
        )
        (run_dir / "run_metadata.json").write_text(
            json.dumps({"status": "succeeded", "train_run_id": "20261118_010000"}),
            encoding="utf-8",
        )
        self.restart(RecordingRunner([CommandResult(["cmd"], 0, "saved", "")]))
        status, body = self.post(self.base_form() | {"action": "inference", "original_score": str(original)})
        self.assertEqual(status, 200)
        self.assertIn("成功", body)
        self.assertIn("probability", body)
        self.assertIn("20261118_010000", body)
        self.assertIn("20261118_020000", body)
        command = self.runner.calls[0]
        self.assertIn("inference", command)
        self.assertIn("--original-score", command)
        self.assertIn(str(original), command)
        self.assertNotIn("--train-run", command)

    def test_inference_with_explicit_run_passes_train_run(self):
        original = self.base / "original_score.csv"
        original.write_text("user_id,item_id\n", encoding="utf-8")
        make_train_run(self.base, "demo_model", "20261118_010000")
        self.restart(RecordingRunner([CommandResult(["cmd"], 2, "", "err")]))
        form = self.base_form() | {
            "action": "inference",
            "original_score": str(original),
            "train_run": "demo_model/20261118_010000",
        }
        self.post(form)
        command = self.runner.calls[0]
        self.assertIn("--train-run", command)
        self.assertIn("20261118_010000", command)
        self.assertEqual(command[command.index("--model-name") + 1], "demo_model")

    def test_inference_with_fixture_passes_fixture_argument(self):
        original = self.base / "original_score.csv"
        original.write_text("user_id,item_id\n", encoding="utf-8")
        fixture = self.base / "fixture.db"
        fixture.write_bytes(b"db")
        self.restart(RecordingRunner([CommandResult(["cmd"], 2, "", "err")]))
        form = self.base_form() | {
            "action": "inference",
            "original_score": str(original),
            "fixture_path": str(fixture),
        }
        self.post(form)
        command = self.runner.calls[0]
        self.assertIn("--sqlite-fixture", command)
        self.assertEqual(command[command.index("--sqlite-fixture") + 1], str(self.fixture))
        self.assertNotIn(str(fixture), command)

    def test_inference_requires_existing_fixture(self):
        self.fixture.unlink()
        original = self.base / "original_score.csv"
        original.write_text("user_id,item_id\n", encoding="utf-8")
        status, body = self.post(self.base_form() | {
            "action": "inference",
            "original_score": str(original),
            "fixture_path": str(self.base / "missing.db"),
        })
        self.assertEqual(status, 200)
        self.assertIn("固定fixture DBを読み取れません", body)
        self.assertEqual(self.runner.calls, [])

    def test_inference_requires_existing_original_score(self):
        status, body = self.post(self.base_form() | {"action": "inference"})
        self.assertEqual(status, 200)
        self.assertIn("元スコアファイルが見つかりません", body)
        self.assertEqual(self.runner.calls, [])

    def test_unknown_action_is_rejected(self):
        status, body = self.post(self.base_form() | {"action": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("未知のアクション", body)
        self.assertEqual(self.runner.calls, [])


class PipelineContractTest(DemoGuiTest):
    """Run the real CLI through the demo server so pipeline changes break here first."""

    def restart_in_process(self) -> None:
        def cli_in_process(command: Sequence[str]) -> CommandResult:
            if command[:3] != cli_command([]):
                raise AssertionError(f"unexpected command: {command}")
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(command[3:])
            return CommandResult(list(command), code, stdout.getvalue(), stderr.getvalue())

        self.restart(RecordingRunner(function=cli_in_process))

    def base_form(self) -> dict[str, str]:
        return {
            "action": "train",
            "model_name": "demo_e2e",
            "seed": "42",
            "train_config": (
                '[model]\nscore = "score"\nlabel = "label"\nprobability = "probability"\n'
                'numeric_features = ["numeric", "empty_numeric"]\n'
                'categorical_features = ["category"]\n'
            ),
            "train_sql": """
WITH RECURSIVE n(i) AS (
  VALUES(0) UNION ALL SELECT i+1 FROM n WHERE i<119
)
SELECT CASE WHEN i<40 THEN 'train' WHEN i<80 THEN 'calibrate' ELSE 'validation' END AS row_role,
       i*0.01 AS score,
       CASE WHEN i%3=0 THEN NULL ELSE i*0.03 END AS numeric,
       NULL AS empty_numeric,
       CASE WHEN i%3=0 THEN NULL WHEN i%3=1 THEN '0' ELSE 'x' END AS category,
       CASE WHEN i%4=0 OR i%7=0 THEN 1 ELSE 0 END AS label
FROM n;
""",
            "fixture_path": "",
            "inference_config": "# Reserved\n",
            "inference_sql": """
SELECT candidate_row_id, score, NULL AS numeric, NULL AS empty_numeric,
       CASE WHEN candidate_row_id=0 THEN NULL ELSE 'unseen' END AS category
FROM calibration_candidates ORDER BY candidate_row_id DESC;
""",
            "original_score": "",
            "train_run": "latest",
        }

    def test_train_and_inference_run_end_to_end(self):
        self.restart_in_process()
        form = self.base_form()
        form["train_config"] = form["train_config"].replace("\n", "\r\n")
        status, body = self.post(form)
        self.assertEqual(status, 200)
        self.assertIn("成功", body, body)
        self.assertIn("catboost_isotonic", body)
        self.assertIn("calibration_plot.svg", body)
        runs = successful_runs(self.base / "experiments" / "demo_gui", "train", "calibration_model.pkl")
        self.assertEqual([model for model, _ in runs], ["demo_e2e"])
        original = self.base / "original_score.csv"
        original.write_text(
            "user_id,item_id,score\n10000000,20000000,0.2\n10000001,20000001,0.8\n",
            encoding="utf-8",
        )
        status, body = self.post(self.base_form() | {"action": "inference", "original_score": str(original)})
        self.assertEqual(status, 200)
        self.assertIn("成功", body, body)
        self.assertIn("probability", body)
        self.assertIn("使用した学習run", body)
        self.assertIn(runs[-1][1], body)
        inference_runs = successful_runs(
            self.base / "experiments" / "demo_gui", "inference", "calibrated_score")
        self.assertEqual([model for model, _ in inference_runs], ["demo_e2e"])

    def test_pipeline_error_surfaces_cli_stderr(self):
        self.restart_in_process()
        status, body = self.post(self.base_form() | {"train_sql": "SELECT * FROM missing_table"})
        self.assertEqual(status, 200)
        self.assertIn("失敗", body)
        self.assertIn("calibration-pipeline:", body)


class FixtureInspectionTest(DemoGuiTest):
    def test_inspection_is_bounded_escaped_and_read_only(self):
        before = self.fixture.read_bytes()
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("events (table) — 12 件", body)
        self.assertIn("scored_referrals (view) — 12 件", body)
        self.assertIn("INTEGER", body)
        self.assertIn("サンプル 5 件", body)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn('name="fixture_path"', body)
        self.assertEqual(before, self.fixture.read_bytes())

    def test_missing_database_is_not_created(self):
        self.fixture.unlink()
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("prepare_example.py", body)
        self.assertIn("固定fixture DBを読み取れません", body)
        self.assertFalse(self.fixture.exists())

    def test_corrupt_database_blocks_both_actions(self):
        self.fixture.write_bytes(b"not a sqlite database")
        original = self.base / "original.csv"
        original.write_text("user_id,item_id\n", encoding="utf-8")
        for action in ("train", "inference"):
            _, body = self.post(self.base_form() | {"action": action, "original_score": str(original)})
            self.assertIn("固定fixture DBを読み取れません", body)
            self.assertIn("読み取り権限", body)
        self.assertEqual(self.runner.calls, [])

    def test_quoted_names_empty_tables_and_nulls(self):
        with closing(sqlite3.connect(self.fixture)) as connection, connection:
            connection.execute('CREATE TABLE "odd""name" (value TEXT)')
            connection.execute('INSERT INTO "odd""name" VALUES (NULL)')
            connection.execute('CREATE TABLE empty_table (value REAL)')
        _, body = self.get("/")
        self.assertIn("odd&quot;name", body)
        self.assertIn("empty_table (table) — 0 件", body)
        self.assertIn("<td>NULL</td>", body)

    def test_documentation_route_stays_inside_docs(self):
        docs = self.base / "docs" / "html"
        docs.mkdir(parents=True)
        (docs / "quickstart.html").write_text("<p>guide</p>", encoding="utf-8")
        self.assertEqual(self.get("/docs/quickstart.html"), (200, "<p>guide</p>"))
        self.assertEqual(self.get("/docs/../../examples/sqlite/train/config.toml")[0], 404)
