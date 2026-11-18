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
        config = MarketConfig(days=60, seeker_arrivals=3, job_arrivals=4)
        manifests = {}
        for output_format in ("sqlite", "parquet", "csv"):
            manifests[output_format] = write_market_dataset(
                config, self.root / output_format, market_start=MARKET_START,
                legacy_models=self.model_path, output_format=output_format)

        with closing(sqlite3.connect(self.root / "sqlite" / "dataset.sqlite.db")) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            events = connection.execute(
                "SELECT count(*), count(oubo_dt), count(shorui_tsuuka_dt), count(kettei_dt) FROM events"
            ).fetchone()
            funnel = connection.execute(
                "SELECT sum(referrals), sum(applications), sum(screenings), sum(decisions) FROM daily"
            ).fetchone()
            self.assertEqual(tuple(funnel), tuple(events))
            self.assertEqual(connection.execute("SELECT count(*) FROM daily").fetchone()[0], config.days)
            self.assertEqual(connection.execute("SELECT sum(new_users) FROM daily").fetchone()[0],
                             connection.execute("SELECT count(*) FROM user").fetchone()[0])
            self.assertEqual(connection.execute("SELECT sum(new_items) FROM daily").fetchone()[0],
                             connection.execute("SELECT count(*) FROM item").fetchone()[0])
            self.assertEqual(connection.execute("SELECT count(*) FROM events_latent").fetchone()[0],
                             events[0])
            departed = connection.execute(
                "SELECT count(*) FROM user WHERE departure_dt IS NOT NULL").fetchone()[0]
            self.assertGreater(departed, 0)
            live_users = connection.execute(
                "SELECT count(*) FROM user WHERE departure_dt IS NULL").fetchone()[0]
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM user WHERE departure_dt IS NOT NULL AND departure_dt <= arrival_dt"
            ).fetchone()[0], 0)
            rows = connection.execute("SELECT * FROM events ORDER BY shoukai_dt, event_id").fetchall()
            unknown = connection.execute(
                "SELECT failure_reason, latent_kettei_dt FROM events_latent").fetchall()
            self.assertTrue(all(tuple(row) == (None, None) for row in unknown))
            self.assertTrue(all(0 <= row["model_decision_score"] <= 1 for row in rows))
            self.assertFalse(any(
                name in rows[0].keys() for name in
                ("seeker_skill", "required_skill", "desired_salary", "offered_salary",
                 "wants_remote", "allows_remote")))
            self.assertGreater(len(rows), 0)
            first = datetime.fromisoformat(rows[0]["shoukai_dt"])
            self.assertEqual(first.utcoffset(), datetime.fromisoformat(MARKET_START).utcoffset())
            self.assertEqual((first.hour, first.minute, first.second), (0, 14, 24))
            manifest = manifests["sqlite"]
            self.assertEqual(set(manifest["row_counts"]), {"events", "events_latent", "user", "item", "daily"})
            self.assertEqual(manifest["row_counts"]["events"], events[0])
            self.assertEqual(manifest["row_counts"]["events_latent"], events[0])
            self.assertEqual(manifest["row_counts"]["user"],
                             connection.execute("SELECT count(*) FROM user").fetchone()[0])
            self.assertEqual(manifest["row_counts"]["item"],
                             connection.execute("SELECT count(*) FROM item").fetchone()[0])
            self.assertEqual(manifest["row_counts"]["daily"], config.days)

        parquet = pq.read_table(self.root / "parquet" / "events.parquet")
        with (self.root / "csv" / "events.csv").open(encoding="utf-8", newline="") as stream:
            csv_rows = list(csv.DictReader(stream))
        self.assertEqual(set(parquet["event_id"].to_pylist()), {row["event_id"] for row in rows})
        self.assertEqual({row["event_id"] for row in csv_rows}, {row["event_id"] for row in rows})
        users_table = pq.read_table(self.root / "parquet" / "user.parquet")
        self.assertTrue(users_table.schema.field("departure_dt").nullable)
        users = users_table.column("departure_dt").to_pylist()
        self.assertEqual(users.count(None), live_users)
        with (self.root / "csv" / "user.csv").open(encoding="utf-8", newline="") as stream:
            csv_users = list(csv.DictReader(stream))
        self.assertEqual(sum(row["departure_dt"] == "" for row in csv_users), users.count(None))
        self.assertTrue((self.root / "csv" / "user.csv").is_file())
        self.assertTrue((self.root / "parquet" / "item.parquet").is_file())

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
