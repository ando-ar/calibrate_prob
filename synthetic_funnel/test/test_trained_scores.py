import unittest
from dataclasses import replace

import numpy as np

from synthetic_funnel import Config,LegacyScores,generate
from synthetic_funnel.trained_scores import fit_models,predict_scores,deadline
from synthetic_funnel.generator import parse_datetime


class TrainedScoreTest(unittest.TestCase):
    def test_past_only_balanced_fit_and_inference(self):
        config=Config(rows=20,reference_rows=2000,legacy_scores=LegacyScores(training_rows=150000))
        bundle=fit_models(config)
        self.assertLess(deadline(parse_datetime(bundle['training_config']['referral_end'])),parse_datetime(config.referral_start))
        self.assertNotEqual(bundle['training_config']['seed'],config.seed)
        for model in bundle['models']:
            self.assertEqual(model['selected_positives'],model['selected_negatives'])
            self.assertEqual(model['population_positives'],model['selected_positives'])
            self.assertNotIn('kettei_dt',model['features'])
            self.assertNotIn('p_application_eventual',model['features'])
        self.assertLess(bundle['models'][1]['population_rows'],bundle['models'][0]['population_rows'])
        scored=list(generate(config))
        plain=list(generate(replace(config,legacy_scores=replace(config.legacy_scores,enabled=False))))
        expected=predict_scores([r for r,_ in plain],bundle)
        for i,((r,t),(old,truth)) in enumerate(zip(scored,plain)):
            self.assertEqual(t,truth)
            self.assertEqual(r['legacy_apply_score'],expected[i,0])
            self.assertEqual(r['legacy_screen_score'],expected[i,1])
            self.assertEqual(r['legacy_decision_score'],expected[i,2])
            for key,value in old.items():
                self.assertEqual(r[key],value)
        self.assertTrue(np.all((expected>0)&(expected<1)))


if __name__=='__main__':
    unittest.main()
