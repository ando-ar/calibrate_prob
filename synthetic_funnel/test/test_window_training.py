import unittest
from synthetic_funnel import Config,LegacyScores
from synthetic_funnel.trained_scores import fit_models
from synthetic_funnel.generator import parse_datetime


class WindowTrainingTest(unittest.TestCase):
    def test_independent_stage_windows_and_balancing(self):
        config=Config(rows=5,reference_rows=2000,legacy_scores=LegacyScores(training_window_days=(1,30,365),training_referrals_per_day=1000,negative_reservoir_size=5000))
        bundle=fit_models(config)
        self.assertEqual([m['window_origin'] for m in bundle['models']],['referral','application','screening'])
        self.assertEqual([m['window_days'] for m in bundle['models']],[1,30,365])
        for m in bundle['models']:
            self.assertEqual(m['selected_positives'],m['selected_negatives'])
            self.assertEqual(m['selected_positives'],m['population_positives'])
            self.assertLess(parse_datetime(m['risk_window_end']),parse_datetime(config.referral_start))
        self.assertGreater(bundle['models'][2]['source_referrals'],bundle['models'][1]['source_referrals'])


if __name__=='__main__':
    unittest.main()
