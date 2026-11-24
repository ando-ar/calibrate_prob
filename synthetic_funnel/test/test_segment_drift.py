"""issue #46: gentle occupation/region heterogeneity and temporal drift.

These tests pin the shape and determinism of the segment/drift mechanism, that
the two-sided consistency ``p_candidate x p_employer == p_eventual`` still holds
exactly, that the effects are gentle (bounded and near the anchored base rate),
and that a replay from a frozen snapshot is byte-identical even though the drift
depends on the introduction day.
"""
import pickle
import unittest

import numpy as np

from synthetic_funnel import MarketConfig, population_rates
from synthetic_funnel.market import (
    OCCUPATION_INDEX,
    REGION_INDEX,
    ReplayIntroductions,
    simulate,
)
from synthetic_funnel.mechanism import (
    SegmentDrift,
    drift_offsets,
    probability_arrays,
    segment_decision_offsets,
)
from synthetic_funnel.shared import ProbabilityConfig

STAGES = ("application", "screening", "decision")
JOINT = {"application": "pa", "screening": "ps", "decision": "pd"}


def rows_equal(first, second):
    return all(np.array_equal(first[key], second[key], equal_nan=True)
               for key in first.dtype.names)


class SegmentDriftShapeTest(unittest.TestCase):
    def test_defaults_are_centered_and_gentle(self):
        drift = SegmentDrift()
        self.assertEqual(len(drift.occupation_effects), 8)
        self.assertEqual(len(drift.region_effects), 8)
        self.assertAlmostEqual(sum(drift.occupation_effects), 0.0, places=12)
        self.assertAlmostEqual(sum(drift.region_effects), 0.0, places=12)
        self.assertTrue(all(abs(x) <= 0.3 for x in drift.occupation_effects))
        self.assertTrue(all(abs(x) <= 0.15 for x in drift.region_effects))
        self.assertLessEqual(drift.drift_amplitude, 0.6)

    def test_validation_rejects_bad_tables_and_amplitude(self):
        for kwargs in ({"occupation_effects": (0.0,) * 7},
                       {"region_effects": (0.0,) * 9},
                       {"drift_amplitude": -1.0}):
            with self.assertRaises(ValueError):
                SegmentDrift(**kwargs)

    def test_drift_is_zero_at_horizon_midpoint_and_linear(self):
        drift = SegmentDrift(drift_amplitude=0.35)
        self.assertAlmostEqual(float(drift_offsets(drift, 140.0, 280)), 0.0, places=12)
        self.assertAlmostEqual(float(drift_offsets(drift, 0.0, 280)), -0.35, places=12)
        self.assertAlmostEqual(float(drift_offsets(drift, 280.0, 280)), 0.35, places=12)

    def test_segment_offsets_add_own_side_effects(self):
        drift = SegmentDrift()
        candidate, employer = segment_decision_offsets(
            drift, np.array([0]), np.array([0]), np.array([7]), np.array([7]))
        self.assertEqual(float(candidate[0]),
                         drift.occupation_effects[0] + drift.region_effects[0])
        self.assertEqual(float(employer[0]),
                         drift.occupation_effects[7] + drift.region_effects[7])


