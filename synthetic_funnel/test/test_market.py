import unittest
from typing import Any, cast
from unittest.mock import patch

import numpy as np

from synthetic_funnel.market import LATENT_DATE_FIELDS, MarketConfig, simulate


def tiled_gates(gates):
    def evaluate(config,f,*args):
        return np.tile(np.asarray(gates),(len(f['skill_match']),1,1))
    return evaluate


class MarketLatentTest(unittest.TestCase):
    def test_counterfactual_latents_are_complete_and_consistent(self):
        gates=np.array([[.9,1.],[.95,.95],[.95,.95]])
        with patch('synthetic_funnel.market.probability_arrays',side_effect=tiled_gates(gates)):
            r,s,j=simulate(MarketConfig(days=120,seeker_arrivals=5,job_arrivals=10))
        for stage,p in zip(('application','screening','decision'),('pa','ps','pd')):
            np.testing.assert_array_equal(r[f'p_candidate_{stage}']*r[f'p_employer_{stage}'],r[p])
        np.testing.assert_array_equal(r['p_employer_application'],1.)
        latent=[r[k] for k in LATENT_DATE_FIELDS]
        # A latent date exists exactly for the stages the counterfactual funnel passed.
        for i in range(3):
            expected=(r['failure_stage']==0)|(r['failure_stage']>i+1)
            np.testing.assert_array_equal(np.isfinite(latent[i]),expected)
            reached=np.isfinite(latent[i])
            self.assertTrue(np.all(latent[i][reached]>r['day'][reached]))
        for earlier,later in ((0,1),(1,2)):
            both=np.isfinite(latent[earlier])&np.isfinite(latent[later])
            self.assertTrue(np.all(latent[later][both]>=latent[earlier][both]))
        self.assertTrue(np.all(r['failure_reason'][r['failure_stage']==0]==0))
        self.assertTrue(np.all(r['failure_reason'][r['failure_stage']==1]==1))
        for code in (2,3):
            self.assertTrue(np.all(np.isin(r['failure_reason'][r['failure_stage']==code],(2,3))))

    def test_zero_gates_fail_every_counterfactual_at_application(self):
        gates=np.array([[0.,1.],[0.,0.],[0.,0.]])
        with patch('synthetic_funnel.market.probability_arrays',side_effect=tiled_gates(gates)):
            r,_,_=simulate(MarketConfig(days=3,seeker_arrivals=5,job_arrivals=10))
        self.assertTrue(np.all(r['failure_stage']==1))
        self.assertTrue(np.all(r['failure_reason']==1))
        for key in LATENT_DATE_FIELDS:
            self.assertTrue(np.all(np.isnan(r[key])))

    def test_counterfactual_draws_reproduce_independently_of_the_market(self):
        config=MarketConfig(days=30,seeker_arrivals=5,job_arrivals=10)
        first,_,_=simulate(config)
        second,_,_=simulate(config)
        for key in (*LATENT_DATE_FIELDS,'failure_stage','failure_reason',
                    'p_candidate_application','p_employer_decision'):
            np.testing.assert_array_equal(first[key],second[key])
        other=MarketConfig(days=30,seeker_arrivals=5,job_arrivals=10,seed=72)
        changed,_,_=simulate(other)
        self.assertFalse(np.array_equal(first['latent_oubo_dt'],changed['latent_oubo_dt'],equal_nan=True))


class MarketTest(unittest.TestCase):
    def test_application_retention_preserves_market_and_replay(self):
        config=MarketConfig(days=100,seeker_arrivals=5,job_arrivals=10)
        snaps_all = {80: None}
        snaps_compact = {80: None}
        all_rows,s,j=simulate(config,snapshot_days=snaps_all)
        retained,ss,jj=simulate(config,snapshot_days=snaps_compact,retain='applications')
        expected=all_rows[np.isfinite(all_rows['application'])]
        for key in expected.dtype.names:
            np.testing.assert_array_equal(expected[key],retained[key])
        np.testing.assert_array_equal(s,ss);np.testing.assert_array_equal(j,jj)
        complete_snapshot = cast(dict[str, Any], snaps_all[80])
        compact_snapshot = cast(dict[str, Any], snaps_compact[80])
        for key in complete_snapshot['rows'].dtype.names:
            np.testing.assert_array_equal(complete_snapshot['rows'][key], compact_snapshot['rows'][key])
        self.assertEqual(complete_snapshot['keys'], compact_snapshot['keys'])

    def test_rules_exits_and_referral_caps(self):
        def high_rates(config,f,*args):
            return np.tile(np.array([[.9,1.],[.95,.95],[.95,.95]]),(len(f['skill_match']),1,1))

        with patch('synthetic_funnel.market.probability_arrays',side_effect=high_rates):
            r,s,j=simulate(MarketConfig(days=130,seeker_arrivals=5,job_arrivals=10))
        self.assertTrue(np.all(s[r['seeker'],3]==j[r['job'],3]))
        self.assertTrue(np.all(s[r['seeker'],2]>np.maximum(100,j[r['job'],2]-100)))
        self.assertTrue(np.all((s[r['seeker'],4]==j[r['job'],4])|(j[r['job'],5]==1)))
        pairs=np.column_stack([r['seeker'],r['job']])
        self.assertEqual(len(np.unique(pairs,axis=0)),len(r))
        _,counts=np.unique(np.column_stack([r['seeker'],r['day']]),axis=0,return_counts=True)
        self.assertLessEqual(int(np.max(counts)),5)
        for k in ('application','screening','decision'):
            valid=np.isfinite(r[k])
            self.assertTrue(np.all(r[k][valid]<j[r['job'][valid],0]+40))
        dec=np.isfinite(r['decision'])
        self.assertGreater(dec.sum(),0)
        self.assertEqual(len(np.unique(r['seeker'][dec])),dec.sum())
        self.assertEqual(len(np.unique(r['job'][dec])),dec.sum())
        for actor in ('seeker','job'):
            for who,t in zip(r[actor][dec],r['decision'][dec]):
                mask=r[actor]==who
                self.assertTrue(np.all(r['day'][mask]+.01<=t))
                for k in ('application','screening','decision'):
                    values=r[k][mask];values=values[np.isfinite(values)]
                    self.assertTrue(np.all(values<=t))


if __name__=='__main__':unittest.main()
