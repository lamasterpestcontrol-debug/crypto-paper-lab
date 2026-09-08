import unittest
from strategy_v04 import *

class T(unittest.TestCase):
    def test_contract_block(self):
        r=assess_contract_risk(ContractRiskInput(honeypot=True))
        self.assertFalse(r.pass_check)
        self.assertIn("HONEYPOT",r.blockers)

    def test_wallet_dev_sell(self):
        r=assess_wallet_flow(WalletFlowInput(dev_sell_usd_1h=10000))
        self.assertFalse(r.pass_check)

    def test_slippage(self):
        r=estimate_slippage(10000,1000)
        self.assertFalse(r.pass_check)

    def test_dynamic_exit(self):
        r=dynamic_exit(ExitInput(80,-10,-50,-10))
        self.assertEqual(r.action,"EXIT_ALL")

    def test_regime(self):
        self.assertEqual(classify_regime(RegimeInput(btc_return_24h_pct=-8)),"RISK_OFF")

    def test_ab(self):
        s=experiment_summary([
            TradeOutcome("A",10,-5),TradeOutcome("A",-5,-8),
            TradeOutcome("B",20,-6),TradeOutcome("B",10,-4)])
        self.assertGreater(s["B"]["win_rate"],s["A"]["win_rate"])

    def test_graduation_fail(self):
        r=graduation_gate(GraduationInput(
            100,10,1.1,1.0,-1,-30,False,False,80))
        self.assertFalse(r.ready)
        self.assertGreater(len(r.failed),3)

    def test_graduation_pass(self):
        r=graduation_gate(GraduationInput(
            500,100,1.8,1.5,4,-18,True,True,20))
        self.assertTrue(r.ready)

if __name__=="__main__":
    unittest.main()
