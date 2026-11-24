import hashlib
import inspect
import unittest
from typing import Any, cast
from unittest.mock import patch

import numpy as np

from synthetic_funnel.market import (
    FEE_RATE_DEFAULT,
    LATENT_DATE_FIELDS,
    OCCUPATIONS,
    OUTCOME_STATUSES,
    OUTCOME_STATUS_CODES,
    REGIONS,
    MarketConfig,
    observed_failure_status,
    simulate,
)


def tiled_gates(gates):
    def evaluate(config,f,*args,**kwargs):
        return np.tile(np.asarray(gates),(len(f['skill_match']),1,1))
    return evaluate


def assert_profiles_equal(test_case,first,second):
    """Object-dtype profiles hold NaN departures; compare NaNs as equal."""
    test_case.assertEqual(first.shape,second.shape)
    np.testing.assert_array_equal(first.astype(str),second.astype(str))


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
        assert_profiles_equal(self,s,ss);assert_profiles_equal(self,j,jj)
        complete_snapshot = cast(dict[str, Any], snaps_all[80])
        compact_snapshot = cast(dict[str, Any], snaps_compact[80])
        for key in complete_snapshot['rows'].dtype.names:
            np.testing.assert_array_equal(complete_snapshot['rows'][key], compact_snapshot['rows'][key])
        self.assertEqual(complete_snapshot['keys'], compact_snapshot['keys'])

    def test_daily_stats_funnel_totals(self):
        config=MarketConfig(days=120,seeker_arrivals=5,job_arrivals=10)
        daily=[]
        r,_,_=simulate(config,daily_stats=daily)
        self.assertEqual(len(daily),config.days)
        self.assertTrue(all(len(entry)==9 for entry in daily))
        self.assertEqual(sum(entry[3] for entry in daily),len(r))
        self.assertEqual(sum(entry[4] for entry in daily),config.days*config.seeker_arrivals)
        self.assertEqual(sum(entry[5] for entry in daily),config.days*config.job_arrivals)
        self.assertEqual(sum(entry[6] for entry in daily),int(np.isfinite(r['application']).sum()))
        self.assertEqual(sum(entry[7] for entry in daily),int(np.isfinite(r['screening']).sum()))
        self.assertEqual(sum(entry[8] for entry in daily),int(np.isfinite(r['decision']).sum()))

    def test_rules_exits_and_referral_caps(self):
        def high_rates(config,f,*args,**kwargs):
            return np.tile(np.array([[.9,1.],[.95,.95],[.95,.95]]),(len(f['skill_match']),1,1))

        with patch('synthetic_funnel.market.probability_arrays',side_effect=high_rates):
            r,s,j=simulate(MarketConfig(days=130,seeker_arrivals=5,job_arrivals=10))
        self.assertEqual(len(OCCUPATIONS),8)
        self.assertEqual(len(REGIONS),8)
        self.assertTrue(set(s[:,3])<=set(OCCUPATIONS))
        self.assertTrue(set(j[:,3])<=set(OCCUPATIONS))
        self.assertTrue(set(s[:,4])<=set(REGIONS))
        self.assertTrue(set(j[:,4])<=set(REGIONS))
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

    def test_profile_arrays_track_departures(self):
        days=130
        r,s,j=simulate(MarketConfig(days=days,seeker_arrivals=5,job_arrivals=10))
        self.assertEqual(s.shape[1],8);self.assertEqual(j.shape[1],9)
        self.assertTrue(np.all(j[:,8]==FEE_RATE_DEFAULT))
        decided=np.isfinite(r['decision'])
        # A decision removes both actors at the decision time.
        for seeker,job,t in zip(r['seeker'][decided],r['job'][decided],r['decision'][decided]):
            self.assertEqual(s[int(seeker),7],t)
            self.assertEqual(j[int(job),7],t)
        seeker_decisions={int(sid):t for sid,t in zip(r['seeker'][decided],r['decision'][decided])}
        job_decisions={int(jid):t for jid,t in zip(r['job'][decided],r['decision'][decided])}
        self.assertEqual(len(seeker_decisions),int(decided.sum()))
        # Jobs leave at their decision or at the scheduled posting expiry.
        for jid in range(len(j)):
            expiry=j[jid,0]+40
            departure=j[jid,7]
            if jid in job_decisions:self.assertEqual(departure,job_decisions[jid])
            elif expiry<=days-1:self.assertEqual(departure,expiry)
            else:self.assertTrue(np.isnan(departure))
        # Seekers leave at their decision, or later via a natural exit once their
        # scheduled window has passed; everyone still active has NaN.
        for sid in range(len(s)):
            departure=s[sid,7]
            if sid in seeker_decisions:self.assertEqual(departure,seeker_decisions[sid])
            elif np.isnan(departure):
                # Still live past the window is only allowed with an unresolved case.
                # The unresolved case may be awaiting screening or decision, so any
                # case that has applied but not decided keeps the seeker live.
                if s[sid,0]+20<=days-1:
                    active=r[(r['seeker']==sid)&np.isfinite(r['application'])&~np.isfinite(r['decision'])]
                    self.assertGreater(len(active),0)
            else:self.assertGreaterEqual(departure,s[sid,0]+20)

    def test_fee_rate_is_uniform_and_fee_yen_is_fixed_only_at_decision(self):
        def high_rates(config,f,*args,**kwargs):
            return np.tile(np.array([[.9,1.],[.95,.95],[.95,.95]]),(len(f['skill_match']),1,1))
        config=MarketConfig(days=120,seeker_arrivals=5,job_arrivals=10,seed=71)
        with patch('synthetic_funnel.market.probability_arrays',side_effect=high_rates):
            rows,seekers,jobs=simulate(config)
        self.assertTrue(np.all(jobs[:,8]==FEE_RATE_DEFAULT))
        decided=np.isfinite(rows['decision'])
        self.assertGreater(int(decided.sum()),0)
        expected=np.asarray(jobs[rows['job'][decided],2],dtype=float)*FEE_RATE_DEFAULT
        np.testing.assert_allclose(np.asarray(rows['fee_yen'][decided],dtype=float),expected,rtol=0,atol=1e-12)
        self.assertTrue(np.all(np.isnan(rows['fee_yen'][~decided])))

    def test_competition_counters_follow_market_density(self):
        def high_rates(config,f,*args,**kwargs):
            return np.tile(np.array([[.9,1.],[.95,.95],[.95,.95]]),(len(f['skill_match']),1,1))

        # Eight occupation and eight region categories make the referral pool far
        # stricter than the old 3x4 one; 2/2 and 8/8 over 60 days still yield a
        # few hundred and a few thousand introductions respectively.
        with patch('synthetic_funnel.market.probability_arrays',side_effect=high_rates):
            sparse,_,_=simulate(MarketConfig(days=60,seeker_arrivals=2,job_arrivals=2))
            dense,_,_=simulate(MarketConfig(days=60,seeker_arrivals=8,job_arrivals=8))
            repeat,_,_=simulate(MarketConfig(days=60,seeker_arrivals=2,job_arrivals=2))
        self.assertGreater(len(sparse),0)
        self.assertGreater(len(dense),len(sparse))
        for rows in (sparse,dense):
            self.assertTrue(np.all(rows['other_applications']>=0))
            self.assertTrue(np.all(rows['competing_candidates']>=0))
        self.assertGreater(float(np.mean(dense['other_applications'])),float(np.mean(sparse['other_applications'])))
        self.assertGreater(float(np.mean(dense['competing_candidates'])),float(np.mean(sparse['competing_candidates'])))
        for key in sparse.dtype.names:
            np.testing.assert_array_equal(sparse[key],repeat[key])


