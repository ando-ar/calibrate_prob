import unittest
import numpy as np
from synthetic_funnel.market import DTYPE,extract,SamplingError


class SamplingTest(unittest.TestCase):
    def setUp(self):
        self.rows=np.zeros(4,dtype=DTYPE)
        self.rows['seeker']=[1,1,1,2]
        self.rows['job']=[1,2,3,4]
        self.rows['day']=10
        for k in ('application','screening','decision'):self.rows[k]=np.nan
        self.rows['application'][:2]=11
        self.horizon=np.full(4,100.)
    def test_negative_reuse_only_across_positives(self):
        r=extract(self.rows,self.horizon,0,(0,20),101,'matched',positive_count='all',negative_ratio=1)
        np.testing.assert_array_equal(r['matches'],[[0,2],[1,2]])
        np.testing.assert_array_equal(r['negative_multiplicity'],[2])
    def test_shortage_and_uniform_contract(self):
        with self.assertRaisesRegex(SamplingError,'INSUFFICIENT_MATCHED_NEGATIVES'):
            extract(self.rows,self.horizon,0,(0,20),101,'matched',positive_count='all',negative_ratio=2)
        filtered=extract(self.rows,self.horizon,0,(0,20),101,'matched',positive_count='all',negative_ratio=2,positive_eligibility='has_matched_negatives')
        self.assertEqual(filtered['excluded_positives'],2)
        self.assertEqual(len(filtered['positive_indices']),0)
        r=extract(self.rows,self.horizon,0,(0,20),101,'uniform',sample_size=4)
        self.assertIn(3,r['indices'])
        with self.assertRaises(SamplingError):
            extract(self.rows,self.horizon,0,(0,20),101,'uniform',sample_size=4,positive_count=2)
        with self.assertRaises(SamplingError):
            extract(self.rows,self.horizon,0,(0,20),50,'uniform',sample_size=1)

    def test_later_stages_use_global_negative_pool(self):
        self.rows['application']=11
        self.rows['screening'][0]=12
        r=extract(self.rows,self.horizon,1,(0,20),101,'matched',positive_count='all',negative_ratio=3)
        self.assertEqual(set(r['negative_indices']),{1,2,3})
        self.rows['screening']=12
        self.rows['decision'][0]=13
        r=extract(self.rows,self.horizon,2,(0,20),101,'matched',positive_count='all',negative_ratio=3)
        self.assertEqual(set(r['negative_indices']),{1,2,3})


if __name__=='__main__':unittest.main()
