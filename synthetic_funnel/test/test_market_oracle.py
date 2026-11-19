import unittest
import numpy as np
from scipy.integrate import quad
from scipy.special import ndtr
from synthetic_funnel.market import DTYPE,FEE_RATE_DEFAULT,OCCUPATIONS,REGIONS,MarketConfig,simulate


class OracleTest(unittest.TestCase):
    def test_forced_path_importance_without_competitors(self):
        row=np.zeros(1,dtype=DTYPE)
        for k in ('application','screening','decision'):row[k]=np.nan
        row['pa']=.8;row['ps']=.7;row['pd']=.6
        snapshot=dict(day=0,seekers=[(0,.5,700,OCCUPATIONS[0],REGIONS[0],0,0,float('nan'))],
                      jobs=[(0,.5,700,OCCUPATIONS[0],REGIONS[0],0,0,float('nan'),FEE_RATE_DEFAULT)],
                      live_s={0:dict(until=20,pending=set(),seen={0})},live_j={0:dict(until=40,pending=set())},
                      rows=row,keys=[(0,0)],anchors=(0,0,0))
        results=[]
        for seed in range(200):
            r=simulate(MarketConfig(days=42,seeker_arrivals=0,job_arrivals=0),resume=snapshot,focal_index=0,branch_seed=seed)
            results.append([np.isfinite(r[k]) for k in ('application','screening','decision')])
        empirical=np.array(results).mean(axis=0)
        def cdf(t,m,s):
            return max(0,(ndtr((t-m)/s)-ndtr(-m/s))/ndtr(m/s))
        def pdf(t,m,s):return np.exp(-.5*((t-m)/s)**2)/(s*np.sqrt(2*np.pi)*ndtr(m/s))
        expected=quad(lambda a:pdf(a,3,2)*quad(lambda b:pdf(b,5,4)*cdf(39.99-a-b,30,20),0,39.99-a)[0],0,19.99)[0]
        self.assertGreater(empirical[0],.98)
        self.assertGreater(empirical[1],.98)
        self.assertAlmostEqual(empirical[2],expected,delta=.12)


if __name__=='__main__':unittest.main()
