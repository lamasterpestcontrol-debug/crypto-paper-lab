import unittest
from strategy_v07 import *

class T(unittest.TestCase):
    def test_fast_reject(self):
        self.assertEqual(fast_route(FastInput(1000,10000,20,5,10)).route,"REJECT_FAST")
    def test_deep(self):
        r=fast_route(FastInput(100000,50000,30,5,30))
        self.assertTrue(r.deep_analysis)
    def test_cost_attribution(self):
        rows=[StrategyTrade("VBP",1,2,100,10,10,5,-8),
              StrategyTrade("VBP",1,2,-20,5,5,5,-12)]
        a=attribution(rows)["VBP"]
        self.assertEqual(a["trades"],2)
        self.assertLess(a["net_pnl_usd"],80)
    def test_health_sample(self):
        self.assertEqual(strategy_health({"trades":2}),"INSUFFICIENT_SAMPLE")
    def test_health_keep(self):
        self.assertEqual(strategy_health({"trades":50,"net_pnl_usd":500,
            "profit_factor":1.5,"max_drawdown_pct":-15}),"KEEP")

if __name__=="__main__": unittest.main()