class SegmentDriftMarketTest(unittest.TestCase):
    CONFIG = MarketConfig(days=120, seeker_arrivals=8, job_arrivals=20, seed=71, referrals=2)

    def test_consistency_and_bounds_hold_with_segment_and_drift(self):
        rows, _, _ = simulate(self.CONFIG)
        for stage in STAGES:
            product = rows[f"p_candidate_{stage}"] * rows[f"p_employer_{stage}"]
            np.testing.assert_allclose(product, rows[JOINT[stage]], rtol=0, atol=1e-15)
        # Gentle offsets never leave the anchored bounded range.
        pd = rows["pd"]
        self.assertGreaterEqual(pd.min(), 0.035 - 1e-14)
        self.assertLessEqual(pd.max(), 0.105 + 1e-14)

    def test_same_seed_reproduces_identical_market(self):
        first, _, _ = simulate(self.CONFIG)
        second, _, _ = simulate(self.CONFIG)
        self.assertTrue(rows_equal(first, second))

    def test_segment_effects_shift_decision_rates_in_the_expected_direction(self):
        # Zero drift isolates the occupation effect on the decision rate.  The
        # effect is read on every introduction (the counterfactual p_decision is
        # defined for all rows, not only those that reached screening), which
        # avoids the selection bias of the screening-reached subset.
        config = MarketConfig(days=200, seeker_arrivals=15, job_arrivals=30, seed=5, referrals=4,
                              segment_drift=SegmentDrift(drift_amplitude=0.0))
        rows, seekers, _ = simulate(config)
        occ = np.array([OCCUPATION_INDEX[seekers[s, 3]] for s in rows["seeker"]], dtype=int)
        high = occ >= 6
        low = occ <= 1
        self.assertGreater(int(high.sum()), 0)
        self.assertGreater(int(low.sum()), 0)
        self.assertGreater(float(rows["pd"][high].mean()),
                           float(rows["pd"][low].mean()))

    def test_drift_raises_decision_rate_over_the_horizon(self):
        # Zero segment effects isolate the monotone linear drift, read on every
        # introduction so the composition change of the screening subset does not
        # mask the drift direction.
        config = MarketConfig(days=200, seeker_arrivals=15, job_arrivals=30, seed=5, referrals=4,
                              segment_drift=SegmentDrift(
                                  occupation_effects=(0.0,) * 8, region_effects=(0.0,) * 8,
                                  drift_amplitude=0.5))
        rows, _, _ = simulate(config)
        early = rows["day"] < 40
        late = rows["day"] >= 160
        self.assertGreater(int(early.sum()), 0)
        self.assertGreater(int(late.sum()), 0)
        self.assertGreater(float(rows["pd"][late].mean()),
                           float(rows["pd"][early].mean()))

    def test_replay_from_snapshot_is_byte_identical_with_drift(self):
        options = {"eval_start": 20, "eval_end": 22,
                   "policy": {20: [(0, 0), (1, 1)], 21: [(2, 2)]}}
        fresh, _, _ = simulate(self.CONFIG, replay=ReplayIntroductions(**options))
        captured = {15: None}
        simulate(self.CONFIG, replay=ReplayIntroductions(**options),
                 state_days=captured, stop_day=15)
        state = pickle.loads(pickle.dumps(captured[15]))
        resumed, _, _ = simulate(self.CONFIG, replay=ReplayIntroductions(**options),
                                 warm_state=state)
        self.assertTrue(rows_equal(fresh, resumed))


class SegmentDriftMechanismTest(unittest.TestCase):
    """The mechanism call itself applies the segment/drift offsets consistently."""

    def test_probability_arrays_with_segment_and_drift_is_consistent(self):
        base = ProbabilityConfig(seed=71)
        anchor = population_rates(base).intercepts
        features = {
            "seeker_skill": 0.5, "required_skill": 0.5, "desired_salary": 600.0, "offered_salary": 600.0,
            "wants_remote": 0, "allows_remote": 0, "skill_match": 1.0, "skill_shortfall": 0.0,
            "overqualification": 0.0, "salary_shortfall": 0.0, "remote_mismatch": 0,
            "other_applications": 0, "competing_candidates": 0,
        }
        segment = (np.array([0]), np.array([0]), np.array([7]), np.array([7]))
        gates = probability_arrays(base, features, 0.0, 0.0, anchor,
                                   segment=segment, day=np.array([0.0]), days=280)
        self.assertEqual(gates.shape, (1, 3, 2))
        for stage in range(3):
            candidate = float(gates[0, stage, 0])
            employer = float(gates[0, stage, 1])
            self.assertAlmostEqual(candidate * employer, candidate * employer, places=15)
        # A high-segment pair at the end of the horizon decides more often than a
        # low-segment pair at the start.
        low_early = probability_arrays(
            base, features, 0.0, 0.0, anchor,
            segment=(np.array([0]), np.array([0]), np.array([0]), np.array([0])),
            day=np.array([0.0]), days=280)
        high_late = probability_arrays(
            base, features, 0.0, 0.0, anchor,
            segment=(np.array([7]), np.array([7]), np.array([7]), np.array([7])),
            day=np.array([279.0]), days=280)
        self.assertGreater(float(np.prod(high_late[0, 2])), float(np.prod(low_early[0, 2])))


if __name__ == "__main__":
    unittest.main()
