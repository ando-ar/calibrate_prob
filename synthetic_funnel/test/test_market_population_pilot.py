import unittest
from synthetic_funnel.market_population_pilot import Settings, simulate


class PopulationPilotTest(unittest.TestCase):
    def test_no_referral_lifetime_and_fixed_inflows(self):
        c=Settings(seeker_arrivals=2,job_arrivals=3,application_days=3,posting_days=4,
                   referrals_per_day=0,warmup_days=10,measure_days=20)
        r=simulate(c)
        self.assertEqual(r['population']['seekers']['mean'],6)
        self.assertEqual(r['population']['jobs']['mean'],12)
        self.assertEqual(r['events']['hires'],0)
        self.assertEqual(r['events']['natural_exits'],40)
        self.assertEqual(r['events']['zero_application_exits'],40)

    def test_reproducibility_and_conservation(self):
        c=Settings(seeker_arrivals=3,job_arrivals=5,application_days=12,posting_days=30,
                   warmup_days=0,measure_days=120,seed=7)
        a=simulate(c)
        self.assertEqual(a,simulate(c))
        last=a['daily'][-1]
        self.assertEqual(last['seekers'],3*120-a['events']['natural_exits']-a['events']['hires'])
        self.assertEqual(last['jobs'],5*120-a['events']['expired_jobs']-a['events']['hires'])
        self.assertLessEqual(a['events']['referrals'],5*sum(d['eligible'] for d in a['daily']))
        self.assertGreater(a['events']['zero_application_exits'],0)


if __name__=='__main__':
    unittest.main()
