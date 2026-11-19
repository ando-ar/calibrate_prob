import unittest
from dataclasses import replace

import numpy as np

from synthetic_funnel import Mechanism, Population, population_rates
from synthetic_funnel.mechanism import (
    array_sigmoid,
    gap_penalties,
    pair_features,
    probability_arrays,
)
from synthetic_funnel.shared import ProbabilityConfig


class PopulationRateTest(unittest.TestCase):
    def test_reference_conditional_rates(self):
        config = ProbabilityConfig(reference_rows=6000, base_probabilities=(.25, .55, .15))
        anchor = population_rates(config)
        np.testing.assert_allclose(anchor.expected_rates, config.base_probabilities, atol=1e-9)
        self.assertEqual(anchor, population_rates(replace(config, rows=70000)))
        changed = replace(config, population=Population(remote_preference_rate=1, remote_availability_rate=0,
                                                        seeker_skill_alpha=2, seeker_skill_beta=3))
        other = population_rates(changed)
        np.testing.assert_allclose(other.expected_rates, config.base_probabilities, atol=1e-9)
        self.assertNotEqual(other.intercepts, anchor.intercepts)

    def test_anchors_track_custom_targets(self):
        config = ProbabilityConfig(
            seed=611, reference_rows=20000, base_probabilities=(.35, .4, .2),
            probability_half_widths=(.25, .30, .15),
        )
        anchor = population_rates(config)
        np.testing.assert_allclose(anchor.expected_rates, config.base_probabilities, atol=1e-9)
        self.assertEqual(anchor.reference_rows, 20000)
        self.assertEqual(anchor.reference_seed, 90210)
        self.assertTrue(all(np.isfinite(anchor.intercepts)))
        self.assertNotEqual(anchor.intercepts, population_rates(ProbabilityConfig()).intercepts)

    def test_gap_direction_and_tolerance(self):
        m = Mechanism()
        aligned = gap_penalties(.5, .5, m)
        high_apply = gap_penalties(.8, .2, m)
        high_screen = gap_penalties(.2, .8, m)
        self.assertEqual(aligned, (0, 0))
        self.assertEqual(high_apply[0], 0)
        self.assertGreater(high_apply[1], 0)
        self.assertGreater(high_screen[0], 0)
        self.assertEqual(high_screen[1], 0)
        self.assertEqual(gap_penalties(.51, .5, m), (0, 0))
        p = lambda penalties: float(array_sigmoid(.4-penalties[0])*array_sigmoid(.4-penalties[1]))
        self.assertGreater(p(aligned), p(high_apply))
        self.assertGreater(p(aligned), p(high_screen))
        self.assertLess(p(gap_penalties(.9, .1, m)), p(high_apply))

    def test_gap_actually_changes_decision_only_with_fixed_anchor(self):
        config = ProbabilityConfig(reference_rows=2000)
        f = pair_features(.9, .2, 1000, 400, 1, 0)
        intercepts = population_rates(config).intercepts
        g = probability_arrays(config, f, -.8, .8, intercepts)
        disabled = replace(config, mechanism=replace(config.mechanism, decision_ng_gap_strength=0,
                                                     decision_withdrawal_gap_strength=0))
        baseline = probability_arrays(disabled, f, -.8, .8, intercepts)
        np.testing.assert_allclose(g[:2], baseline[:2])
        self.assertLess(np.prod(g[2]), np.prod(baseline[2]))
        # Screening is more likely relative to its target, increasing withdrawals.
        self.assertLess(g[2, 0], baseline[2, 0])

    def test_parameter_validation(self):
        for settings in ({'job_count': 0}, {'remote_preference_rate': 1.1},
                         {'desired_salary_std': -1}, {'seeker_skill_alpha': 0}):
            with self.assertRaises(ValueError):
                ProbabilityConfig.from_dict({"population": settings})
        for settings in ({'decision_ng_gap_strength': -1}, {'decision_gap_tolerance': 2},
                         {'decision_gap_power': .5}):
            with self.assertRaises(ValueError):
                ProbabilityConfig.from_dict({"mechanism": settings})


if __name__ == '__main__':
    unittest.main()
