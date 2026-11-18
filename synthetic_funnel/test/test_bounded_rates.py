import random
import unittest

import numpy as np

from synthetic_funnel import (
    MarketConfig,
    Population,
    population_rates,
)
from synthetic_funnel.market import simulate
from synthetic_funnel.mechanism import (
    configured_gap_penalties,
    pair_features,
    probability_arrays,
)
from synthetic_funnel.population import poisson
from synthetic_funnel.shared import ProbabilityConfig

STAGES = ("application", "screening", "decision")
LIMITS = np.array([[0.005, 0.05, 0.035], [0.015, 0.15, 0.105]])


class BoundedRateTest(unittest.TestCase):
    def test_bounds_and_gate_products_hold_for_every_market_referral(self):
        config = ProbabilityConfig(base_probabilities=(0.01, 0.10, 0.07))
        np.testing.assert_allclose(population_rates(config).expected_rates, [0.01, 0.1, 0.07], atol=1e-9)
        rows, _, _ = simulate(MarketConfig(days=120, seeker_arrivals=5, job_arrivals=8, seed=29))
        pa = np.column_stack([rows[f"p_candidate_{stage}"] * rows[f"p_employer_{stage}"] for stage in STAGES])
        np.testing.assert_allclose(pa, np.column_stack([rows[k] for k in ("pa", "ps", "pd")]), atol=1e-12)
        self.assertTrue(np.all(pa >= LIMITS[0] - 1e-14))
        self.assertTrue(np.all(pa <= LIMITS[1] + 1e-14))
        self.assertTrue(np.all(pa.std(axis=0) > [0.0005, 0.005, 0.0035]))

    def test_relative_gap(self):
        config = ProbabilityConfig()
        self.assertEqual(configured_gap_penalties(.01, .1, config), (0, 0))
        self.assertEqual(configured_gap_penalties(.012, .12, config), (0, 0))
        withdrawal, ng = configured_gap_penalties(.015, .05, config)
        self.assertEqual(withdrawal, 0)
        self.assertGreater(ng, 0)
        withdrawal, ng = configured_gap_penalties(.005, .15, config)
        self.assertGreater(withdrawal, 0)
        self.assertEqual(ng, 0)

    def test_competition_direction_and_bounds_under_extreme_affinity(self):
        config = ProbabilityConfig()
        anchor = population_rates(config).intercepts
        f = pair_features(.7, .7, 800, 800, 1, 1)
        f.update(other_applications=0, competing_candidates=0)
        raw = probability_arrays(config, f, intercepts=anchor)
        other = probability_arrays(config, {**f, "other_applications": 10}, intercepts=anchor)
        rivals = probability_arrays(config, {**f, "competing_candidates": 20}, intercepts=anchor)
        self.assertLess(np.prod(other[0]), np.prod(raw[0]))
        self.assertLess(np.prod(rivals[1]), np.prod(raw[1]))
        for a in (-1000., 1000.):
            g = probability_arrays(config, f, a, -a, anchor)
            p = np.prod(g, axis=-1)
            self.assertTrue(np.all(p >= LIMITS[0] - 1e-14))
            self.assertTrue(np.all(p <= LIMITS[1] + 1e-14))

    def test_poisson_snapshot_counts(self):
        rng = random.Random(24)
        for mean in (2, 5, 30):
            values = np.array([poisson(rng, mean) for _ in range(10000)])
            self.assertAlmostEqual(values.mean(), mean, delta=6 * np.sqrt(mean / len(values)))
        self.assertEqual(poisson(rng, 0), 0)
        with self.assertRaises(ValueError):
            Population(other_applications_mean=-1)
        with self.assertRaises(ValueError):
            ProbabilityConfig(probability_half_widths=(.02, .05, .035))


if __name__ == "__main__":
    unittest.main()
