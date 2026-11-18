"""Issue #38: indexed evaluation lookups and byte-reproducible manifests."""

import sqlite3
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from synthetic_funnel import MarketConfig, write_market_dataset
from synthetic_funnel.market_candidates import DAILY_CANDIDATES_LATENT_SQL, candidate_rows

MARKET_START = "2025-01-01T00:00:00+09:00"
START = datetime.fromisoformat(MARKET_START)
# Large enough that full table scans of events are clearly measurable, small
# enough to keep the suite fast (the unindexed plan takes tens of seconds per
# day at this size; the indexed plan takes milliseconds).
CONFIG = MarketConfig(days=30, warmup=10, seeker_arrivals=20, job_arrivals=20, seed=71)
EXPECTED_INDEXES = {
    "events_shoukai_dt": ["shoukai_dt"],
    "idx_events_user": ["user_id", "oubo_dt", "kettei_dt", "outcome_dt"],
    "idx_events_item": ["item_id", "oubo_dt", "kettei_dt", "outcome_dt"],
    "idx_events_user_item": ["user_id", "item_id"],
    "idx_user_occupation": ["occupation", "arrival_dt", "departure_dt"],
    "idx_item_occupation": ["occupation", "arrival_dt", "departure_dt"],
}


class Issue38PerformanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.root = Path(directory.name)
        cls.dataset = cls.root / "dataset"
        write_market_dataset(CONFIG, cls.dataset, market_start=MARKET_START)
        # Candidate pairs only appear after the first arrival cohorts overlap;
        # these offsets all have a non-empty latent candidate set.
        cls.days = (25, 26, 27)
        cls.day = (START + timedelta(days=cls.days[-1])).isoformat()

    def connect(self):
        return sqlite3.connect(self.dataset / "dataset.sqlite.db")

    def test_export_creates_candidate_recipe_indexes(self):
        with closing(self.connect()) as connection:
            names = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")}
            for name, columns in EXPECTED_INDEXES.items():
                self.assertIn(name, names)
                self.assertEqual([row[2] for row in connection.execute(f"PRAGMA index_info({name})")], columns)

    def test_latent_extraction_plan_uses_indexes(self):
        with closing(self.connect()) as connection:
            plan = connection.execute("EXPLAIN QUERY PLAN " + DAILY_CANDIDATES_LATENT_SQL,
                                      {"day": self.day}).fetchall()
        detail = " | ".join(row[3] for row in plan)
        for index in ("idx_events_user", "idx_events_item", "idx_events_user_item"):
            self.assertIn(index, detail)
        for table in ("e", "o", "c"):  # NOT EXISTS and the two correlated COUNT subqueries
            self.assertNotIn(f"SCAN {table}", detail)

    def test_latent_extraction_within_time_budget(self):
        budget = 5.0  # seconds for three evaluation days on this fixture
        with closing(self.connect()) as connection:
            started = time.perf_counter()
            rows = [candidate_rows(connection, (START + timedelta(days=offset)).isoformat(), latent=True)
                    for offset in self.days]
            elapsed = time.perf_counter() - started
        self.assertTrue(all(rows), "fixture produced no latent candidates")
        self.assertLess(elapsed, budget, f"latent extraction took {elapsed:.2f}s (budget {budget}s)")


class ManifestReproducibilityTest(unittest.TestCase):
    def test_same_config_and_seed_produce_identical_manifest_bytes(self):
        config = MarketConfig(days=12, warmup=4, seeker_arrivals=5, job_arrivals=5, seed=71)
        byte_strings = []
        with TemporaryDirectory() as root:
            for name in ("first", "second"):
                output = Path(root) / name
                write_market_dataset(config, output, market_start=MARKET_START)
                byte_strings.append((output / "manifest.json").read_bytes())
        self.assertEqual(byte_strings[0], byte_strings[1])
