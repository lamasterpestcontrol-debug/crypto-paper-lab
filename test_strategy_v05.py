import unittest,time
from strategy_v05 import *

class T(unittest.TestCase):
    def test_real_pre_token(self):
        e=Evidence(time.time(),github_activity=.9,product_live=.8,ecosystem_support=.7,
                   onchain_adoption=.5,social_early_growth=.2,catalyst=.6,
                   official_identity=1,independent_sources=4)
        r=classify(e)
        self.assertEqual(r.stage,"PRE_TOKEN_WATCH")
        self.assertTrue(r.route_to_risk)

    def test_hype_only_rejected(self):
        e=Evidence(time.time(),social_early_growth=1,paid_promo_risk=.9,
                   official_identity=1,independent_sources=3)
        self.assertEqual(classify(e).stage,"REJECT")

    def test_unverified_identity(self):
        e=Evidence(time.time(),github_activity=1,product_live=1,
                   official_identity=.2,independent_sources=5)
        self.assertFalse(classify(e).route_to_risk)

    def test_first_pool_handoff(self):
        e=Evidence(time.time(),github_activity=.8,product_live=.8,onchain_adoption=.7,
                   ecosystem_support=.7,contract_deployed=1,first_pool=1,
                   official_identity=1,independent_sources=4)
        r=classify(e)
        self.assertEqual(r.stage,"FIRST_POOL_HANDOFF")
        self.assertTrue(r.route_to_risk)

    def test_freshness(self):
        self.assertGreater(freshness_weight(2),freshness_weight(200))

if __name__=="__main__": unittest.main()
