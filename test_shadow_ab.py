import tempfile, unittest, time
from contextlib import closing
from pathlib import Path
import shadow_ab

class T(unittest.TestCase):
    def test_db_schema_v07(self):
        with tempfile.TemporaryDirectory() as td, closing(shadow_ab.dbopen(Path(td)/"x.sqlite3")) as db:
            cols={r[1] for r in db.execute("PRAGMA table_info(strategy_ab_observations)")}
            self.assertIn("prediscovery_stage",cols)
            self.assertIn("persistence_streak",cols)
            self.assertIn("fast_route",cols)
            self.assertIn("fast_priority",cols)
            self.assertIn("deep_analysis",cols)
            self.assertIn("persistence_fail_reason",cols)
            self.assertIn("persistence_key_matches",cols)

    def test_fast_route_proxy(self):
        p={"pairCreatedAt":int(time.time()*1000)-30*60_000,
           "liquidity":{"usd":100000},"volume":{"h1":50000},
           "txns":{"h1":{"buys":30,"sells":5}}}
        r=shadow_ab.fast_route_from_pair(p,time.time())
        self.assertEqual(r.route,"DEEP_ANALYSIS")
        self.assertTrue(r.deep_analysis)

    def test_prediscovery_proxy(self):
        p={"pairCreatedAt":int(time.time()*1000)-3600_000,
           "liquidity":{"usd":50000},"volume":{"h1":10000},
           "info":{"websites":[{"url":"https://example.com"}],"socials":[]}}
        e,r=shadow_ab.prediscovery_from_pair(p,time.time())
        self.assertEqual(r.stage,"RESEARCH")

    def test_persistence_builds(self):
        with tempfile.TemporaryDirectory() as td, closing(shadow_ab.dbopen(Path(td)/"x.sqlite3")) as db:
            p=shadow_ab.persistence_for_token(db,"solana","abc",time.time(),2,True,True,True)
            self.assertEqual(p.streak,1)
            self.assertFalse(p.confirmed)

    def test_persistence_three_tick_confirmed(self):
        with tempfile.TemporaryDirectory() as td, closing(shadow_ab.dbopen(Path(td)/"x.sqlite3")) as db:
            now=time.time()
            for i in range(2):
                db.execute("""INSERT INTO strategy_ab_observations(
                    ts,chain,address,challenger_state,challenger_score) VALUES(?,?,?,?,?)""",
                    (now-120+60*i,"solana","same-token","WATCH",2.0))
            db.commit()
            p,d=shadow_ab.persistence_for_token(db,"solana","same-token",now,2.0,True,True,True,return_diag=True)
            self.assertEqual(p.streak,3)
            self.assertTrue(p.confirmed)
            self.assertEqual(d["key_matches"],2)
            self.assertEqual(d["failed"],"NONE")

    def test_persistence_diagnostic_reason(self):
        with tempfile.TemporaryDirectory() as td, closing(shadow_ab.dbopen(Path(td)/"x.sqlite3")) as db:
            p,d=shadow_ab.persistence_for_token(db,"solana","abc",time.time(),0.2,True,False,True,return_diag=True)
            self.assertEqual(p.streak,0)
            self.assertIn("SCORE_LT_1",d["failed"])
            self.assertIn("EXIT_FAIL",d["failed"])



import copy
import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

