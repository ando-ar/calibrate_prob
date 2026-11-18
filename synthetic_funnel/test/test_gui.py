import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from synthetic_funnel import MarketConfig, write_market_dataset
from synthetic_funnel.export import SQLITE_FILENAME
from synthetic_funnel.gui import server
from synthetic_funnel.gui.runner import CommandResult

MARKET_FORM = {
    "seed": "71", "seeker_arrivals": "8", "job_arrivals": "8", "days": "90", "warmup": "365",
    "application_days": "20", "posting_days": "40", "referrals": "5",
    "market_start": "2025-01-01T00:00:00+09:00", "format": "sqlite", "advanced_config": "{}",
}
TINY_CONFIG = MarketConfig(days=5, seeker_arrivals=2, job_arrivals=2, warmup=0, seed=3)


def fake_runner(returncode=0, stdout="", stderr=""):
    def runner(command):
        return CommandResult(list(command), returncode, stdout, stderr)
    return runner


def cli_like_runner(base: Path):
    """Pretend the CLI ran: record argv and leave a market manifest behind."""
    def runner(command):
        run_dir = Path(command[command.index("--run-dir") + 1])
        output = run_dir / "output"
        output.mkdir(parents=True, exist_ok=True)
        (output / "manifest.json").write_text(json.dumps({
            "mode": "market", "output_format": "sqlite",
            "row_counts": {"events": 500, "events_latent": 500, "user": 400, "item": 400, "daily": 90},
        }), encoding="utf-8")
        return CommandResult(list(command), 0, json.dumps({"rows": 500}), "")
    return runner


class GuiTestCase(unittest.TestCase):
    def setUp(self):
        patcher = tempfile.TemporaryDirectory()
        self.root = Path(patcher.name)
        self.addCleanup(patcher.cleanup)
        self.state = server.GuiState(base=self.root, runner=fake_runner())


class BuildValuesTest(GuiTestCase):
    def test_market_values_collect_integers_and_advanced_config(self):
        form = {**MARKET_FORM, "warmup": "60",
                "advanced_config": json.dumps({"training_window_days": [2, 10, 20]})}
        values, error = server.build_market_values(form)
        self.assertEqual(error, "")
        self.assertEqual(values["seed"], 71)
        self.assertEqual(values["seeker_arrivals"], 8)
        self.assertEqual(values["days"], 90)
        self.assertEqual(values["warmup"], 60)
        self.assertEqual(values["training_window_days"], [2, 10, 20])
        self.assertEqual(values["market_start"], "2025-01-01T00:00:00+09:00")

    def test_market_values_reject_bad_integers_and_json(self):
        values, error = server.build_market_values({**MARKET_FORM, "days": "abc"})
        self.assertIsNone(values)
        self.assertIn("整数で指定してください", error)
        values, error = server.build_market_values({**MARKET_FORM, "seeker_arrivals": "-1"})
        self.assertIn("0以上の整数", error)
        values, error = server.build_market_values({**MARKET_FORM, "advanced_config": "{"})
        self.assertIn("JSONとして読めません", error)
        values, error = server.build_market_values({**MARKET_FORM, "advanced_config": "[1]"})
        self.assertIn("JSONオブジェクト", error)

    def test_market_values_require_market_start(self):
        values, error = server.build_market_values({**MARKET_FORM, "market_start": " "})
        self.assertIsNone(values)
        self.assertIn("market_start", error)

    def test_form_values_take_precedence_over_advanced_config(self):
        form = {**MARKET_FORM, "advanced_config": json.dumps({"market_start": "2000-01-01T00:00:00+09:00"})}
        values, error = server.build_market_values(form)
        self.assertEqual(error, "")
        self.assertEqual(values["market_start"], MARKET_FORM["market_start"])


