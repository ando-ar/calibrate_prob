import csv
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow.parquet as pq

from synthetic_funnel import MarketConfig, write_market_dataset

MARKET_START = "2024-01-01T00:00:00+09:00"


class MarketExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.root = Path(directory.name)

    def test_three_formats_share_keys_and_truth_is_counterfactual(self):
        config = MarketConfig(days=60, seeker_arrivals=3, job_arrivals=4)
        manifests = {}
        for output_format in ("sqlite", "parquet", "csv"):
            manifests[output_format] = write_market_dataset(
                config, self.root / output_format, market_start=MARKET_START,
                output_format=output_format)

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
            truth = connection.execute(
                "SELECT * FROM events_latent ORDER BY event_id"
            ).fetchall()
            self.assertGreater(len(rows), 0)
            stages = ("application", "screening", "decision")
            for row in truth:
                for stage in stages:
                    self.assertAlmostEqual(
                        row[f"p_candidate_{stage}"] * row[f"p_employer_{stage}"],
                        row[f"p_{stage}_eventual"], places=12,
                    )
                latent = [row[f"latent_{column}"] for column in ("oubo_dt", "shorui_tsuuka_dt", "kettei_dt")]
                reached = [value is not None for value in latent]
                # Counterfactual arrivals form a prefix: no stage is reached after a failure.
                self.assertEqual(reached, sorted(reached, reverse=True))
                if row["failure_stage"] is None:
                    self.assertIsNone(row["failure_reason"])
                    self.assertEqual(reached, [True, True, True])
                else:
                    self.assertIsNotNone(row["failure_reason"])
                    if row["failure_stage"] == "application":
                        self.assertEqual(row["failure_reason"], "not_applied")
                        # The candidate-side application gate was not passed, so no latent application date exists.
                        self.assertIsNone(row["latent_oubo_dt"])
                    else:
                        self.assertIn(row["failure_reason"], ("candidate_withdrawal", "employer_rejection"))
                arrival = datetime.fromisoformat(rows[[r["event_id"] for r in rows].index(row["event_id"])]["shoukai_dt"])
                previous = arrival
                for value in latent:
                    if value is not None:
                        moment = datetime.fromisoformat(value)
                        self.assertGreaterEqual(moment, previous)
                        previous = moment
            self.assertTrue(any(row["failure_stage"] == "application" for row in truth))
            self.assertTrue(all(row["model_decision_score"] is None for row in rows))
            self.assertFalse(any(
                name in rows[0].keys() for name in
                ("seeker_skill", "required_skill", "desired_salary", "offered_salary",
                 "wants_remote", "allows_remote")))
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
            self.assertIn("truth_scope", manifest)
            self.assertIn("probability_scope", manifest)
            self.assertIn("training", manifest)
            self.assertIsInstance(manifest["generation_seconds"], float)
            self.assertGreaterEqual(manifest["generation_seconds"], 0)
            self.assertNotIn("legacy_models", manifest)
            self.assertNotIn("retention", manifest)

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

    def test_manifest_is_identical_across_formats_except_timing_and_files(self):
        config = MarketConfig(days=20, seeker_arrivals=3, job_arrivals=4, seed=5)
        manifests = [
            write_market_dataset(config, self.root / f"same-{fmt}", market_start=MARKET_START,
                                 output_format=fmt)
            for fmt in ("sqlite", "parquet", "csv")
        ]
        for manifest in manifests:
            self.assertEqual(manifests[0]["row_counts"], manifest["row_counts"])
            self.assertEqual(manifests[0]["training"], manifest["training"])
        self.assertEqual(
            json.loads((self.root / "same-csv" / "manifest.json").read_text(encoding="utf-8")),
            manifests[2],
        )

    def test_invalid_market_start_leaves_no_published_dataset(self):
        output = self.root / "bad-start"
        with self.assertRaises(ValueError):
            write_market_dataset(MarketConfig(days=1), output, market_start="2024-01-01T00:00:00")
        self.assertFalse(output.exists())

    def test_existing_output_is_not_overwritten(self):
        output = self.root / "exists"
        output.mkdir()
        with self.assertRaises(FileExistsError):
            write_market_dataset(MarketConfig(days=1), output, market_start=MARKET_START)


if __name__ == "__main__":
    unittest.main()
