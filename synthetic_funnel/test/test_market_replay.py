import sqlite3
import unittest
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from synthetic_funnel import MarketConfig, write_market_dataset
from synthetic_funnel.market import ReplayIntroductions, replay_market, simulate
from synthetic_funnel.market_candidates import candidate_rows
from synthetic_funnel.shared import parse_datetime

MARKET_START = "2024-01-01T00:00:00+09:00"
START = parse_datetime(MARKET_START)


def row(day, user, item, rank, partition=None):
    value = {"day_dt": day, "user_id": user, "item_id": item, "rank": rank}
    if partition is not None:
        value["market"] = partition
    return value


class BudgetAllocationTest(unittest.TestCase):
    def test_dedup_keeps_best_rank_and_ties_sort_by_key(self):
        from synthetic_funnel.market_evaluation import allocate_budget
        day = "2024-01-01T00:00:00+09:00"
        rows = [
            row(day, "u1", "i1", 9), row(day, "u1", "i1", 2), row(day, "u1", "i1", 2),
            row(day, "u1", "i2", 3), row(day, "u2", "i1", 3),
        ]
        result = allocate_budget(rows, daily_budget=100)
        # u1/i1 deduped to rank 2; rank 3 tie broken by user_id then item_id.
        self.assertEqual([(r["user_id"], r["item_id"], r["rank"]) for r in result.selected],
                         [("u1", "i1", 2), ("u1", "i2", 3), ("u2", "i1", 3)])
        self.assertEqual(result.audit[day]["submitted"], 5)
        self.assertEqual(result.audit[day]["deduplicated"], 3)

    def test_per_user_cap_is_five_and_resets_daily(self):
        from synthetic_funnel.market_evaluation import allocate_budget
        day1, day2 = "2024-01-01T00:00:00+09:00", "2024-01-02T00:00:00+09:00"
        rows = [row(day1, "u1", f"i{i}", i) for i in range(1, 9)]
        rows += [row(day2, "u1", f"i{i}", i) for i in range(1, 9)]
        result = allocate_budget(rows, max_per_user_per_day=5, daily_budget=100)
        self.assertEqual(len([r for r in result.selected if r["day_dt"] == day1]), 5)
        self.assertEqual(len([r for r in result.selected if r["day_dt"] == day2]), 5)

    def test_total_budget_is_per_active_user_times_distinct_users(self):
        from synthetic_funnel.market_evaluation import allocate_budget
        day = "2024-01-01T00:00:00+09:00"
        rows = [row(day, "u1", f"i{i}", i) for i in range(1, 6)]
        rows += [row(day, "u2", f"j{i}", 10 + i) for i in range(1, 6)]
        rows += [row(day, "u3", "k1", 0)]
        result = allocate_budget(rows, daily_budget_per_active_user=3)
        # 3 distinct users -> total 9; u1/u2 have 5 each but only 3 survive each.
        self.assertEqual(len(result.selected), 9)
        self.assertEqual(result.audit[day]["total_budget"], 9)
        self.assertEqual(len([r for r in result.selected if r["user_id"] == "u3"]), 1)

    def test_absolute_budget_is_the_smaller_of_the_two(self):
        from synthetic_funnel.market_evaluation import allocate_budget
        day = "2024-01-01T00:00:00+09:00"
        rows = [row(day, f"u{i}", f"i{j}", i * 10 + j) for i in range(4) for j in range(3)]
        result = allocate_budget(rows, daily_budget_per_active_user=5, daily_budget=6)
        self.assertEqual(len(result.selected), 6)
        self.assertEqual(result.audit[day]["total_budget"], 6)

    def test_partition_budget_applies_on_top_of_user_cap(self):
        from synthetic_funnel.market_evaluation import allocate_budget
        day = "2024-01-01T00:00:00+09:00"
        rows = [row(day, f"u{i}", f"i{j}", i * 10 + j,
                    partition="a" if i % 2 == 0 else "b") for i in range(6) for j in range(3)]
        result = allocate_budget(rows, daily_budget_per_active_user=10,
                                 partition_field="market", partition_budgets={"a": 2, "b": 4})
        self.assertEqual(len([r for r in result.selected if r["market"] == "a"]), 2)
        self.assertEqual(len([r for r in result.selected if r["market"] == "b"]), 4)


