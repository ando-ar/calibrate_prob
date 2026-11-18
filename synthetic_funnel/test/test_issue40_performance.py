"""Issue #40: simulation hot-path refactor and D_train snapshot reuse.

The refactor intentionally changes the market RNG stream, so these tests assert
structural invariants and byte-identical *replay* behaviour rather than the old
exact byte outputs (which are updated in test_market.py).
"""
import pickle
import sqlite3
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from synthetic_funnel import MarketConfig, write_market_dataset
from synthetic_funnel.market import ReplayIntroductions, simulate
from synthetic_funnel.market_evaluation import SNAPSHOT_FILENAME, evaluate, freeze_market
from synthetic_funnel.shared import parse_datetime

MARKET_START = "2024-01-01T00:00:00+09:00"


def rows_equal(first, second):
    return all(np.array_equal(first[key], second[key], equal_nan=True)
               for key in first.dtype.names)


class MarketStructuralInvariantTest(unittest.TestCase):
    """Funnel sums, FK integrity and daily consistency survive the refactor."""

    def test_funnel_and_foreign_key_invariants(self):
        config = MarketConfig(days=60, seeker_arrivals=8, job_arrivals=12,
                              seed=71, referrals=2)
        daily = []
        rows, seekers, jobs = simulate(config, daily_stats=daily)
        self.assertEqual(len(daily), config.days)
        self.assertTrue(all(len(entry) == 9 for entry in daily))
        self.assertEqual(sum(entry[3] for entry in daily), len(rows))
        self.assertEqual(sum(entry[4] for entry in daily), config.days * config.seeker_arrivals)
        self.assertEqual(sum(entry[5] for entry in daily), config.days * config.job_arrivals)
        self.assertEqual(sum(entry[6] for entry in daily), int(np.isfinite(rows['application']).sum()))
        self.assertEqual(sum(entry[7] for entry in daily), int(np.isfinite(rows['screening']).sum()))
        self.assertEqual(sum(entry[8] for entry in daily), int(np.isfinite(rows['decision']).sum()))
        # Every row points at an actor that actually arrived in this market.
        self.assertGreaterEqual(int(rows['seeker'].min()), 0)
        self.assertLess(int(rows['seeker'].max()), len(seekers))
        self.assertGreaterEqual(int(rows['job'].min()), 0)
        self.assertLess(int(rows['job'].max()), len(jobs))
        # Stage order: decisions imply screenings imply applications.
        applied = np.isfinite(rows['application'])
        screened = np.isfinite(rows['screening'])
        decided = np.isfinite(rows['decision'])
        self.assertTrue(np.all(screened <= applied))
        self.assertTrue(np.all(decided <= screened))
        self.assertTrue(np.all(rows['application'][applied] >= rows['day'][applied]))
        self.assertTrue(np.all(rows['screening'][screened] >= rows['application'][screened]))
        self.assertTrue(np.all(rows['decision'][decided] >= rows['screening'][decided]))
        # The daily referral cap is still enforced per seeker.
        self.assertLessEqual(np.max(np.bincount(rows['seeker'])), config.days * config.referrals)


