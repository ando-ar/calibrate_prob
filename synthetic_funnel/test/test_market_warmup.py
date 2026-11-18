import sqlite3
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from synthetic_funnel import (
    Config,
    LegacyScores,
    MarketConfig,
    write_dataset,
    write_market_dataset,
)
from synthetic_funnel.market import simulate

MARKET_START = "2024-01-01T00:00:00+09:00"
MODEL_FIELDS = ("model_apply_score", "model_screen_score", "model_decision_score")


def _events(output):
    with closing(sqlite3.connect(output / "dataset.sqlite.db")) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM events ORDER BY event_id")]


class MarketWarmupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.root = Path(directory.name)
        model_run = cls.root / "model"
        write_dataset(
            Config(rows=2, reference_rows=1000, base_probabilities=(0.35, 0.45, 0.35),
                   probability_half_widths=(0.1, 0.1, 0.1), legacy_scores=LegacyScores(training_rows=3000)),
            model_run,
        )
        cls.model_path = model_run / "legacy_models.json"

    def output(self, name):
        return self.root / name

    def test_warmup_learning_scores_and_manifest(self):
        config = MarketConfig(days=120, warmup=60, seeker_arrivals=8, job_arrivals=8)
        output = self.output("warmup")
        manifest = write_market_dataset(config, output, market_start=MARKET_START)
        self.assertEqual(manifest["mode"], "market")
        self.assertEqual(manifest["config"]["warmup"], 60)
        self.assertEqual(manifest["config"]["training_window_days"], [1, 30, 365])
        self.assertEqual(manifest["config"]["training_referrals_per_day"], 10000)
        self.assertEqual(manifest["config"]["negative_reservoir_size"], 100000)
        self.assertNotIn("legacy_models", manifest)
        training = manifest["training"]
        self.assertEqual(training["method"], "balanced_logistic_regression")
        self.assertEqual(training["warmup"], 60)
        self.assertEqual(training["training_window_days"], [1, 30, 365])
        self.assertEqual(training["status"], "trained")
        self.assertLessEqual(datetime.fromisoformat(training["training_cutoff"]),
                             datetime.fromisoformat(training["warmup_end"]))
        self.assertEqual(len(training["stages"]), 3)

        warmup_end = training["warmup_end"]
        before = [row for row in _events(output) if row["shoukai_dt"] < warmup_end]
        after = [row for row in _events(output) if row["shoukai_dt"] >= warmup_end]
        self.assertGreater(len(before), 0)
        self.assertGreater(len(after), 0)
        for field in MODEL_FIELDS:
            self.assertTrue(all(row[field] is None for row in before))
            self.assertTrue(all(isinstance(row[field], float) and 0 <= row[field] <= 1 for row in after))

    def test_warmup_not_finished_leaves_all_scores_null(self):
        config = MarketConfig(days=30, warmup=60, seeker_arrivals=4, job_arrivals=4)
        output = self.output("no-warmup")
        manifest = write_market_dataset(config, output, market_start=MARKET_START)
        events = _events(output)
        self.assertGreater(len(events), 0)
        for field in MODEL_FIELDS:
            self.assertTrue(all(row[field] is None for row in events))
        self.assertEqual(manifest["training"]["status"], "not_trained")
        self.assertEqual(manifest["training"]["training_cutoff"], None)

    def test_legacy_models_path_keeps_legacy_entry(self):
        config = MarketConfig(days=16, seeker_arrivals=3, job_arrivals=4)
        output = self.output("legacy")
        manifest = write_market_dataset(config, output, market_start=MARKET_START,
                                        legacy_models=self.model_path)
        self.assertNotIn("training", manifest)
        self.assertEqual(manifest["legacy_models"]["method"], "balanced_logistic_regression")
        self.assertIn("sha256", manifest["legacy_models"])
        self.assertIn("training_cutoff", manifest["legacy_models"])
        events = _events(output)
        self.assertTrue(all(isinstance(row["model_decision_score"], float) for row in events))
        self.assertTrue(all(row["model_apply_score"] is not None for row in events))

    def test_same_seed_reproduces_scores_and_row_counts(self):
        config = MarketConfig(days=100, warmup=60, seeker_arrivals=8, job_arrivals=8)
        first = self.output("determinism-1")
        second = self.output("determinism-2")
        first_manifest = write_market_dataset(config, first, market_start=MARKET_START)
        second_manifest = write_market_dataset(config, second, market_start=MARKET_START)
        self.assertEqual(first_manifest["row_counts"], second_manifest["row_counts"])
        self.assertEqual(first_manifest["training"], second_manifest["training"])
        first_events, second_events = _events(first), _events(second)
        self.assertEqual(len(first_events), len(second_events))
        for key in ("event_id", "shoukai_dt", "oubo_dt", *MODEL_FIELDS):
            self.assertEqual([row[key] for row in first_events], [row[key] for row in second_events])

    def test_warmup_settings_do_not_change_market_randomness(self):
        base = MarketConfig(days=60, seeker_arrivals=5, job_arrivals=6, warmup=0)
        trained = MarketConfig(days=60, seeker_arrivals=5, job_arrivals=6, warmup=60,
                               training_window_days=(2, 10, 20), negative_reservoir_size=50)
        first, seekers, jobs = simulate(base)
        second, other_seekers, other_jobs = simulate(trained)
        for key in first.dtype.names:
            np.testing.assert_array_equal(first[key], second[key])
        np.testing.assert_array_equal(seekers, other_seekers)
        np.testing.assert_array_equal(jobs, other_jobs)


if __name__ == "__main__":
    unittest.main()