class ReplayMarketTest(unittest.TestCase):
    CONFIG = MarketConfig(days=120, seeker_arrivals=4, job_arrivals=30, seed=11, referrals=1)
    EVAL_DAY = 20
    EVAL_DT = (START + timedelta(days=20)).isoformat()

    @classmethod
    def setUpClass(cls):
        cls.directory = TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        write_market_dataset(cls.CONFIG, cls.root / "dataset", market_start=MARKET_START)

    def connect(self):
        return sqlite3.connect(self.root / "dataset" / "dataset.sqlite.db")

    def candidate_pairs(self):
        """SQL recipe at the eval day after removing base introductions on/after it."""
        source = self.connect()
        target = sqlite3.connect(":memory:")
        source.backup(target)
        source.close()
        target.execute("DELETE FROM events WHERE shoukai_dt >= ?", (self.EVAL_DT,))
        rows = candidate_rows(target, self.EVAL_DT)
        target.close()
        return {(row["user_id"], row["item_id"]): row for row in rows}

    def test_replay_introductions_equal_daily_candidates_recipe(self):
        candidates = self.candidate_pairs()
        self.assertGreater(len(candidates), 0)
        policy = {self.EVAL_DAY: [(_idx("user", u), _idx("item", i)) for u, i in candidates]}
        replay = ReplayIntroductions(eval_start=self.EVAL_DAY, eval_end=self.EVAL_DAY + 1,
                                     policy=policy)
        simulate(self.CONFIG, replay=replay)
        introduced = {(f"user-{sid:09d}", f"item-{jid:04d}")
                      for sid, jid in replay.introduced[self.EVAL_DAY]}
        self.assertEqual(introduced, set(candidates))
        self.assertEqual(replay.skipped, [])

    def test_replay_is_deterministic_and_does_not_introduce_outside_evaluation(self):
        policy = {self.EVAL_DAY: [(0, 0), (1, 1)]}
        options = dict(eval_start=self.EVAL_DAY, eval_end=self.EVAL_DAY + 1, policy=policy)
        first, _, _ = replay_market(self.CONFIG, **options)
        second, _, _ = replay_market(self.CONFIG, **options)
        for key in first.dtype.names:
            np.testing.assert_array_equal(first[key], second[key])
        # No introductions outside the single evaluation day.
        before = first[first["day"] < self.EVAL_DAY]
        after = first[first["day"] > self.EVAL_DAY]
        self.assertGreater(len(before), 0)
        self.assertEqual(len(after), 0)  # maturation only: no new rows after eval_end

    def test_decided_replay_rows_carry_the_deterministic_fee(self):
        from unittest.mock import patch
        candidates = self.candidate_pairs()
        policy = {self.EVAL_DAY: [(_idx("user", u), _idx("item", i)) for u, i in candidates]}

        def high_rates(config, f, *args, **kwargs):
            return np.tile(np.array([[.9, 1.], [.95, .95], [.95, .95]]),
                           (len(f["skill_match"]), 1, 1))

        with patch("synthetic_funnel.market.probability_arrays", side_effect=high_rates):
            rows, _, jobs = replay_market(self.CONFIG, eval_start=self.EVAL_DAY,
                                          eval_end=self.EVAL_DAY + 1, policy=policy)
        selected = rows[rows["day"] == self.EVAL_DAY]
        self.assertGreater(len(selected), 0)
        decided = np.isfinite(selected["decision"])
        self.assertGreater(int(decided.sum()), 0)
        expected = np.asarray(jobs[selected["job"][decided], 2], dtype=float) * 0.33
        np.testing.assert_allclose(np.asarray(selected["fee_yen"][decided], dtype=float), expected,
                                   rtol=0, atol=1e-12)
        self.assertTrue(np.all(np.isnan(selected["fee_yen"][~decided])))

    def test_frozen_state_at_d_train_matches_the_base_market(self):
        # Compare the actual snapshot at D_train (day before evaluation), not the
        # final rows: outcomes of pre-existing cases may legitimately differ once
        # the policy changes the introductions on later days.
        candidates = self.candidate_pairs()
        policy = {self.EVAL_DAY: [(_idx("user", u), _idx("item", i)) for u, i in candidates]}
        base_snapshot, replay_snapshot = {self.EVAL_DAY - 1: None}, {self.EVAL_DAY - 1: None}
        simulate(self.CONFIG, snapshot_days=base_snapshot)
        replay_market(self.CONFIG, eval_start=self.EVAL_DAY, eval_end=self.EVAL_DAY + 1,
                      policy=policy, snapshot_days=replay_snapshot)
        base = base_snapshot[self.EVAL_DAY - 1]
        replayed = replay_snapshot[self.EVAL_DAY - 1]
        self.assertEqual(base["keys"], replayed["keys"])
        for key in base["rows"].dtype.names:
            np.testing.assert_array_equal(base["rows"][key], replayed["rows"][key])
        np.testing.assert_array_equal(base["anchors"], replayed["anchors"])
        self.assertEqual(base["live_s"], replayed["live_s"])
        self.assertEqual(base["live_j"], replayed["live_j"])


def _idx(prefix, value):
    return int(value.split("-", 1)[1])


if __name__ == "__main__":
    unittest.main()
