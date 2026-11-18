import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from synthetic_funnel import MarketConfig, write_market_dataset
from synthetic_funnel.market import simulate
from synthetic_funnel.market_candidates import (
    BATCH_DAY_TABLE_SQL,
    DAILY_CANDIDATES_LATENT_SQL,
    DAILY_CANDIDATES_SQL,
    DAILY_CANDIDATES_VIEW_SQL,
    attach_latent_truth,
    candidate_rows,
)

MARKET_START = "2024-01-01T00:00:00+09:00"
START = datetime.fromisoformat(MARKET_START)
DAY_INDEX = 10
DAY = (START + timedelta(days=DAY_INDEX)).isoformat()
DAY_END = (START + timedelta(days=DAY_INDEX + 1)).isoformat()
CONFIG = MarketConfig(days=30, seeker_arrivals=4, job_arrivals=30, seed=11, referrals=1)
OBSERVED_COLUMNS = {
    "day_dt", "user_id", "item_id", "seeker_skill", "required_skill", "desired_salary",
    "offered_salary", "wants_remote", "allows_remote", "salary_shortfall", "remote_mismatch",
    "other_applications", "competing_candidates",
}
LATENT_COLUMNS = OBSERVED_COLUMNS | {
    "skill_match", "skill_shortfall", "overqualification", "user_motivation", "item_motivation",
}


class DailyCandidatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.root = Path(directory.name)
        cls.manifest = write_market_dataset(CONFIG, cls.root / "dataset", market_start=MARKET_START)
        cls.seed = cls.manifest["config"]["seed"]

    def connect(self, *, memory=False):
        source = sqlite3.connect(self.root / "dataset" / "dataset.sqlite.db")
        if not memory:
            return source
        target = sqlite3.connect(":memory:")
        source.backup(target)
        source.close()
        return target

    def expected_pool(self, connection):
        """Independently reconstruct the issue's pool condition in Python."""
        users = connection.execute(
            "SELECT user_id, occupation, region, desired_salary FROM user "
            "WHERE arrival_dt <= ? AND (departure_dt IS NULL OR departure_dt > ?)", (DAY, DAY)
        ).fetchall()
        items = connection.execute(
            "SELECT item_id, occupation, region, allows_remote, offered_salary FROM item "
            "WHERE arrival_dt <= ? AND (departure_dt IS NULL OR departure_dt > ?)", (DAY, DAY)
        ).fetchall()
        introduced = {(u, i) for u, i in connection.execute("SELECT user_id, item_id FROM events")}
        by_occupation = {}
        for item in items:
            by_occupation.setdefault(item[1], []).append(item)
        expected = set()
        for user_id, occupation, region, desired_salary in users:
            for item_id, _, item_region, allows_remote, offered_salary in by_occupation.get(occupation, ()):
                if not (item_region == region or allows_remote == 1):
                    continue
                if max(100, offered_salary - 100) > desired_salary:
                    continue
                if (user_id, item_id) in introduced:
                    continue
                expected.add((user_id, item_id))
        return expected

    def test_recipe_reproduces_introduction_pool(self):
        with closing(self.connect()) as connection:
            rows = candidate_rows(connection, DAY)
            self.assertGreater(len(rows), 0)
            self.assertEqual(set(rows[0]), OBSERVED_COLUMNS)
            self.assertEqual({(row["user_id"], row["item_id"]) for row in rows}, self.expected_pool(connection))
            self.assertTrue(all(row["day_dt"] == DAY for row in rows))

    def test_day_d_introductions_belong_to_pre_introduction_pool(self):
        with closing(self.connect(memory=True)) as connection:
            introduced = connection.execute(
                "SELECT user_id, item_id FROM events WHERE shoukai_dt >= ? AND shoukai_dt < ?", (DAY, DAY_END)
            ).fetchall()
            self.assertGreater(len(introduced), 0)
            connection.execute("DELETE FROM events WHERE shoukai_dt >= ?", (DAY,))
            pool = {(row["user_id"], row["item_id"]) for row in candidate_rows(connection, DAY)}
            self.assertTrue(set(introduced) <= pool)

    def test_snapshot_counts_match_observed_events(self):
        with closing(self.connect()) as connection:
            rows = candidate_rows(connection, DAY)
            for row in rows:
                other = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE user_id = ? AND oubo_dt IS NOT NULL "
                    "AND oubo_dt <= ? AND (kettei_dt IS NULL OR kettei_dt > ?) "
                    "AND (outcome_dt IS NULL OR outcome_dt > ?)",
                    (row["user_id"], DAY, DAY, DAY),
                ).fetchone()[0]
                rivals = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE item_id = ? AND oubo_dt IS NOT NULL "
                    "AND oubo_dt <= ? AND (kettei_dt IS NULL OR kettei_dt > ?) "
                    "AND (outcome_dt IS NULL OR outcome_dt > ?)",
                    (row["item_id"], DAY, DAY, DAY),
                ).fetchone()[0]
                self.assertEqual(row["other_applications"], other)
                self.assertEqual(row["competing_candidates"], rivals)
            self.assertTrue(any(row["other_applications"] or row["competing_candidates"] for row in rows))

    def test_candidates_latent_adds_proxies_motivations_and_truth(self):
        with closing(self.connect()) as connection:
            rows = candidate_rows(connection, DAY, latent=True)
            self.assertGreater(len(rows), 0)
            self.assertEqual(set(rows[0]), LATENT_COLUMNS)
            for row in rows:
                user_skill, desired_salary = connection.execute(
                    "SELECT user_skill, desired_salary FROM user WHERE user_id = ?", (row["user_id"],)
                ).fetchone()
                required_skill, offered_salary = connection.execute(
                    "SELECT required_skill, offered_salary FROM item WHERE item_id = ?", (row["item_id"],)
                ).fetchone()
                self.assertAlmostEqual(row["skill_match"], 1 - abs(user_skill - required_skill), places=12)
                self.assertAlmostEqual(row["skill_shortfall"], max(required_skill - user_skill, 0), places=12)
                self.assertAlmostEqual(row["overqualification"], max(user_skill - required_skill, 0), places=12)
                self.assertAlmostEqual(row["salary_shortfall"], max((desired_salary - offered_salary) / desired_salary, 0),
                                       places=12)

            enriched = attach_latent_truth(rows, seed=self.seed)
            self.assertEqual(len(enriched), len(rows))
            self.assertNotIn("p_application_eventual", rows[0])  # the helper does not mutate its input
            for row in enriched:
                for stage in ("application", "screening", "decision"):
                    candidate = row[f"p_candidate_{stage}"]
                    employer = row[f"p_employer_{stage}"]
                    self.assertTrue(0 <= candidate <= 1)
                    self.assertTrue(0 <= employer <= 1)
                    self.assertAlmostEqual(candidate * employer, row[f"p_{stage}_eventual"], places=12)

    def test_parameterless_view_pattern(self):
        with closing(self.connect(memory=True)) as connection:
            connection.execute(BATCH_DAY_TABLE_SQL)
            connection.execute(DAILY_CANDIDATES_VIEW_SQL)
            connection.execute("INSERT INTO batch_day (day_dt) VALUES (?)", (DAY,))
            from_view = {(row[0], row[1]) for row in connection.execute(
                "SELECT user_id, item_id FROM daily_candidates")}
            with closing(self.connect()) as source:
                from_query = {(row["user_id"], row["item_id"]) for row in candidate_rows(source, DAY)}
            self.assertEqual(from_view, from_query)

    def test_docs_sql_matches_module_constants(self):
        page = (Path(__file__).resolve().parents[1] / "docs" / "DAILY_CANDIDATES.md").read_text(encoding="utf-8")
        self.assertIn(DAILY_CANDIDATES_SQL.strip(), page)
        self.assertIn(DAILY_CANDIDATES_LATENT_SQL.strip(), page)


    def test_snapshot_counts_equal_observed_reconstruction(self):
        """The #31 snapshot is exactly reconstructable now that outcomes are observed."""
        snapshot_days = {DAY_INDEX: None}
        rows, seekers, jobs = simulate(CONFIG, snapshot_days=snapshot_days)
        snapshot = snapshot_days[DAY_INDEX]
        # Map snapshot-local actor indices back to the canonical user/item ids.
        seeker_index = {tuple(str(v) for v in profile[:7]): index
                        for index, profile in enumerate(seekers)}
        job_index = {tuple(str(v) for v in profile[:7]): index
                     for index, profile in enumerate(jobs)}
        user_ids = {local: f"user-{seeker_index[tuple(str(v) for v in profile[:7])]:09d}"
                    for local, profile in enumerate(snapshot['seekers'])}
        item_ids = {local: f"item-{job_index[tuple(str(v) for v in profile[:7])]:04d}"
                    for local, profile in enumerate(snapshot['jobs'])}
        snapshot_rows = snapshot['rows']
        user_in_flight = {user_ids[local]: int(np.sum((snapshot_rows['seeker'] == local)
                                                      & np.isfinite(snapshot_rows['application'])))
                          for local in user_ids}
        job_in_flight = {item_ids[local]: int(np.sum((snapshot_rows['job'] == local)
                                                     & np.isfinite(snapshot_rows['application'])))
                         for local in item_ids}
        self.assertTrue(any(user_in_flight.values()))

        # The snapshot is taken at the end of day D, after all events before D+1
        # have been processed, so the matching SQL boundary is the D+1 boundary.
        day_end = (START + timedelta(days=DAY_INDEX + 1)).isoformat()
        with closing(self.connect()) as connection:
            reconstructed_users = {
                uid: connection.execute(
                    "SELECT COUNT(*) FROM events WHERE user_id = ? AND oubo_dt IS NOT NULL "
                    "AND oubo_dt <= ? AND (kettei_dt IS NULL OR kettei_dt > ?) "
                    "AND (outcome_dt IS NULL OR outcome_dt > ?)", (uid, day_end, day_end, day_end)
                ).fetchone()[0]
                for uid in user_ids.values()
            }
            reconstructed_items = {
                iid: connection.execute(
                    "SELECT COUNT(*) FROM events WHERE item_id = ? AND oubo_dt IS NOT NULL "
                    "AND oubo_dt <= ? AND (kettei_dt IS NULL OR kettei_dt > ?) "
                    "AND (outcome_dt IS NULL OR outcome_dt > ?)", (iid, day_end, day_end, day_end)
                ).fetchone()[0]
                for iid in item_ids.values()
            }
        self.assertEqual(user_in_flight, reconstructed_users)
        self.assertEqual(job_in_flight, reconstructed_items)

        # The same predicate used by the recipe must match exactly on a sample
        # of candidate rows (candidate_rows runs at the inclusive day boundary).
        with closing(self.connect()) as connection:
            candidates = candidate_rows(connection, day_end)
            self.assertTrue(candidates)
            for candidate in candidates:
                expected_user = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE user_id = ? AND oubo_dt IS NOT NULL "
                    "AND oubo_dt <= ? AND (kettei_dt IS NULL OR kettei_dt > ?) "
                    "AND (outcome_dt IS NULL OR outcome_dt > ?)",
                    (candidate['user_id'], day_end, day_end, day_end)
                ).fetchone()[0]
                self.assertEqual(candidate['other_applications'], expected_user)


if __name__ == "__main__":
    unittest.main()
