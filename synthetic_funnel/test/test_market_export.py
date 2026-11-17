import csv
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow.parquet as pq

from synthetic_funnel import (
    Config,
    LegacyScores,
    MarketConfig,
    write_dataset,
    write_market_dataset,
)

MARKET_START = "2024-01-01T00:00:00+09:00"


class MarketExportTest(unittest.TestCase):
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

    def test_three_formats_share_keys_and_keep_unknown_truth_null(self):
        config = MarketConfig(days=16, seeker_arrivals=3, job_arrivals=4)
        for output_format in ("sqlite", "parquet", "csv"):
            write_market_dataset(config, self.root / output_format, market_start=MARKET_START,
                                 legacy_models=self.model_path, output_format=output_format)

        with closing(sqlite3.connect(self.root / "sqlite" / "dataset.sqlite.db")) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("SELECT count(*) FROM daily").fetchone()[0], config.days)
            self.assertEqual(connection.execute("SELECT sum(referrals) FROM daily").fetchone()[0],
                             connection.execute("SELECT count(*) FROM events").fetchone()[0])
            rows = connection.execute("SELECT * FROM events ORDER BY shoukai_dt, event_id").fetchall()
            unknown = connection.execute("SELECT failure_reason, latent_kettei_dt FROM truth").fetchall()
            self.assertTrue(all(tuple(row) == (None, None) for row in unknown))
            self.assertTrue(all(0 <= row["legacy_decision_score"] <= 1 for row in rows))
            self.assertGreater(len(rows), 0)
            first = datetime.fromisoformat(rows[0]["shoukai_dt"])
            self.assertEqual(first.utcoffset(), datetime.fromisoformat(MARKET_START).utcoffset())
            self.assertEqual((first.hour, first.minute, first.second), (0, 14, 24))

        parquet = pq.read_table(self.root / "parquet" / "events.parquet")
        with (self.root / "csv" / "events.csv").open(encoding="utf-8", newline="") as stream:
            csv_rows = list(csv.DictReader(stream))
        self.assertEqual(set(parquet["event_id"].to_pylist()), {row["event_id"] for row in rows})
        self.assertEqual({row["event_id"] for row in csv_rows}, {row["event_id"] for row in rows})
        self.assertTrue((self.root / "csv" / "seekers.csv").is_file())
        self.assertTrue((self.root / "parquet" / "jobs.parquet").is_file())

    def test_rejects_cutoff_after_market_start_without_publishing_output(self):
        output = self.root / "too-early"
        with self.assertRaisesRegex(ValueError, "training_cutoff must be at or before market_start"):
            write_market_dataset(MarketConfig(days=1), output, market_start="2023-12-31T14:59:59+00:00",
                                 legacy_models=self.model_path)
        self.assertFalse(output.exists())

    def test_rejects_malformed_model_even_when_market_has_no_referrals(self):
        model = json.loads(self.model_path.read_text(encoding="utf-8"))
        model["models"][0]["scale"][0] = 0.0
        invalid = self.root / "invalid.json"
        invalid.write_text(json.dumps(model), encoding="utf-8")
        output = self.root / "invalid"
        with self.assertRaisesRegex(ValueError, "needs positive scales"):
            write_market_dataset(MarketConfig(days=1, referrals=0), output, market_start=MARKET_START,
                                 legacy_models=invalid)
        self.assertFalse(output.exists())