class RunGenerationTest(GuiTestCase):
    def test_successful_run_writes_reproducible_config(self):
        self.state = server.GuiState(base=self.root, runner=cli_like_runner(self.root))
        step = server.run_generation(self.state, MARKET_FORM)
        self.assertTrue(step.succeeded)
        run_dir = self.state.runs_dir / step.run_id
        config = json.loads((run_dir / "input" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["seeker_arrivals"], 8)
        self.assertEqual(config["market_start"], MARKET_FORM["market_start"])
        self.assertIn("--run-dir", step.command)
        self.assertIn("--progress", step.command)
        self.assertNotIn("--market", step.command)
        self.assertNotIn("--rows", step.command)
        self.assertIn("成功", server.render_step(step))
        self.assertIn("求職者（user）", step.detail)
        self.assertIn(f"tab=results&amp;run={step.run_id}", step.detail)

    def test_failed_run_shows_hint_without_creating_a_run(self):
        step = server.run_generation(self.state, {**MARKET_FORM, "days": "-1"})
        self.assertEqual(step.result.returncode, 2)
        self.assertIn("指定してください", step.detail)
        self.assertEqual(step.run_id, "")
        self.assertFalse(self.state.runs_dir.exists())

    def test_failure_hint_from_cli_stderr(self):
        self.state = server.GuiState(base=self.root, runner=fake_runner(
            returncode=2, stderr="error: market_start is required"))
        step = server.run_generation(self.state, MARKET_FORM)
        self.assertIn("market_start", step.detail)


class RunInspectionTest(GuiTestCase):
    def write_run(self, name, fmt="sqlite"):
        run_dir = self.root / "runs" / "gui" / name
        (run_dir / "input").mkdir(parents=True)
        (run_dir / "input" / "config.json").write_text(
            json.dumps({"market_start": MARKET_FORM["market_start"]}), encoding="utf-8")
        write_market_dataset(TINY_CONFIG, run_dir / "output",
                             market_start="2025-01-01T00:00:00+09:00", output_format=fmt)
        return run_dir

    def expected_counts(self, run_dir):
        with closing(sqlite3.connect(run_dir / "output" / SQLITE_FILENAME)) as database:
            return database.execute(
                "SELECT COUNT(*), COUNT(oubo_dt), COUNT(shorui_tsuuka_dt), COUNT(kettei_dt) FROM events"
            ).fetchone()

    def test_event_counts_match_database_across_formats(self):
        sqlite_run = self.write_run("sqlite_run")
        counts = server.funnel_event_counts(sqlite_run / "output")
        self.assertEqual([counts["total"], *counts["arrivals"]], list(self.expected_counts(sqlite_run)))
        csv_run = self.write_run("csv_run", fmt="csv")
        csv_counts = server.funnel_event_counts(csv_run / "output")
        self.assertEqual(csv_counts["total"], counts["total"])
        self.assertEqual(csv_counts["arrivals"], counts["arrivals"])
        parquet_run = self.write_run("parquet_run", fmt="parquet")
        parquet_counts = server.funnel_event_counts(parquet_run / "output")
        self.assertEqual(parquet_counts["arrivals"], counts["arrivals"])

    def test_run_entries_and_status(self):
        self.write_run("001_success")
        self.write_run("002_failure", fmt="csv")
        (self.root / "runs" / "gui" / "002_failure" / "output" / "manifest.json").unlink()
        (self.root / "runs" / "gui" / "003_pending" / "input").mkdir(parents=True)
        entries = {entry["id"]: entry for entry in server.run_entries(self.state)}
        self.assertEqual(entries["001_success"]["status"], "成功")
        self.assertEqual(entries["002_failure"]["status"], "失敗")
        self.assertEqual(entries["003_pending"]["status"], "未実行")
        self.assertEqual(entries["001_success"]["kind"], "市場")
        self.assertIsInstance(int(entries["001_success"]["rows"].replace(",", "")), int)
        listing = server.render_run_list(self.state)
        self.assertIn("001_success", listing)
        self.assertIn("tab=results&amp;run=001_success", listing)

    def test_run_detail_renders_tables_manifest_and_config(self):
        self.write_run("detail_run")
        page = server.render_run_detail(self.state, "detail_run")
        self.assertIn("状態: <strong>成功</strong>", page)
        self.assertIn("件数サマリ", page)
        self.assertIn("events_latent", page)
        self.assertIn("<details><summary>events", page)
        self.assertIn("<details><summary>daily", page)
        self.assertIn("generation_seconds", page)
        self.assertIn("CLIで同じ条件を再現", page)

    def test_run_detail_rejects_bad_ids_and_missing_runs(self):
        self.assertIn("run が見つかりません", server.render_run_detail(self.state, ".."))
        self.assertIn("run が見つかりません", server.render_run_detail(self.state, "nope"))

    def test_market_manifest_summary(self):
        manifest = {"mode": "market", "output_format": "sqlite",
                    "row_counts": {"events": 12, "events_latent": 12, "user": 8, "item": 9, "daily": 90}}
        table = server.market_counts_table(manifest)
        self.assertIn("求職者（user）", table)
        self.assertIn("events_latent", table)
        self.assertIn("12", table)


class PageRenderingTest(GuiTestCase):
    def test_pages_render_all_tabs_with_defaults(self):
        template = server.load_template()
        for tab in ("market", "results"):
            page = server.render_page(self.state, template, tab=tab,
                                      values=server.default_values(tab),
                                      results_html=server.render_run_list(self.state))
            self.assertIn(f'id="{tab}"', page)
            self.assertNotIn("${", page)
        market = server.render_page(self.state, template, tab="market",
                                    values=server.default_values("market"))
        self.assertIn('name="seeker_arrivals"', market)
        self.assertIn('name="warmup"', market)
        self.assertNotIn('name="legacy_models"', market)
        self.assertNotIn('name="retain"', market)
        results = server.render_page(self.state, template, tab="results",
                                     values=server.default_values("results"),
                                     results_html="<p>まだ実行がありません。</p>")
        self.assertIn("まだ実行がありません", results)

    def test_html_escaping_of_values(self):
        values = {**server.default_values("market"), "market_start": '<script>"x"'}
        page = server.render_page(self.state, server.load_template(), tab="market", values=values)
        self.assertNotIn('<script>"x"', page)
        self.assertIn("&lt;script&gt;", page)


if __name__ == "__main__":
    unittest.main()
