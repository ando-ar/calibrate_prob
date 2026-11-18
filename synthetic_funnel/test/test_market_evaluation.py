import csv
import json
import random
import sqlite3
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from unittest.mock import patch

from synthetic_funnel import MarketConfig, write_market_dataset
from synthetic_funnel.market_candidates import attach_latent_truth, candidate_rows
from synthetic_funnel.market_evaluation import (
    CompetitionConfig,
    evaluate,
    evaluate_fallback,
    load_submission,
)

MARKET_START = "2024-01-01T00:00:00+09:00"
CONFIG = MarketConfig(days=150, warmup=40, seeker_arrivals=10, job_arrivals=10, seed=71)
EVAL_DAYS = tuple(range(45, 50))


def day_strings(start_ms, days):
    from datetime import timedelta
    from synthetic_funnel.shared import parse_datetime
    start = parse_datetime(start_ms)
    return {(start + timedelta(days=d)).isoformat() for d in days}


class EvaluatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        cls.dataset = cls.root / "dataset"
        cls.manifest = write_market_dataset(CONFIG, cls.dataset, market_start=MARKET_START)
        cls.eval_dts = day_strings(MARKET_START, EVAL_DAYS)
        connection = sqlite3.connect(cls.dataset / "dataset.sqlite.db")
        candidates = []
        latent = []
        for dt in sorted(cls.eval_dts):
            candidates.extend(candidate_rows(connection, dt))
            latent.extend(attach_latent_truth(candidate_rows(connection, dt, latent=True),
                                              seed=CONFIG.seed))
        connection.close()
        pq.write_table(pa.Table.from_pylist(candidates), cls.dataset / "candidates.parquet")
        latent_projection = [{key: row[key] for key in
                              ("day_dt", "user_id", "item_id", "p_decision_eventual", "fee_if_decided_yen")}
                             for row in latent]
        pq.write_table(pa.Table.from_pylist(latent_projection),
                       cls.dataset / "candidates_latent.parquet")
        cls.candidates = candidates

    def write_submission(self, name, *, mode):
        paths = []
        for day in sorted(self.eval_dts):
            rows = [row for row in self.candidates if row["day_dt"] == day]
            if mode == "fee":
                rows = sorted(rows, key=lambda row: (-row["fee_if_decided_yen"],
                                                     row["user_id"], row["item_id"]))
            else:
                rng = random.Random(1234 + int(day[8:10]))
                rows = list(rows)
                rng.shuffle(rows)
            path = self.root / f"{name}_{day[:10]}.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["day_dt", "user_id", "item_id", "rank"])
                writer.writeheader()
                for rank, row in enumerate(rows, 1):
                    writer.writerow({"day_dt": row["day_dt"], "user_id": row["user_id"],
                                     "item_id": row["item_id"], "rank": rank})
            paths.append(path)
        submission = self.root / f"{name}.csv"
        with submission.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["day_dt", "user_id", "item_id", "rank"])
            writer.writeheader()
            for path in paths:
                with path.open(encoding="utf-8", newline="") as source:
                    for row in csv.DictReader(source):
                        writer.writerow(row)
        return submission

    def test_submission_schema_validation(self):
        submission = self.write_submission("schema", mode="fee")
        rows = load_submission(submission)
        self.assertGreater(len(rows), 0)
        self.assertEqual(set(rows[0]), {"day_dt", "user_id", "item_id", "rank"})
        self.assertTrue(all(isinstance(row["rank"], int) for row in rows))

    def test_metrics_schema_and_determinism(self):
        submission = self.write_submission("fee", mode="fee")
        first = evaluate(self.dataset, submission, self.root / "out_fee")
        second = evaluate(self.dataset, submission, self.root / "out_fee_again")
        self.assertEqual(first, second)
        expected_metrics = {
            "macro_daily_replay_fee_yen", "total_replay_fee_yen", "replay_selected_positives",
            "replay_positive_capture", "macro_daily_logged_fee_yen",
            "macro_daily_counterfactual_fee_yen",
        }
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(set(first["metrics"]), expected_metrics)
        self.assertEqual(first["evaluation_scope"]["primary"],
                         "replay_policy_dependent_observed_fee")
        self.assertFalse(first["evaluation_scope"]["fallback"])
        self.assertEqual(len(first["evaluation"]["eval_days"]), len(EVAL_DAYS))
        self.assertIsInstance(first["metrics"]["macro_daily_replay_fee_yen"], float)
        self.assertGreater(first["metrics"]["macro_daily_counterfactual_fee_yen"], 0)
        for name in ("metrics.json", "daily.csv", "ranked.parquet", "summary.md"):
            self.assertTrue((self.root / "out_fee" / name).is_file())
        written = json.loads((self.root / "out_fee" / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(written, first)

    def test_fee_maximizing_policy_wins_replay_observed_fee(self):
        # Stub only the stochastic simulator: every selected pair is decided with
        # its deterministic fee_if_decided_yen.  This isolates budget allocation
        # and the primary metric -- ranking by fee must beat a random ranking.
        from synthetic_funnel.market import DTYPE
        from synthetic_funnel.shared import parse_datetime
        fee_by_key = {(row["day_dt"], row["user_id"], row["item_id"]): row["fee_if_decided_yen"]
                      for row in self.candidates}
        start = parse_datetime(MARKET_START)

        def decided_simulate(config, *, replay, **kwargs):
            rows = []
            for day, pairs in replay.policy.items():
                day_dt = (start + timedelta(days=day)).isoformat()
                for sid, jid in pairs:
                    row = np.zeros(1, dtype=DTYPE)[0].copy()
                    row["day"] = day
                    row["seeker"] = sid
                    row["job"] = jid
                    row["decision"] = day + 1
                    row["fee_yen"] = fee_by_key.get(
                        (day_dt, f"user-{sid:09d}", f"item-{jid:04d}"), 0.0)
                    rows.append(row)
            return (np.array(rows, dtype=DTYPE), np.empty((0, 8), dtype=object),
                    np.empty((0, 9), dtype=object))

        fee_submission = self.write_submission("fee2", mode="fee")
        random_submission = self.write_submission("random2", mode="random")
        with patch("synthetic_funnel.market_evaluation.simulate", side_effect=decided_simulate):
            fee = evaluate(self.dataset, fee_submission, self.root / "out_fee2")
            random_result = evaluate(self.dataset, random_submission, self.root / "out_random2")
        self.assertGreater(fee["metrics"]["macro_daily_replay_fee_yen"],
                           random_result["metrics"]["macro_daily_replay_fee_yen"])
        self.assertGreater(fee["metrics"]["macro_daily_counterfactual_fee_yen"],
                           random_result["metrics"]["macro_daily_counterfactual_fee_yen"])

    def test_rejects_pairs_not_in_candidates(self):
        submission = self.root / "invalid.csv"
        with submission.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["day_dt", "user_id", "item_id", "rank"])
            writer.writeheader()
            writer.writerow({"day_dt": sorted(self.eval_dts)[0], "user_id": "user-999999999",
                             "item_id": "item-9999", "rank": 1})
        with self.assertRaisesRegex(ValueError, "absent from candidates"):
            evaluate(self.dataset, submission, self.root / "out_invalid")

    def test_fallback_aggregates_seeds_and_records_scope(self):
        submission = self.write_submission("fallback", mode="fee")
        runs = [{"manifest": self.dataset, "submission": submission, "day": EVAL_DAYS[0]}
                for _ in range(3)]
        result = evaluate_fallback(runs, self.root / "fallback_out")
        self.assertEqual(result["evaluation_scope"]["primary"],
                         "replay_unavailable_single_day_seed_amplified")
        self.assertTrue(result["evaluation_scope"]["fallback"])
        self.assertEqual(len(result["metrics"]["macro_daily_replay_fee_yen_per_seed"]), 3)
        self.assertGreaterEqual(result["metrics"]["replay_fee_yen_std"], 0)
        self.assertTrue((self.root / "fallback_out" / "metrics.json").is_file())

    def test_defaults_follow_section_11(self):
        config = CompetitionConfig()
        self.assertEqual(config.horizon, "3_calendar_months")
        self.assertEqual(config.max_training_rows, 300000)
        self.assertEqual(config.fallback_seeds, 20)
        self.assertEqual(config.max_per_user_per_day, 5)
        self.assertEqual(config.daily_budget_per_active_user, 5)


if __name__ == "__main__":
    unittest.main()