class ReplaySnapshotTest(unittest.TestCase):
    # Small enough to keep the suite fast while exercising the full state.
    CONFIG = MarketConfig(days=60, warmup=10, seeker_arrivals=6, job_arrivals=20,
                          seed=11, referrals=2)
    FREEZE_DAY = 15
    EVAL_DAY = 20

    def population(self):
        base_rows, seekers, jobs = simulate(self.CONFIG)
        # Dataset profiles carry NaN departures at arrival, not the final
        # departures of the completed base run.
        population = {"seekers": [list(p) for p in seekers], "jobs": [list(p) for p in jobs]}
        for profile in population["seekers"]:
            profile[7] = np.nan
        for profile in population["jobs"]:
            profile[7] = np.nan
        return {"seekers": population["seekers"], "jobs": population["jobs"],
                "departure": {"seekers": {}, "jobs": {}}}, base_rows

    def replay_kwargs(self, population):
        pairs = [(sid, sid % 20) for sid in range(0, 24)]
        options = {"eval_start": self.EVAL_DAY, "eval_end": self.EVAL_DAY + 2, "policy": {
            self.EVAL_DAY: pairs, self.EVAL_DAY + 1: pairs[:4],
        }}
        return options, population

    def test_snapshot_roundtrip_is_byte_identical(self):
        population, _ = self.population()
        options, population = self.replay_kwargs(population)
        fresh_replay = ReplayIntroductions(**options)
        fresh_replay.departure = population["departure"]
        fresh, _, _ = simulate(self.CONFIG, replay=fresh_replay, population=population)
        # Capture at D_train, then persist and reload through pickle exactly as
        # the organiser's freeze step does.
        captured = {self.FREEZE_DAY: None}
        simulate(self.CONFIG, replay=ReplayIntroductions(**options), population=population,
                 state_days=captured, stop_day=self.FREEZE_DAY)
        state = pickle.loads(pickle.dumps(captured[self.FREEZE_DAY]))
        replay_from_state = ReplayIntroductions(**options)
        replay_from_state.departure = population["departure"]
        resumed, _, _ = simulate(self.CONFIG, replay=replay_from_state,
                                 population=population, warm_state=state)
        self.assertTrue(rows_equal(fresh, resumed))
        self.assertEqual(replay_from_state.introduced, fresh_replay.introduced)
        self.assertEqual(replay_from_state.skipped, fresh_replay.skipped)


class EvaluationSnapshotTest(unittest.TestCase):
    """The evaluator must reuse a frozen D_train state without changing metrics."""

    CONFIG = MarketConfig(days=60, warmup=10, seeker_arrivals=4, job_arrivals=30,
                          seed=11, referrals=1)
    EVAL_DAYS = (20, 21)

    @classmethod
    def setUpClass(cls):
        directory = TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.root = Path(directory.name)
        cls.dataset = cls.root / "dataset"
        write_market_dataset(cls.CONFIG, cls.dataset, market_start=MARKET_START)
        start = parse_datetime(MARKET_START)
        connection = sqlite3.connect(cls.dataset / "dataset.sqlite.db")
        candidates = []
        for day in cls.EVAL_DAYS:
            candidates.extend(_candidate_rows(connection, (start + timedelta(days=day)).isoformat()))
        connection.close()
        pq.write_table(pa.Table.from_pylist(candidates), cls.dataset / "candidates.parquet")
        cls.submission = cls.root / "submission.csv"
        import csv
        with cls.submission.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["day_dt", "user_id", "item_id", "rank"])
            writer.writeheader()
            for rank, row in enumerate(sorted(candidates, key=lambda r: (r["day_dt"], r["user_id"])), 1):
                writer.writerow({"day_dt": row["day_dt"], "user_id": row["user_id"],
                                 "item_id": row["item_id"], "rank": rank})

    def test_replay_from_frozen_state_matches_fresh_simulation(self):
        snapshot = self.root / "runs" / SNAPSHOT_FILENAME
        state = freeze_market(self.dataset, snapshot)
        self.assertEqual(state["day"], self.EVAL_DAYS[0] - 1)
        self.assertGreater(snapshot.stat().st_size, 0)
        horizon = {"horizon": 30}  # D_eval_end + H must fit inside this short market
        fresh = evaluate(self.dataset, self.submission, self.root / "fresh", config=horizon)
        reused = evaluate(self.dataset, self.submission, self.root / "reused",
                          config=horizon, snapshot_path=snapshot)
        self.assertEqual(fresh, reused)
        for name in ("metrics.json", "daily.csv"):
            self.assertEqual((self.root / "fresh" / name).read_bytes(),
                             (self.root / "reused" / name).read_bytes())


def _candidate_rows(connection, day):
    from synthetic_funnel.market_candidates import candidate_rows
    return candidate_rows(connection, day)


if __name__ == "__main__":
    unittest.main()
