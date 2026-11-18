import csv
import json
import math
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import pyarrow.parquet as pq

from synthetic_funnel import MarketConfig, OCCUPATIONS, REGIONS, write_market_dataset
from synthetic_funnel.market import DTYPE, FEE_RATE_DEFAULT, OUTCOME_STATUS_CODES, simulate
from synthetic_funnel.market_export import EVENTS_LATENT_TABLE, EVENTS_TABLE, ITEM_TABLE, _event_records

OUTCOME_STATUSES = ('not_applied', 'ng', 'withdrawn', 'cancelled_posting_expiry',
                    'cancelled_seeker_exit', 'cancelled_market_end')

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
            for table in ("user", "item"):
                columns = {row[1]: row[2] for row in connection.execute(f"PRAGMA table_info({table})")}
                self.assertEqual(columns["occupation"], "TEXT")
                self.assertEqual(columns["region"], "TEXT")
                sample = connection.execute(f"SELECT occupation, region FROM {table} LIMIT 50").fetchall()
                self.assertTrue(all(occupation in OCCUPATIONS and region in REGIONS
                                    for occupation, region in sample))
            # Motivation is immutable, so it lives in dedicated 1:1 latent tables rather
            # than in the masters or duplicated on every events_latent row.
            for master, latent, id_column, motivation in (
                ("user", "user_latent", "user_id", "user_motivation"),
                ("item", "item_latent", "item_id", "item_motivation"),
            ):
                self.assertEqual(connection.execute(f"SELECT count(*) FROM {latent}").fetchone()[0],
                                 connection.execute(f"SELECT count(*) FROM {master}").fetchone()[0])
                self.assertEqual(connection.execute(
                    f"SELECT {id_column} FROM {latent} EXCEPT SELECT {id_column} FROM {master}").fetchall(), [])
                self.assertEqual(connection.execute(
                    f"SELECT {id_column} FROM {master} EXCEPT SELECT {id_column} FROM {latent}").fetchall(), [])
                values = [row[0] for row in connection.execute(f"SELECT {motivation} FROM {latent}")]
                self.assertTrue(all(isinstance(value, float) and math.isfinite(value) for value in values))
                self.assertGreater(len(values), 1)
                self.assertLess(abs(np.mean(values)), 0.2)
                self.assertTrue(0.2 < np.std(values) < 0.9)
                latent_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({latent})")}
                self.assertEqual(latent_columns, {id_column, motivation})
                master_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({master})")}
                self.assertNotIn(f"{master}_affinity", master_columns)
                self.assertNotIn(f"{master}_affinity", latent_columns)
            latent_columns = {row[1] for row in connection.execute("PRAGMA table_info(events_latent)")}
            self.assertNotIn("user_affinity", latent_columns)
            self.assertNotIn("item_affinity", latent_columns)
            # Skill-derived features belong to events_latent; computable ones stay in events.
            events_columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
            self.assertTrue({"salary_shortfall", "remote_mismatch", "other_applications",
                             "competing_candidates"} <= events_columns)
            self.assertFalse({"skill_match", "skill_shortfall", "overqualification"} & events_columns)
            self.assertTrue({"skill_match", "skill_shortfall", "overqualification"} <= latent_columns)
            # The moved values are unchanged: each latent feature still matches the
            # proxy skills in the masters, and the observable ones match published values.
            for salary_shortfall, skill_match, skill_shortfall, overqualification, \
                    user_skill, required_skill, desired_salary, offered_salary in connection.execute(
                "SELECT e.salary_shortfall, l.skill_match, l.skill_shortfall, l.overqualification, "
                "       u.user_skill, i.required_skill, u.desired_salary, i.offered_salary "
                "FROM events AS e JOIN events_latent AS l USING (event_id) "
                "JOIN user AS u USING (user_id) JOIN item AS i USING (item_id)"
            ):
                self.assertAlmostEqual(skill_match, 1 - abs(user_skill - required_skill), places=12)
                self.assertAlmostEqual(skill_shortfall, max(required_skill - user_skill, 0), places=12)
                self.assertAlmostEqual(overqualification, max(user_skill - required_skill, 0), places=12)
                self.assertAlmostEqual(salary_shortfall, max((desired_salary - offered_salary) / desired_salary, 0),
                                       places=12)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM events AS e "
                "JOIN user AS u USING (user_id) JOIN user_latent AS ul USING (user_id) "
                "JOIN item AS i USING (item_id) JOIN item_latent AS il USING (item_id) "
                "WHERE ul.user_motivation IS NULL OR il.item_motivation IS NULL").fetchone()[0], 0)
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
            self.assertEqual(set(manifest["row_counts"]),
                             {"events", "events_latent", "user", "user_latent", "item", "item_latent", "daily"})
            self.assertEqual(manifest["row_counts"]["events"], events[0])
            self.assertEqual(manifest["row_counts"]["events_latent"], events[0])
            self.assertEqual(manifest["row_counts"]["user"],
                             connection.execute("SELECT count(*) FROM user").fetchone()[0])
            self.assertEqual(manifest["row_counts"]["item"],
                             connection.execute("SELECT count(*) FROM item").fetchone()[0])
            self.assertEqual(manifest["row_counts"]["user_latent"],
                             connection.execute("SELECT count(*) FROM user_latent").fetchone()[0])
            self.assertEqual(manifest["row_counts"]["item_latent"],
                             connection.execute("SELECT count(*) FROM item_latent").fetchone()[0])
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
        for table in ("user", "item"):
            schema = pq.read_schema(self.root / "parquet" / f"{table}.parquet")
            self.assertEqual(str(schema.field("occupation").type), "string")
            self.assertEqual(str(schema.field("region").type), "string")
        users_table = pq.read_table(self.root / "parquet" / "user.parquet")
        self.assertTrue(users_table.schema.field("departure_dt").nullable)
        users = users_table.column("departure_dt").to_pylist()
        self.assertEqual(users.count(None), live_users)
        with (self.root / "csv" / "user.csv").open(encoding="utf-8", newline="") as stream:
            csv_users = list(csv.DictReader(stream))
        self.assertEqual(sum(row["departure_dt"] == "" for row in csv_users), users.count(None))
        self.assertTrue(all(row["occupation"] in OCCUPATIONS and row["region"] in REGIONS
                            for row in csv_users))
        self.assertTrue((self.root / "csv" / "user.csv").is_file())
        self.assertTrue((self.root / "parquet" / "item.parquet").is_file())
        # The two latent tables are published in all three formats with the same ids.
        for table, id_column, motivation in (
            ("user_latent", "user_id", "user_motivation"),
            ("item_latent", "item_id", "item_motivation"),
        ):
            parquet_latent = pq.read_table(self.root / "parquet" / f"{table}.parquet")
            with (self.root / "csv" / f"{table}.csv").open(encoding="utf-8", newline="") as stream:
                csv_latent = list(csv.DictReader(stream))
            with closing(sqlite3.connect(self.root / "sqlite" / "dataset.sqlite.db")) as connection:
                sqlite_latent = connection.execute(f"SELECT {id_column}, {motivation} FROM {table}").fetchall()
            self.assertEqual(set(parquet_latent[id_column].to_pylist()),
                             {row[id_column] for row in csv_latent})
            self.assertEqual(set(parquet_latent[id_column].to_pylist()),
                             {row[0] for row in sqlite_latent})
            self.assertEqual(len(parquet_latent), len(csv_latent))
            self.assertEqual(len(parquet_latent), len(sqlite_latent))
            csv_motivations = {row[id_column]: float(row[motivation]) for row in csv_latent}
            sqlite_motivations = {row[0]: row[1] for row in sqlite_latent}
            self.assertEqual(csv_motivations, sqlite_motivations)
            for key, value in zip(parquet_latent[id_column].to_pylist(),
                                  parquet_latent[motivation].to_pylist(), strict=True):
                self.assertAlmostEqual(value, sqlite_motivations[key], places=12)

    def test_daily_funnel_sums_match_exported_dates(self):
        config = MarketConfig(days=24, seeker_arrivals=6, job_arrivals=6)
        rows, _, _ = simulate(config)
        expected_applications = int(np.isfinite(rows["application"]).sum())
        output = self.root / "counts"
        write_market_dataset(config, output, market_start=MARKET_START)
        with closing(sqlite3.connect(output / "dataset.sqlite.db")) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("SELECT sum(referrals) FROM daily").fetchone()[0],
                             connection.execute("SELECT count(*) FROM events").fetchone()[0])
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM events WHERE oubo_dt IS NOT NULL").fetchone()[0],
                expected_applications)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM events WHERE oubo_dt IS NOT NULL AND oubo_dt < shoukai_dt").fetchone()[0], 0)

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


    def test_outcome_columns_are_consistent_across_formats(self):
        config = MarketConfig(days=40, seeker_arrivals=4, job_arrivals=5, seed=71)
        paths = {}
        for output_format in ("sqlite", "parquet", "csv"):
            output = self.root / f"outcomes-{output_format}"
            write_market_dataset(config, output, market_start=MARKET_START,
                                 output_format=output_format)
            paths[output_format] = output
        self.assertTrue({"status", "outcome_dt", "auto"} <= set(EVENTS_TABLE.fields))
        self.assertNotIn("status", EVENTS_LATENT_TABLE.fields)
        self.assertNotIn("outcome_dt", EVENTS_LATENT_TABLE.fields)
        self.assertNotIn("auto", EVENTS_LATENT_TABLE.fields)

        def normalized(status, outcome, auto):
            return (status or None, outcome or None, int(auto))

        with closing(sqlite3.connect(paths["sqlite"] / "dataset.sqlite.db")) as connection:
            columns = {row[1]: row[2] for row in connection.execute("PRAGMA table_info(events)")}
            self.assertEqual(columns["status"], "TEXT")
            self.assertEqual(columns["outcome_dt"], "TEXT")
            self.assertEqual(columns["auto"], "INTEGER")
            sqlite_outcomes = {
                event_id: normalized(status, outcome, auto)
                for event_id, status, outcome, auto in connection.execute(
                    "SELECT event_id, status, outcome_dt, auto FROM events")
            }
        for status, outcome, auto in sqlite_outcomes.values():
            self.assertTrue(status is None or status in OUTCOME_STATUSES)
            self.assertEqual(status is None, outcome is None)
            if status is None:
                self.assertEqual(auto, 0)
            if auto == 1:
                self.assertIn(status, ("ng", "withdrawn"))
        self.assertTrue(any(value[0] is not None for value in sqlite_outcomes.values()))
        with closing(sqlite3.connect(paths["sqlite"] / "dataset.sqlite.db")) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM events WHERE status IS NOT NULL AND kettei_dt IS NOT NULL"
            ).fetchone()[0], 0)

        parquet = pq.read_table(paths["parquet"] / "events.parquet")
        parquet_outcomes = {
            event_id: normalized(status, outcome, auto)
            for event_id, status, outcome, auto in zip(
                parquet["event_id"].to_pylist(), parquet["status"].to_pylist(),
                parquet["outcome_dt"].to_pylist(), parquet["auto"].to_pylist(), strict=True)
        }
        with (paths["csv"] / "events.csv").open(encoding="utf-8", newline="") as stream:
            csv_outcomes = {
                row["event_id"]: normalized(row["status"], row["outcome_dt"], row["auto"])
                for row in csv.DictReader(stream)
            }
        self.assertEqual(parquet_outcomes, sqlite_outcomes)
        self.assertEqual(csv_outcomes, sqlite_outcomes)

    def test_fee_columns_are_consistent_across_formats(self):
        def high_rates(config,f,*args):
            return np.tile(np.array([[.9,1.],[.95,.95],[.95,.95]]),(len(f['skill_match']),1,1))
        config = MarketConfig(days=60, seeker_arrivals=4, job_arrivals=5, seed=71)
        paths = {}
        with patch('synthetic_funnel.market.probability_arrays',side_effect=high_rates):
            for output_format in ("sqlite", "parquet", "csv"):
                output = self.root / f"fees-{output_format}"
                write_market_dataset(config, output, market_start=MARKET_START,
                                     output_format=output_format)
                paths[output_format] = output
        self.assertIn("fee_rate", ITEM_TABLE.fields)
        self.assertIn("fee_yen", EVENTS_TABLE.fields)
        with closing(sqlite3.connect(paths["sqlite"] / "dataset.sqlite.db")) as connection:
            item_columns = {row[1]: row[2] for row in connection.execute("PRAGMA table_info(item)")}
            self.assertEqual(item_columns["fee_rate"], "REAL")
            self.assertEqual(connection.execute("SELECT DISTINCT fee_rate FROM item").fetchall(),
                             [(FEE_RATE_DEFAULT,)])
            expected = {
                event_id: fee for event_id, fee in connection.execute(
                    "SELECT e.event_id, i.offered_salary * i.fee_rate "
                    "FROM events AS e JOIN item AS i USING (item_id) "
                    "WHERE e.kettei_dt IS NOT NULL")
            }
            self.assertGreater(len(expected), 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM events WHERE kettei_dt IS NOT NULL").fetchone()[0], len(expected))
            sqlite_fees = dict(connection.execute(
                "SELECT event_id, fee_yen FROM events WHERE fee_yen IS NOT NULL"))
            self.assertEqual(set(sqlite_fees), set(expected))
            for event_id, fee in expected.items():
                self.assertAlmostEqual(sqlite_fees[event_id], fee, places=12)
        item_parquet = pq.read_table(paths["parquet"] / "item.parquet")
        self.assertTrue(all(rate == FEE_RATE_DEFAULT for rate in item_parquet["fee_rate"].to_pylist()))
        parquet = pq.read_table(paths["parquet"] / "events.parquet")
        parquet_fees = {
            event_id: fee for event_id, fee in zip(parquet["event_id"].to_pylist(),
                                                   parquet["fee_yen"].to_pylist(), strict=True)
            if fee is not None
        }
        with (paths["csv"] / "events.csv").open(encoding="utf-8", newline="") as stream:
            csv_fees = {row["event_id"]: float(row["fee_yen"]) for row in csv.DictReader(stream)
                        if row["fee_yen"] != ""}
        for fees in (parquet_fees, csv_fees):
            self.assertEqual(set(fees), set(expected))
            for event_id, fee in expected.items():
                self.assertAlmostEqual(fees[event_id], fee, places=12)

    def test_every_status_code_maps_to_its_enum_text(self):
        count = len(OUTCOME_STATUSES)
        rows = np.zeros(count, dtype=DTYPE)
        rows['seeker'] = np.arange(count)
        rows['job'] = np.arange(count)
        rows['day'] = 1.0
        rows['status'] = np.arange(1, count + 1)
        rows['outcome_dt'] = 2.5
        rows['auto'] = [0, 1, 1, 0, 0, 0]
        seekers = np.empty((count, 8), dtype=object)
        jobs = np.empty((count, 8), dtype=object)
        profile = [0., .5, 500., 'エンジニア', '東京', 0, 0., np.nan]
        for index in range(count):
            seekers[index] = profile
            jobs[index] = profile
        scores = np.zeros((count, 3))
        events = [event for event, _ in _event_records(
            rows, seekers, jobs, datetime.fromisoformat(MARKET_START), scores, None)]
        self.assertEqual([event['status'] for event in events], list(OUTCOME_STATUSES))
        self.assertTrue(all(event['outcome_dt'] is not None for event in events))
        self.assertEqual([event['auto'] for event in events], [0, 1, 1, 0, 0, 0])
        self.assertEqual(OUTCOME_STATUS_CODES['cancelled_market_end'], count)


if __name__ == "__main__":
    unittest.main()
