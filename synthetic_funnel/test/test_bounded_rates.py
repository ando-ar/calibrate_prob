import random
import unittest

import numpy as np

from synthetic_funnel import (
    Config,
    LegacyScores,
    Population,
    generate,
    population_rates,
)
from synthetic_funnel.batch import pair_batches
from synthetic_funnel.mechanism import (
    configured_gap_penalties,
    pair_features,
    probability_arrays,
)
from synthetic_funnel.population import poisson


class BoundedRateTest(unittest.TestCase):
    def test_batches_do_not_change_probabilities(self):
        config = Config(rows=100, reference_rows=2000)
        small = np.concatenate([gates for _, _, _, _, gates in pair_batches(config, 13)])
        large = np.concatenate([gates for _, _, _, _, gates in pair_batches(config, 100)])
        np.testing.assert_array_equal(small, large)

    def test_targets_ranges_and_gate_products(self):
        config = Config(rows=5000, legacy_scores=LegacyScores(enabled=False))
        np.testing.assert_allclose(population_rates(config).expected_rates, [.01,.1,.07], atol=1e-9)
        ps = []
        for row, truth in generate(config):
            p = [truth[f'p_{s}_eventual'] for s in ('application','screening','decision')]
            ps.append(p)
            self.assertIn('other_applications', row)
            self.assertIn('competing_candidates', row)
            for i, s in enumerate(('application','screening','decision')):
                self.assertAlmostEqual(p[i], truth[f'p_candidate_{s}']*truth[f'p_employer_{s}'])
        ps = np.asarray(ps)
        self.assertTrue(np.all(ps >= [.005,.05,.035]))
        self.assertTrue(np.all(ps <= [.015,.15,.105]))
        joint = np.cumprod(ps, axis=1).sum(axis=0)
        rate = joint / np.r_[len(ps),joint[:-1]]
        np.testing.assert_allclose(rate, config.base_probabilities, atol=.002)
        self.assertTrue(np.all(ps.std(axis=0) > [.0005,.005,.0035]))

    def test_relative_gap(self):
        config = Config()
        self.assertEqual(configured_gap_penalties(.01,.1,config), (0,0))
        self.assertEqual(configured_gap_penalties(.012,.12,config), (0,0))
        withdrawal, ng = configured_gap_penalties(.015,.05,config)
        self.assertEqual(withdrawal, 0)
        self.assertGreater(ng, 0)
        withdrawal, ng = configured_gap_penalties(.005,.15,config)
        self.assertGreater(withdrawal, 0)
        self.assertEqual(ng, 0)

    def test_competition_direction_and_bounds_under_extreme_affinity(self):
        config = Config()
        anchor = population_rates(config).intercepts
        f = pair_features(.7,.7,800,800,1,1)
        f.update(other_applications=0,competing_candidates=0)
        raw = probability_arrays(config,f,intercepts=anchor)
        other = probability_arrays(config,{**f,'other_applications':10},intercepts=anchor)
        rivals = probability_arrays(config,{**f,'competing_candidates':20},intercepts=anchor)
        self.assertLess(np.prod(other[0]),np.prod(raw[0]))
        self.assertLess(np.prod(rivals[1]),np.prod(raw[1]))
        for a in (-1000.,1000.):
            g = probability_arrays(config,f,a,-a,anchor)
            p = np.prod(g,axis=-1)
            self.assertTrue(np.all(p >= np.array([.005,.05,.035])-1e-14))
            self.assertTrue(np.all(p <= np.array([.015,.15,.105])+1e-14))

    def test_poisson_snapshot_counts(self):
        rng = random.Random(24)
        for mean in (2,5,30):
            values = np.array([poisson(rng,mean) for _ in range(10000)])
            self.assertAlmostEqual(values.mean(),mean,delta=6*np.sqrt(mean/len(values)))
        self.assertEqual(poisson(rng,0),0)
        with self.assertRaises(ValueError):
            Population(other_applications_mean=-1)
        with self.assertRaises(ValueError):
            Config(probability_half_widths=(.02,.05,.035))


if __name__ == '__main__':
    unittest.main()
