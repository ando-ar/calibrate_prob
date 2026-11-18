"""Issue #40: independent, byte-reproducible concurrent seed generation."""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from synthetic_funnel import MarketConfig, generate_seeds, write_market_dataset
from synthetic_funnel.__main__ import main

MARKET_START = "2024-01-01T00:00:00+09:00"
BASE = {"market_start": MARKET_START, "days": 6, "warmup": 2,
        "seeker_arrivals": 2, "job_arrivals": 2, "seed": 5}


class SeedBatchTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(BASE), encoding="utf-8")

    def test_parallel_seed_is_byte_identical_to_solo_run(self):
        summaries = generate_seeds(self.config, self.root / "batch", (71, 72), jobs=2)
        self.assertEqual(set(summaries), {71, 72})
        for seed in (71, 72):
            manifest = json.loads((self.root / "batch" / f"seed-{seed}" / "manifest.json").read_text())
            self.assertEqual(manifest["config"]["seed"], seed)
            self.assertEqual(manifest["row_counts"]["events"], summaries[seed]["rows"])

        solo = MarketConfig(days=6, warmup=2, seeker_arrivals=2, job_arrivals=2, seed=71)
        write_market_dataset(solo, self.root / "solo", market_start=MARKET_START)
        for name in ("dataset.sqlite.db", "manifest.json"):
            self.assertEqual((self.root / "batch" / "seed-71" / name).read_bytes(),
                             (self.root / "solo" / name).read_bytes(), name)

        # The seeds are genuinely independent, not copies of a single run.
        self.assertNotEqual((self.root / "batch" / "seed-71" / "manifest.json").read_bytes(),
                            (self.root / "batch" / "seed-72" / "manifest.json").read_bytes())

    def test_cli_generates_one_directory_per_seed_with_jobs_bound(self):
        stdout = io.StringIO()
        argv = ["synthetic-funnel", "--config", str(self.config), "--output", str(self.root / "cli"),
                "--seeds", "71,72,73", "--jobs", "1"]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout):
            main()
        summary = json.loads(stdout.getvalue())
        self.assertEqual(set(summary), {"71", "72", "73"})
        for seed in (71, 72, 73):
            manifest = json.loads((self.root / "cli" / f"seed-{seed}" / "manifest.json").read_text())
            self.assertEqual(manifest["config"]["seed"], seed)

    def test_batch_arguments_are_validated(self):
        with self.assertRaises(ValueError):
            generate_seeds(self.config, self.root / "bad", ())
        with self.assertRaises(ValueError):
            generate_seeds(self.config, self.root / "bad", (71, 71))
        with self.assertRaises(ValueError):
            generate_seeds(self.config, self.root / "bad", (71,), jobs=0)


if __name__ == "__main__":
    unittest.main()