class PersistenceRegressionTests(unittest.TestCase):
    BASE=1700000000.0
    ADDRESS="FixedCaseTokenAddress11111111"
    def setUp(self):
        self.db=shadow_ab.dbopen(Path(":memory:"))
        self.addCleanup(self.db.close)
    def record(self,ts=None,score=2.0,state="WATCH",gates=None,address=None,chain="solana",price=1.0,raw=None):
        if gates is not None:
            raw=json.dumps({"persistence_diag":dict(zip(("risk_pass","exit_pass","liquidity_pass"),gates))})
        self.db.execute("INSERT INTO strategy_ab_observations(ts,chain,address,challenger_state,challenger_score,raw_json,price) VALUES(?,?,?,?,?,?,?)",(self.BASE if ts is None else ts,chain,address or self.ADDRESS,state,score,raw,price))
        self.db.commit()
    def evaluate(self,now=None,score=2.0,risk=True,exit_ok=True,liq=True,chain="solana",address=None):
        return shadow_ab.persistence_for_token(self.db,chain,address or self.ADDRESS,self.BASE+120 if now is None else now,score,risk,exit_ok,liq,return_diag=True)
    def pair(self,volume=1000,liquidity=100000):
        return {"priceUsd":"1.0","liquidity":{"usd":liquidity},"volume":{"h1":volume,"h24":100000},"txns":{"h1":{"buys":100,"sells":10}},"priceChange":{"h1":0},"pairCreatedAt":int((self.BASE-600)*1000),"marketCap":1000000,"baseToken":{"address":self.ADDRESS,"symbol":"SYNTHETIC"}}
    def test_fixed_rounds_at_exact_threshold(self):
        got=[]
        for i in range(3):
            p,d=self.evaluate(now=self.BASE+i*60,score=1.0)
            got.append((p.streak,p.confirmed,d["key_matches"]))
            self.record(ts=self.BASE+i*60,score=1.0,gates=(True,True,True))
        self.assertEqual(got,[(1,False,0),(2,False,1),(3,True,2)])
    def test_each_failure_gate_blocks_after_good_history(self):
        self.record();self.record(ts=self.BASE+60)
        for reason,kwargs in [("SCORE_LT_1",{"score":0.999999}),("RISK_FAIL",{"risk":False}),("EXIT_FAIL",{"exit_ok":False}),("LIQUIDITY_FAIL",{"liq":False})]:
            with self.subTest(reason=reason):
                p,d=self.evaluate(**kwargs)
                self.assertEqual((p.streak,p.confirmed,d["failed"]),(0,False,reason))
    def test_persisted_exit_failure_must_break_history(self):
        self.record(gates=(True,True,True));self.record(ts=self.BASE+60,gates=(True,False,True))
        p,d=self.evaluate()
        self.assertEqual(p.streak,1);self.assertFalse(p.confirmed)
        self.assertEqual(d["history_break_reason"],"EXIT_FAIL")
    def test_persisted_liquidity_failure_must_break_history(self):
        self.record(gates=(True,True,True));self.record(ts=self.BASE+60,gates=(True,True,False))
        p,d=self.evaluate();self.assertEqual(p.streak,1);self.assertFalse(p.confirmed)
    def test_persisted_risk_failure_must_break_history(self):
        self.record(gates=(True,True,True));self.record(ts=self.BASE+60,gates=(False,True,True))
        p,d=self.evaluate();self.assertEqual(p.streak,1);self.assertFalse(p.confirmed)
    def test_malformed_history_does_not_pass(self):
        self.record();self.record(ts=self.BASE+60,raw="{broken")
        p,d=self.evaluate();self.assertEqual(p.streak,1)
        self.assertIn("INVALID_HISTORY_DIAGNOSTICS",d["history_gate_sources"])
    def test_unknown_legacy_state_does_not_pass(self):
        self.record();self.record(ts=self.BASE+60,state=None)
        p,d=self.evaluate();self.assertEqual(p.streak,1)
    def test_infinite_current_score_does_not_confirm(self):
        self.record();self.record(ts=self.BASE+60)
        for score in (float("inf"),float("-inf"),float("nan"),"not-a-number"):
            with self.subTest(score=repr(score)):
                p,d=self.evaluate(score=score)
                self.assertFalse(p.confirmed);self.assertEqual(p.streak,0)
                self.assertIn("SCORE_INVALID",d["failed"])
                json.dumps(d,allow_nan=False)
    def test_infinite_history_score_does_not_confirm(self):
        self.record();self.record(ts=self.BASE+60,score=float("inf"))
        p,d=self.evaluate();self.assertEqual(p.streak,1)
    def test_future_rows_cannot_confirm(self):
        self.record(ts=self.BASE+300);self.record(ts=self.BASE+600)
        p,d=self.evaluate();self.assertEqual((p.streak,d["key_matches"]),(1,0))
    def test_same_timestamp_cannot_supply_two_rounds(self):
        self.record();self.record()
        p,d=self.evaluate();self.assertEqual((p.streak,d["key_matches"]),(2,1))
    def test_current_timestamp_not_previous_round(self):
        self.record(ts=self.BASE+120);self.record(ts=self.BASE+120)
        p,d=self.evaluate();self.assertEqual((p.streak,d["key_matches"]),(1,0))
    def test_newer_id_does_not_override_later_timestamp(self):
        self.record(ts=self.BASE+60,score=0.2)
        self.record(ts=self.BASE,score=2.0)
        p,d=self.evaluate();self.assertEqual(p.streak,1)
    def test_solana_case_is_significant(self):
        self.record();self.record(ts=self.BASE+60)
        p,d=self.evaluate(address=self.ADDRESS.lower())
        self.assertEqual((p.streak,d["key_matches"]),(1,0))
    def test_different_chains_cannot_share_history(self):
        self.record();self.record(ts=self.BASE+60)
        p,d=self.evaluate(chain="base")
        self.assertEqual((p.streak,d["key_matches"]),(1,0))
    def test_evm_case_variants_find_existing_history(self):
        addr="0xAbCdEf1234567890123456789012345678901234"
        self.record(chain="base",address=addr)
        self.record(ts=self.BASE+60,chain="base",address=addr.lower())
        p,d=self.evaluate(chain="base",address=addr)
        self.assertEqual((p.streak,d["key_matches"]),(3,2));self.assertTrue(p.confirmed)
    def test_solana_pair_does_not_match_wrong_case(self):
        pair=self.pair();pair["baseToken"]["address"]=self.ADDRESS.lower()
        with patch.object(shadow_ab,"get_json",return_value=[pair]):
            self.assertIsNone(shadow_ab.pair_for("solana",self.ADDRESS))
    def test_first_price_is_not_peak_price(self):
        self.record(price=10);self.record(ts=self.BASE+60,price=20)
        pair=self.pair();pair["priceUsd"]="15"
        shadow_ab.evaluate_one(self.db,"solana",self.ADDRESS,"SYNTHETIC",pair,self.BASE+120)
        row=self.db.execute("SELECT raw_json FROM strategy_ab_observations ORDER BY id DESC LIMIT 1").fetchone()
        diag=json.loads(row[0])["persistence_diag"]
        self.assertEqual(diag["first_seen_price"],10);self.assertEqual(diag["local_high_price"],20)
    def test_price_anchors_exclude_future(self):
        self.record(price=10);self.record(ts=self.BASE+600,price=100)
        self.assertEqual(shadow_ab.price_anchors(self.db,"solana",self.ADDRESS,self.BASE+120,12),(10,12))
    def test_exit_too_thin_is_not_mislabeled_as_risk(self):
        out=shadow_ab.evaluate_one(self.db,"solana",self.ADDRESS,"SYNTHETIC",self.pair(liquidity=8000),self.BASE)
        self.assertEqual(out["reason"],"EXIT_TOO_THIN")
        self.assertIn("EXIT_FAIL",out["persistence_failed"])
        self.assertNotIn("RISK_FAIL",out["persistence_failed"])
    def test_full_pipeline_fixed_sequence_and_saved_flags(self):
        shadow_ab.evaluate_one(self.db,"solana",self.ADDRESS,"SYNTHETIC",self.pair(),self.BASE)
        for i in range(1,4):
            out=shadow_ab.evaluate_one(self.db,"solana",self.ADDRESS,"SYNTHETIC",self.pair(volume=1000*3**i),self.BASE+i*60)
            self.assertEqual(out["persistence"],f"{i}/3")
            self.assertEqual(out["persistence_confirmed"],i==3)
            self.assertEqual(out["persistence_score"],2)
            self.assertEqual(out["address"],self.ADDRESS)
            row=self.db.execute("SELECT raw_json FROM strategy_ab_observations ORDER BY id DESC LIMIT 1").fetchone()
            diag=json.loads(row[0])["persistence_diag"]
            self.assertTrue(diag["risk_pass"] and diag["exit_pass"] and diag["liquidity_pass"])
    def test_same_symbol_does_not_share_history(self):
        for addr in (self.ADDRESS,self.ADDRESS+"B"):
            out=shadow_ab.evaluate_one(self.db,"solana",addr,"SAME_SYMBOL",self.pair(),self.BASE)
            self.assertEqual(out["persistence_key_matches"],0)
            self.assertEqual(out["address"],addr)
    def test_cycle_error_is_visible(self):
        profiles=[{"chainId":"solana","tokenAddress":self.ADDRESS}]
        with patch.object(shadow_ab,"get_json",return_value=profiles),patch.object(shadow_ab,"pair_for",side_effect=ValueError("synthetic error")),redirect_stdout(io.StringIO()) as stream:
            out=shadow_ab.cycle(self.db)
        self.assertEqual(out["errors"],1)
        self.assertIn("STRATEGY_AB_TOKEN_ERROR",stream.getvalue())
        self.assertIn("STRATEGY_AB_CYCLE_DEGRADED",stream.getvalue())
    def test_cycle_deduplicates_profiles_and_logs_address(self):
        profile={"chainId":"solana","tokenAddress":self.ADDRESS}
        with patch.object(shadow_ab,"get_json",return_value=[profile,profile]),patch.object(shadow_ab,"pair_for",return_value=self.pair()) as pair,patch.object(shadow_ab.time,"sleep"),redirect_stdout(io.StringIO()) as stream:
            out=shadow_ab.cycle(self.db)
        self.assertEqual(out["observed"],1);self.assertEqual(pair.call_count,1)
        lines=[json.loads(x) for x in stream.getvalue().splitlines()]
        obs=next(x for x in lines if x["event"]=="STRATEGY_AB_OBSERVATION")
        self.assertEqual(obs["address"],self.ADDRESS)
    def test_invalid_feed_type_fails_visibly(self):
        with patch.object(shadow_ab,"get_json",return_value={"error":"bad"}):
            with self.assertRaises(ValueError):shadow_ab.cycle(self.db)
    def test_gate_state_survives_database_reopen(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"persist.sqlite3"
            for i in range(4):
                db=shadow_ab.dbopen(path)
                try:
                    out=shadow_ab.evaluate_one(db,"solana",self.ADDRESS,"SYNTHETIC",self.pair(volume=1000*3**i),self.BASE+i*60)
                finally:db.close()
            self.assertEqual(out["persistence"],"3/3");self.assertTrue(out["persistence_confirmed"])


    def test_infinite_price_change_is_not_coerced_to_zero(self):
        pair=self.pair();pair["priceChange"]["h1"]=float("inf")
        with self.assertRaises(ValueError):shadow_ab.mk_snapshot(pair,self.BASE)
    def test_invalid_prices_are_rejected(self):
        for price in (0,-1,float("inf"),float("nan"),None):
            with self.subTest(price=repr(price)):
                pair=self.pair();pair["priceUsd"]=price
                with self.assertRaises(ValueError):shadow_ab.mk_snapshot(pair,self.BASE)
    def test_negative_market_quantity_is_rejected(self):
        pair=self.pair();pair["volume"]["h1"]=-1
        with self.assertRaises(ValueError):shadow_ab.mk_snapshot(pair,self.BASE)

if __name__=="__main__":
    unittest.main()
