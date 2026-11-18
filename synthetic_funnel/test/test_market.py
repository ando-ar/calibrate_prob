import unittest
from typing import Any, cast
from unittest.mock import patch

import numpy as np

from synthetic_funnel.market import MarketConfig, simulate


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

    def test_profile_arrays_track_departures(self):
        days=130
        r,s,j=simulate(MarketConfig(days=days,seeker_arrivals=5,job_arrivals=10))
        self.assertEqual(s.shape[1],8);self.assertEqual(j.shape[1],8)
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
                if s[sid,0]+20<=days-1:
                    active=r[(r['seeker']==sid)&np.isfinite(r['screening'])&~np.isfinite(r['decision'])]
                    self.assertGreater(len(active),0)
            else:self.assertGreaterEqual(departure,s[sid,0]+20)


if __name__=='__main__':unittest.main()