class MarketOutcomeTest(unittest.TestCase):
    """issue #32: observed terminal outcomes for every finish() path."""

    # Fingerprints for the issue #46 market stream (occupation/region segment
    # effects + temporal drift on top of the issue #40 direct unseen sampling).
    # The added heterogeneity/drift shifts the funnel probabilities and therefore
    # the market stream, so these hashes intentionally differ from the #40 values.
    # The latent counterfactual and training streams remain independent.
    PRE_CHANGE_GOLDEN = {
        "application": "bd5d99fe536a0cfe04ea46ec317d0401fa12efcad02e6e96287bb415ac23f431",
        "screening": "ce4d2adda0d032f3d954e0d67c7817125383b871cad90e0e3a4217f4a3f79c07",
        "decision": "a3183835d08b78250b9f0bf0a0ede456dd85dce38c22b14e1ed148fd6b97b1ca",
        "pa": "2cd2a9fcf87331cf105de50d1f5a9401e225606271ea3f584331ea6a5b44d76b",
        "ps": "4c003cd51635b696ac80e9831acc05034b56797248da40b9471835ecd9ffad36",
        "pd": "ed22084ee343fdce04faec3dbda19867ab0356785ccfe81cf2dc18ab0740b4a7",
        "p_candidate_application": "2cd2a9fcf87331cf105de50d1f5a9401e225606271ea3f584331ea6a5b44d76b",
        "p_candidate_screening": "7f9c81d90e95a0a64a677acbfae9c87346da8a6b6e04e2a9c814c8dc96e6f56c",
        "p_candidate_decision": "0d28f262ab8188378b78599c596daad3c0126c1e0d5f805398bdfbac75f55102",
        "p_employer_application": "0cd21e588c9d7179e13190a635990964d3e55181d0a2c903980f979beac422fc",
        "p_employer_screening": "7808a782b9af9d52e3e465aecc9a346c6d4f38158c2f10313a4c07c8cdb53ae2",
        "p_employer_decision": "bb383ceb4580f4796754a83159b6ca0efc6e0864ba2939a0bd75f4cc9a3170f4",
        "latent_oubo_dt": "cca586ac4e7ab8d7ea4a8357717606e9bcad2e7b6b8e21595f858ce3cd54830b",
        "latent_shorui_tsuuka_dt": "a3183835d08b78250b9f0bf0a0ede456dd85dce38c22b14e1ed148fd6b97b1ca",
        "latent_kettei_dt": "a3183835d08b78250b9f0bf0a0ede456dd85dce38c22b14e1ed148fd6b97b1ca",
        "failure_stage": "a89dd1ad8ac509b32546998d400066a998d63fb678d985e25b9974fbf7004a44",
        "failure_reason": "eba28b3e9440fd557eeb66dad20077f4ebd8ba4027111232cee207ea986c8efe",
    }
    GOLDEN_ROWS = 1264

    def simulate_with_gates(self, gates, **overrides):
        options = dict(days=30, seeker_arrivals=5, job_arrivals=5, seed=7)
        options.update(overrides)
        with patch('synthetic_funnel.market.probability_arrays',
                   side_effect=tiled_gates(np.asarray(gates))):
            return simulate(MarketConfig(**options))[0]

    def codes(self, rows, *names):
        return np.isin(rows['status'], [OUTCOME_STATUS_CODES[name] for name in names])

    def test_status_enum_is_exactly_six_mutually_exclusive_values(self):
        self.assertEqual(OUTCOME_STATUSES, (
            'not_applied', 'ng', 'withdrawn', 'cancelled_posting_expiry',
            'cancelled_seeker_exit', 'cancelled_market_end',
        ))
        self.assertEqual(set(OUTCOME_STATUS_CODES.values()), set(range(1, 7)))

    def test_ng_side_comes_from_the_existing_gate_draw(self):
        # p = 0.25 with p_employer = 0.5; employer side is the culprit for u < 0.75.
        self.assertEqual(observed_failure_status(0.25, 0.25, 0.5), 'ng')
        self.assertEqual(observed_failure_status(0.749999, 0.25, 0.5), 'ng')
        self.assertEqual(observed_failure_status(0.75, 0.25, 0.5), 'withdrawn')
        self.assertEqual(observed_failure_status(0.99, 0.25, 0.5), 'withdrawn')
        # An employer-only gate (p_employer = 1) can never produce an NG.
        self.assertEqual(observed_failure_status(0.999, 0.01, 1.0), 'withdrawn')

    def test_not_applied_on_stage_zero_gate_failure(self):
        rows = self.simulate_with_gates([[0., 1.], [.5, .5], [.5, .5]])
        self.assertGreater(len(rows), 0)
        self.assertTrue(np.all(self.codes(rows, 'not_applied')))
        self.assertTrue(np.all(~np.isfinite(rows['application'])))
        self.assertTrue(np.all(rows['auto'] == 0))
        self.assertTrue(np.all(np.isfinite(rows['outcome_dt'])))

    def test_gate_failures_record_ng_and_withdrawn_without_auto(self):
        rows = self.simulate_with_gates([[1., 1.], [.25, .5], [.25, .5]])
        gate_failed = self.codes(rows, 'ng', 'withdrawn') & (rows['auto'] == 0)
        self.assertGreater(int(np.sum(gate_failed & self.codes(rows, 'ng'))), 0)
        self.assertGreater(int(np.sum(gate_failed & self.codes(rows, 'withdrawn'))), 0)
        self.assertFalse(np.any(self.codes(rows, 'not_applied')))
        # Stage 1 (screening) failures have no screening date; stage 2 failures do.
        stage1 = gate_failed & ~np.isfinite(rows['screening'])
        stage2 = gate_failed & np.isfinite(rows['screening'])
        self.assertGreater(int(stage1.sum()), 0)
        self.assertGreater(int(stage2.sum()), 0)

    def test_hired_cascades_set_auto_and_skip_the_hired_case(self):
        rows = self.simulate_with_gates([[1., 1.], [1., 1.], [1., 1.]], days=40, referrals=5)
        hired = np.isfinite(rows['decision'])
        auto_rows = rows[rows['auto'] == 1]
        # The hired case itself never receives a negative status.
        self.assertTrue(np.all(rows['status'][hired] == 0))
        self.assertGreater(int(np.sum(self.codes(auto_rows, 'ng'))), 0)
        self.assertGreater(int(np.sum(self.codes(auto_rows, 'withdrawn'))), 0)
        self.assertTrue(np.all(~np.isfinite(auto_rows['decision'])))
        hired_decisions = set(rows['decision'][hired])
        self.assertTrue(all(o in hired_decisions for o in auto_rows['outcome_dt']))

    def test_seeker_exit_and_posting_expiry_are_observed(self):
        seeker_rows = self.simulate_with_gates(
            [[1., 1.], [.5, .5], [.5, .5]], days=40, application_days=3)
        self.assertGreater(int(np.sum(self.codes(seeker_rows, 'cancelled_seeker_exit'))), 0)
        job_rows = self.simulate_with_gates(
            [[1., 1.], [1., 1.], [.5, .5]], days=40, posting_days=8, application_days=30)
        self.assertGreater(int(np.sum(self.codes(job_rows, 'cancelled_posting_expiry'))), 0)

    def test_market_end_safety_net_is_wired_to_cancelled_market_end(self):
        # The safety net is defensive and does not trigger in ordinary runs;
        # guard the wiring so the reason cannot be silently dropped.
        self.assertIn('cancelled_market_end', OUTCOME_STATUSES)
        source = inspect.getsource(simulate)
        self.assertIn('if sid not in live_s or jid not in live_j', source)
        self.assertIn("finish(eid,t,'cancelled_market_end')", source)

    def test_outcome_invariants_on_a_fixed_seed(self):
        config = MarketConfig(days=40, seeker_arrivals=4, job_arrivals=5, seed=71)
        rows, _, _ = simulate(config)
        nonzero = rows['status'] != 0
        np.testing.assert_array_equal(np.isfinite(rows['outcome_dt']), nonzero)
        self.assertTrue(np.all(rows['auto'][~nonzero] == 0))
        self.assertTrue(np.all(np.isin(rows['auto'], (0, 1))))
        self.assertTrue(np.all(rows['auto'][~np.isin(rows['status'],
            (OUTCOME_STATUS_CODES['ng'], OUTCOME_STATUS_CODES['withdrawn']))] == 0))
        self.assertFalse(np.any(nonzero & np.isfinite(rows['decision'])))
        last_stage = np.where(np.isfinite(rows['decision']), rows['decision'],
                     np.where(np.isfinite(rows['screening']), rows['screening'],
                              np.where(np.isfinite(rows['application']), rows['application'],
                                       rows['day'] + 0.01)))
        self.assertTrue(np.all(rows['outcome_dt'][nonzero] >= last_stage[nonzero]))
        self.assertTrue(np.all(rows['outcome_dt'][nonzero] < config.days))

    def test_observed_dates_and_latent_values_are_bit_identical(self):
        rows, _, _ = simulate(MarketConfig(days=40, seeker_arrivals=4, job_arrivals=5, seed=71))
        self.assertEqual(len(rows), self.GOLDEN_ROWS)
        for key, expected in self.PRE_CHANGE_GOLDEN.items():
            digest = hashlib.sha256(np.ascontiguousarray(rows[key]).tobytes()).hexdigest()
            self.assertEqual(digest, expected, f'{key} changed from the pre-change behaviour')


if __name__=='__main__':unittest.main()
