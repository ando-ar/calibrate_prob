import sqlite3
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from synthetic_funnel import MarketConfig, write_market_dataset
from synthetic_funnel.market import simulate

MARKET_START = "2024-01-01T00:00:00+09:00"
MODEL_FIELDS = ("model_apply_score", "model_screen_score", "model_decision_score")


def assert_profiles_equal(first, second):
    """Object-dtype profiles hold NaN departures; compare NaNs as equal."""
    assert first.shape == second.shape
    np.testing.assert_array_equal(first.astype(str), second.astype(str))


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

    def output(self, name):
        return self.root / name

    def test_warmup_learning_scores_and_manifest(self):
        # Eight occupation/region categories make introductions much sparser; the
        # larger market is needed for at least two observed decisions in warmup.
        # issue #40 changed the market RNG stream, and issue #46 (segment/drift)
        # lowers the warmup decision rate, so the training-capable seed was
        # re-selected again (seed 42 keeps at least two observed decisions).
        config = MarketConfig(days=240, warmup=60, seeker_arrivals=20, job_arrivals=20, seed=42)
        output = self.output("warmup")
        manifest = write_market_dataset(config, output, market_start=MARKET_START)
        self.assertEqual(manifest["mode"], "market")
        self.assertEqual(manifest["config"]["warmup"], 60)
        self.assertEqual(manifest["config"]["training_window_days"], [1, 30, 365])
        self.assertEqual(manifest["config"]["training_referrals_per_day"], 10000)
        self.assertEqual(manifest["config"]["negative_reservoir_size"], 100000)
        self.assertNotIn("legacy_models", manifest)
        self.assertNotIn("retention", manifest)
        training = manifest["training"]
        self.assertEqual(training["method"], "balanced_logistic_regression")
        self.assertEqual(training["warmup"], 60)
        self.assertEqual(training["training_window_days"], [1, 30, 365])
        self.assertEqual(training["status"], "trained")
        self.assertLessEqual(datetime.fromisoformat(training["training_cutoff"]),
                             datetime.fromisoformat(training["warmup_end"]))
        self.assertEqual(len(training["stages"]), 3)
        self.assertEqual(manifest["training_cutoff"], training["training_cutoff"])
        self.assertEqual(manifest["warmup_end"], training["warmup_end"])
        self.assertNotIn("generation_seconds", manifest)  # issue #38: reproducible manifest bytes

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
        assert_profiles_equal(seekers, other_seekers)
        assert_profiles_equal(jobs, other_jobs)


if __name__ == "__main__":
    unittest.main()
