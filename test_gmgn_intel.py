import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import gmgn_intel as g

class T(unittest.TestCase):
    def item(self,**kw):
        x={"address":"ADDR123456789","symbol":"NEW","name":"New Token","launchpad_platform":"Pump.fun","created_timestamp":1000,
           "usd_market_cap":70000,"liquidity":40000,"smart_degen_count":3,"renowned_count":1,"top_10_holder_rate":.15,
           "rat_trader_amount_rate":.08,"bundler_trader_amount_rate":.07,"rug_ratio":.05,"is_wash_trading":False,"sniper_count":2}
        x.update(kw);return x
    def test_iter_grouped_trenches(self):
        got=list(g.iter_tokens({"data":{"new_creation":[self.item()]}}));self.assertEqual(len(got),1);self.assertEqual(got[0][1]["symbol"],"NEW")
    def test_high_insider_or_wash_fails_contract(self):
        self.assertFalse(g.risk_metrics(self.item(rat_trader_amount_rate=.6))["contract_pass"])
        self.assertFalse(g.risk_metrics(self.item(is_wash_trading=True))["contract_pass"])
    def test_unknown_safety_is_not_claimed_safe(self):
        x={"address":"ADDR123456789","smart_degen_count":1};self.assertIsNone(g.risk_metrics(x)["contract_pass"])
    def test_save_writes_candidate_and_shared_intel(self):
        with tempfile.TemporaryDirectory() as td:
            db=g.dbopen(Path(td)/"x.db");self.addCleanup(db.close)
            self.assertTrue(g.save_token(db,"sol","new_creation",self.item(),1000))
            c=db.execute("select * from gmgn_discovery_candidates").fetchone();self.assertEqual(c["chain"],"solana");self.assertEqual(c["sighting_streak"],1)
            i=db.execute("select * from token_intel_signals").fetchone();self.assertEqual(i["source"],"GMGN");self.assertEqual(i["contract_risk_pass"],1)
    def test_repeated_sightings_build_persistence_without_duplicate_same_tick(self):
        with tempfile.TemporaryDirectory() as td:
            db=g.dbopen(Path(td)/"x.db");self.addCleanup(db.close)
            for t in (1000,1030,1060):g.save_token(db,"sol","new_creation",self.item(),t)
            self.assertEqual(db.execute("select sighting_streak from gmgn_discovery_candidates").fetchone()[0],3)
    def test_missing_key_disables_without_crash(self):
        with tempfile.TemporaryDirectory() as td,patch.dict("os.environ",{},clear=True):
            db=g.dbopen(Path(td)/"x.db");self.addCleanup(db.close);r=g.cycle(db,1000,chains=());self.assertFalse(r["enabled"])
    def test_cycle_normalizes_mock_cli(self):
        with tempfile.TemporaryDirectory() as td,patch.dict("os.environ",{"GMGN_API_KEY":"test"},clear=False),patch.object(g,"fetch_chain",return_value={"data":{"new_creation":[self.item()]}}):
            db=g.dbopen(Path(td)/"x.db");self.addCleanup(db.close);r=g.cycle(db,1000,chains=("sol",));self.assertEqual(r["observed"],1)
    def test_cli_never_passes_private_key_to_child(self):
        cp=type("X",(),{"returncode":0,"stdout":"{}","stderr":""})()
        with patch.dict("os.environ",{"GMGN_API_KEY":"k","GMGN_PRIVATE_KEY":"secret"},clear=False),patch.object(g.shutil,"which",return_value="/x/gmgn-cli"),patch.object(g.subprocess,"run",return_value=cp) as run:
            g.run_cli(["market","trending"]);env=run.call_args.kwargs["env"];self.assertNotIn("GMGN_PRIVATE_KEY",env);self.assertEqual(env["GMGN_API_KEY"],"k")

if __name__=="__main__":unittest.main()
