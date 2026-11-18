import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from synthetic_funnel import Config, LegacyScores, write_dataset
from synthetic_funnel.export import SQLITE_FILENAME
from synthetic_funnel.gui import server
from synthetic_funnel.gui.runner import CommandResult

GENERATE_FORM = {
    "rows": "500", "seed": "42", "referral_start": "2024-01-01T00:00:00+09:00",
    "referral_end": "2025-12-31T00:00:00+09:00", "as_of": "2026-02-01T00:00:00+09:00",
    "reference_rows": "1000", "reference_seed": "90210", "legacy_scores": "off",
    "format": "sqlite", "advanced_config": "{}",
}
TINY_CONFIG = Config(rows=5, seed=3, reference_rows=100, legacy_scores=LegacyScores(enabled=False))


def fake_runner(returncode=0, stdout="", stderr=""):
    def runner(command):
        return CommandResult(list(command), returncode, stdout, stderr)
    return runner


def cli_like_runner(base: Path):
    """Pretend the CLI ran: record argv and leave a manifest-only output behind."""
    def runner(command):
        run_dir = Path(command[command.index("--run-dir") + 1])
        output = run_dir / "output"
        output.mkdir(parents=True, exist_ok=True)
        (output / "manifest.json").write_text(json.dumps({
            "config": {"rows": 500}, "generated_expected_rates": [0.1, 0.5, 0.1],
            "generated_realized_rates": [0.08, 0.4, 0.0], "output_format": "sqlite",
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
    def test_funnel_values_merge_advanced_and_legacy(self):
        form = {**GENERATE_FORM, "legacy_scores": "on",
                "advanced_config": json.dumps({
                    "application_delay": {"mean_days": 4, "std_days": 1},
                    "legacy_scores": {"training_rows": 3000},
                })}
        values, error = server.build_funnel_values(form)
        self.assertEqual(error, "")
        self.assertEqual(values["rows"], 500)
        self.assertEqual(values["reference_rows"], 1000)
        self.assertEqual(values["application_delay"], {"mean_days": 4, "std_days": 1})
        self.assertEqual(values["legacy_scores"], {"training_rows": 3000, "enabled": True})

    def test_funnel_values_reject_bad_integers_and_json(self):
        values, error = server.build_funnel_values({**GENERATE_FORM, "rows": "abc"})
        self.assertIsNone(values)
        self.assertIn("件数は整数で指定してください", error)
        values, error = server.build_funnel_values({**GENERATE_FORM, "rows": "0"})
        self.assertIn("件数は1以上の整数で指定してください", error)
        values, error = server.build_funnel_values({**GENERATE_FORM, "advanced_config": "{"})
        self.assertIn("JSONとして読めません", error)
        values, error = server.build_funnel_values({**GENERATE_FORM, "advanced_config": "[1]"})
        self.assertIn("JSONオブジェクト", error)

    def test_funnel_values_require_dates(self):
        values, error = server.build_funnel_values({**GENERATE_FORM, "as_of": " "})
        self.assertIn("観測締切（as_of）", error)

    def test_market_values_resolve_legacy_models_against_base(self):
        model = self.root / "runs" / "model" / "output" / "legacy_models.json"
        model.parent.mkdir(parents=True)
        model.write_text("{}", encoding="utf-8")
        form = {"seed": "71", "seeker_arrivals": "8", "job_arrivals": "8", "days": "90",
                "application_days": "20", "posting_days": "40", "referrals": "5",
                "market_start": "2025-01-01T00:00:00+09:00",
                "legacy_models": str(model.relative_to(self.root)), "retain": "applications",
                "format": "sqlite", "advanced_config": "{}"}
        values, error = server.build_market_values(self.state, form)
        self.assertEqual(error, "")
        self.assertEqual(values["legacy_models"], str(model.resolve()))
        self.assertEqual(values["retain"], "applications")

    def test_market_values_require_existing_model(self):
        form = {"seed": "71", "seeker_arrivals": "8", "job_arrivals": "8", "days": "90",
                "application_days": "20", "posting_days": "40", "referrals": "5",
                "market_start": "2025-01-01T00:00:00+09:00", "legacy_models": "",
                "format": "sqlite", "advanced_config": "{}"}
        values, error = server.build_market_values(self.state, form)
        self.assertIn("legacy_scores を有効にして実行", error)
        form["legacy_models"] = "runs/missing.json"
        values, error = server.build_market_values(self.state, form)
        self.assertIn("見つかりません", error)


class RunGenerationTest(GuiTestCase):
    def test_successful_run_writes_reproducible_config(self):
        self.state = server.GuiState(base=self.root, runner=cli_like_runner(self.root))
        step = server.run_generation(self.state, GENERATE_FORM, market=False)
        self.assertTrue(step.succeeded)
        run_dir = self.state.runs_dir / step.run_id
        self.assertEqual(
            json.loads((run_dir / "input" / "config.json").read_text(encoding="utf-8"))["rows"], 500)
        self.assertIn("--run-dir", step.command)
        self.assertIn("--progress", step.command)
        self.assertIn("成功", server.render_step(step))
        self.assertIn("到達段階", step.detail)
        self.assertIn(f"tab=results&amp;run={step.run_id}", step.detail)

    def test_failed_run_shows_hint(self):
        step = server.run_generation(
            self.state, {**GENERATE_FORM, "rows": "0"}, market=False)
        self.assertEqual(step.result.returncode, 2)
        self.assertIn("件数は1以上の整数", step.detail)
        self.assertEqual(step.run_id, "")
        self.assertFalse(self.state.runs_dir.exists())

    def test_failure_hint_from_cli_stderr(self):
        self.state = server.GuiState(base=self.root, runner=fake_runner(
            returncode=2, stderr="error: Require referral_start <= referral_end <= as_of"))
        step = server.run_generation(self.state, GENERATE_FORM, market=False)
        self.assertIn("その順で並ぶ", step.detail)


class RunInspectionTest(GuiTestCase):
    def write_run(self, name, fmt="sqlite"):
        run_dir = self.root / "runs" / "gui" / name
        (run_dir / "input").mkdir(parents=True)
        (run_dir / "input" / "config.json").write_text(json.dumps({"rows": 5}), encoding="utf-8")
        manifest = write_dataset(TINY_CONFIG, run_dir / "output", output_format=fmt)
        return run_dir, manifest

    def expected_counts(self, run_dir):
        with closing(sqlite3.connect(run_dir / "output" / SQLITE_FILENAME)) as database:
            return database.execute(
                "SELECT COUNT(*), COUNT(oubo_dt), COUNT(shorui_tsuuka_dt), COUNT(kettei_dt) FROM events"
            ).fetchone()

    def test_event_counts_match_database_across_formats(self):
        sqlite_run, _ = self.write_run("sqlite_run")
        counts = server.funnel_event_counts(sqlite_run / "output")
        self.assertEqual([counts["total"], *counts["arrivals"]], list(self.expected_counts(sqlite_run)))
        self.assertEqual(counts["total"], 5)
        csv_run, _ = self.write_run("csv_run", fmt="csv")
        csv_counts = server.funnel_event_counts(csv_run / "output")
        self.assertEqual(csv_counts["total"], counts["total"])
        self.assertEqual(csv_counts["arrivals"], counts["arrivals"])
        parquet_run, _ = self.write_run("parquet_run", fmt="parquet")
        parquet_counts = server.funnel_event_counts(parquet_run / "output")
        self.assertEqual(parquet_counts["arrivals"], counts["arrivals"])

    def test_run_entries_and_status(self):
        self.write_run("001_success")
        self.write_run("002_failure", fmt="csv")  # replaced by explicit failure below
        (self.root / "runs" / "gui" / "002_failure" / "output" / "manifest.json").unlink()
        (self.root / "runs" / "gui" / "003_pending" / "input").mkdir(parents=True)
        (self.root / "runs" / "gui" / "003_pending" / "input" / "config.json").write_text(
            json.dumps({"rows": 5}), encoding="utf-8")
        entries = {entry["id"]: entry for entry in server.run_entries(self.state)}
        self.assertEqual(entries["001_success"]["status"], "成功")
        self.assertEqual(entries["002_failure"]["status"], "失敗")
        self.assertEqual(entries["003_pending"]["status"], "未実行")
        self.assertEqual(entries["001_success"]["rows"], "5")
        self.assertEqual(entries["001_success"]["kind"], "紹介履歴")
        listing = server.render_run_list(self.state)
        self.assertIn("001_success", listing)
        self.assertIn("tab=results&amp;run=001_success", listing)

    def test_run_detail_renders_tables_manifest_and_config(self):
        run_dir, manifest = self.write_run("detail_run")
        page = server.render_run_detail(self.state, "detail_run")
        self.assertIn("状態: <strong>成功</strong>", page)
        self.assertIn("件数サマリ", page)
        self.assertIn("真の条件付き確率", page)
        self.assertIn("<details><summary>events", page)
        self.assertIn("<details><summary>truth", page)
        self.assertIn("&quot;rows&quot;: 5", page)
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
        self.assertIn("—", server.rate_table(manifest))


class PageRenderingTest(GuiTestCase):
    def test_pages_render_all_tabs_with_defaults(self):
        template = server.load_template()
        for tab in ("generate", "market", "results"):
            page = server.render_page(self.state, template, tab=tab,
                                      values=server.default_values(tab),
                                      results_html=server.render_run_list(self.state))
            self.assertIn(f'id="{tab}"', page)
            self.assertNotIn("${", page)
        generate = server.render_page(self.state, template, tab="generate",
                                      values=server.default_values("generate"))
        self.assertIn('name="rows"', generate)
        self.assertIn('name="legacy_scores"', generate)
        self.assertIn('name="advanced_config"', generate)
        market = server.render_page(self.state, template, tab="market",
                                    values=server.default_values("market"))
        self.assertIn('name="seeker_arrivals"', market)
        self.assertIn('name="legacy_models"', market)
        self.assertIn('name="retain"', market)
        results = server.render_page(self.state, template, tab="results",
                                     values=server.default_values("results"),
                                     results_html="<p>まだ実行がありません。</p>")
        self.assertIn("まだ実行がありません", results)

    def test_html_escaping_of_values(self):
        values = {**server.default_values("generate"), "referral_start": '<script>"x"'}
        page = server.render_page(self.state, server.load_template(), tab="generate", values=values)
        self.assertNotIn('<script>"x"', page)
        self.assertIn("&lt;script&gt;", page)


if __name__ == "__main__":
    unittest.main()
