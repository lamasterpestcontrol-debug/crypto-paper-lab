import unittest
from strategy_v08 import *

def trend_signal(price=100, atrv=2, rsi_v=60, ready=True):
    return TrendSnapshot(price,95,90,rsi_v,atrv,atrv/price,1.5,True,True,False,ready,"TEST")

class T(unittest.TestCase):
    def test_indicators(self):
        bars=[]
        for i in range(60):
            c=100+i*.5; bars.append(Bar(i+1,c-.2,c+.5,c-.5,c,1000+i*20))
        s=analyze_mature_crypto(bars); self.assertTrue(s.bullish_structure); self.assertGreater(s.ema20,s.ema50)
    def test_early_profile_needs_only_21_bars(self):
        bars=[]
        for i in range(25):
            c=100+i*.5; bars.append(Bar(i+1,c-.2,c+.5,c-.5,c,1000+i*30))
        s=analyze_early_crypto(bars); self.assertTrue(s.bullish_structure)
    def test_partial_profit(self):
        s=PositionState(100,50,0.5,100,100,0.5)
        a=manage_position(s,126,trend_signal(126),params=BALANCED)
        self.assertTrue(any(x.kind=="SELL_PART" and x.reason=="TAKE_25" for x in a))
        self.assertAlmostEqual(s.qty,.4)
    def test_trailing_protects_profit(self):
        s=PositionState(100,50,.5,100,140,.5)
        a=manage_position(s,130,trend_signal(130,atrv=2),params=BALANCED)
        self.assertEqual(a[0].kind,"EXIT_ALL"); self.assertEqual(a[0].reason,"TRAILING_PROFIT_PROTECTION")
    def test_never_average_down(self):
        s=PositionState(100,50,.5,100,110,.5)
        sig=trend_signal(95); self.assertFalse(pyramiding_allowed(s,95,sig,BALANCED))
    def test_add_only_profitable_pullback(self):
        s=PositionState(100,50,.5,100,125,.5)
        sig=TrendSnapshot(115,110,100,60,5,.0435,1.5,False,True,False,True,"TEST")
        self.assertTrue(pyramiding_allowed(s,115,sig,BALANCED))
        a=manage_position(s,115,sig,params=BALANCED)
        self.assertTrue(any(x.kind=="ADD" for x in a)); self.assertEqual(s.deployed_usd,75)
    def test_variants_sum(self):
        for p in variant_grid().values():
            self.assertAlmostEqual(sum(x.sell_fraction_initial for x in p.tiers)+p.runner_fraction_initial,1)

if __name__=='__main__': unittest.main()

class RegimeSizingTests(unittest.TestCase):
    def signal(self):
        return TrendSnapshot(1,0.98,0.95,60,.02,.02,1.5,True,True,False,True,"TEST")
    def test_regime_initial_sizing_order(self):
        s=self.signal()
        vals=[initial_entry_budget(100,s,p) for p in (CONSERVATIVE,BALANCED,AGGRESSIVE)]
        self.assertEqual(vals,[35.0,50.0,60.0])
    def test_conservative_max_deployment_is_seventy_percent(self):
        p=CONSERVATIVE;s=self.signal()
        st=PositionState(100,70,10,1,1.2,10)
        self.assertFalse(pyramiding_allowed(st,1.15,s,p))
