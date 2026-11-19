import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from synthetic_funnel.__main__ import main

MARKET_START = "2024-01-01T00:00:00+09:00"


class CliTest(unittest.TestCase):
    def setUp(self):
        patcher = tempfile.TemporaryDirectory()
        self.root = Path(patcher.name)
        self.addCleanup(patcher.cleanup)

    def invoke(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["synthetic-funnel", *argv]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                main()
            except SystemExit as exc:
                return exc.code, stdout.getvalue(), stderr.getvalue()
        return 0, stdout.getvalue(), stderr.getvalue()

    def write_config(self, name, **overrides):
        values = {"market_start": MARKET_START, "days": 6, "warmup": 2,
                  "seeker_arrivals": 2, "job_arrivals": 2, "seed": 5}
        values.update(overrides)
        config = self.root / name / "input" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps(values), encoding="utf-8")
        return config.parent.parent

    def test_help_is_market_only(self):
        code, stdout, _ = self.invoke("--help")
        self.assertEqual(code, 0)
        self.assertIn("--seeker-arrivals", stdout)
        self.assertIn("--job-arrivals", stdout)
        self.assertNotIn("--market", stdout)
        self.assertNotIn("--rows", stdout)

    def test_run_dir_with_arrival_overrides(self):
        run_dir = self.write_config("run")
        code, stdout, stderr = self.invoke("--run-dir", str(run_dir), "--seeker-arrivals", "4",
                                           "--job-arrivals", "3")
        self.assertEqual((code, stderr), (0, ""))
        manifest = json.loads((run_dir / "output" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["config"]["seeker_arrivals"], 4)
        self.assertEqual(manifest["config"]["job_arrivals"], 3)
        self.assertNotIn("generation_seconds", manifest)  # issue #38: reproducible manifest bytes
        self.assertEqual(json.loads(stdout)["rows"],
                         manifest["row_counts"]["events"])
        self.assertNotIn("legacy_models", manifest)

    def test_output_and_config_with_arrival_overrides(self):
        run_dir = self.write_config("direct")
        output = self.root / "direct-output"
        code, stdout, stderr = self.invoke("--config", str(run_dir / "input" / "config.json"),
                                           "--output", str(output), "--format", "csv",
                                           "--seeker-arrivals", "1")
        self.assertEqual((code, stderr), (0, ""))
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["config"]["seeker_arrivals"], 1)
        self.assertEqual(manifest["config"]["job_arrivals"], 2)
        self.assertEqual(manifest["output_format"], "csv")
        self.assertTrue((output / "events.csv").is_file())
        self.assertEqual(json.loads(stdout)["rows"], manifest["row_counts"]["events"])

    def test_removed_config_keys_are_rejected(self):
        run_dir = self.write_config("legacy", legacy_models="model.json")
        code, _, stderr = self.invoke("--run-dir", str(run_dir))
        self.assertEqual(code, 2)
        self.assertIn("legacy_models", stderr)
        self.assertFalse((run_dir / "output").exists())

    def test_market_start_is_required(self):
        run_dir = self.write_config("no-start")
        config = run_dir / "input" / "config.json"
        values = json.loads(config.read_text(encoding="utf-8"))
        values.pop("market_start")
        config.write_text(json.dumps(values), encoding="utf-8")
        code, _, stderr = self.invoke("--run-dir", str(run_dir))
        self.assertEqual(code, 2)
        self.assertIn("market_start", stderr)


if __name__ == "__main__":
    unittest.main()
