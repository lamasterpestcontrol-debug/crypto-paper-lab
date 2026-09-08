import tempfile, unittest, time, json, sqlite3
from pathlib import Path
import shadow_ab

class T(unittest.TestCase):
    def test_db_schema_v07(self):
        with tempfile.TemporaryDirectory() as td:
            db=shadow_ab.dbopen(Path(td)/"x.sqlite3")
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
        with tempfile.TemporaryDirectory() as td:
            db=shadow_ab.dbopen(Path(td)/"x.sqlite3")
            p=shadow_ab.persistence_for_token(db,"solana","abc",time.time(),2,True,True,True)
            self.assertEqual(p.streak,1)
            self.assertFalse(p.confirmed)

    def test_persistence_three_round_deterministic(self):
        """Three-round deterministic persistence test with fixed timestamp and same chain/address."""
        with tempfile.TemporaryDirectory() as td:
            db_path=Path(td)/"x.sqlite3"
            base_ts=1000000.0  # Fixed deterministic timestamp
            chain="solana"
            address="EPjFWaJQwKJrhrarjSuwwnTfnCH3PzC4XQDvDX5n7x3a"  # Fixed address (USDC-like)
            
            results=[]
            
            # Round 1: Initial observation
            db=shadow_ab.dbopen(db_path)
            db.execute("""INSERT INTO strategy_ab_observations(
                ts,chain,address,challenger_state,challenger_score) VALUES(?,?,?,?,?)""",
                (base_ts,"solana",address,"WATCH",2.0))
            db.commit()
            p1,d1=shadow_ab.persistence_for_token(db,chain,address,base_ts,2.0,True,True,True,return_diag=True)
            db.close()
            results.append({"round":1,"streak":p1.streak,"confirmed":p1.confirmed,"key_matches":d1.get("key_matches",0),"failed":d1.get("failed","NONE")})
            self.assertEqual(p1.streak,1,"Round 1 streak must be 1")
            self.assertFalse(p1.confirmed,"Round 1 confirmed must be False")
            self.assertEqual(d1.get("key_matches",0),1,"Round 1 key_matches must be 1")
            
            # Round 2: Add second observation
            db=sqlite3.connect(db_path)
            db.execute("""INSERT INTO strategy_ab_observations(
                ts,chain,address,challenger_state,challenger_score) VALUES(?,?,?,?,?)""",
                (base_ts+60,chain,address,"WATCH",2.0))
            db.commit()
            db.close()
            db=shadow_ab.dbopen(db_path)
            p2,d2=shadow_ab.persistence_for_token(db,chain,address,base_ts+60,2.0,True,True,True,return_diag=True)
            db.close()
            results.append({"round":2,"streak":p2.streak,"confirmed":p2.confirmed,"key_matches":d2.get("key_matches",0),"failed":d2.get("failed","NONE")})
            self.assertEqual(p2.streak,2,"Round 2 streak must be 2")
            self.assertFalse(p2.confirmed,"Round 2 confirmed must be False")
            self.assertEqual(d2.get("key_matches",0),2,"Round 2 key_matches must be 2")
            
            # Round 3: Add third observation (should trigger confirmed)
            db=sqlite3.connect(db_path)
            db.execute("""INSERT INTO strategy_ab_observations(
                ts,chain,address,challenger_state,challenger_score) VALUES(?,?,?,?,?)""",
                (base_ts+120,chain,address,"WATCH",2.0))
            db.commit()
            db.close()
            db=shadow_ab.dbopen(db_path)
            p3,d3=shadow_ab.persistence_for_token(db,chain,address,base_ts+120,2.0,True,True,True,return_diag=True)
            db.close()
            results.append({"round":3,"streak":p3.streak,"confirmed":p3.confirmed,"key_matches":d3.get("key_matches",0),"failed":d3.get("failed","NONE")})
            self.assertEqual(p3.streak,3,"Round 3 streak must be 3")
            self.assertTrue(p3.confirmed,"Round 3 confirmed must be True")
            self.assertEqual(d3.get("key_matches",0),3,"Round 3 key_matches must be 3")
            
            # Emit compact test result
            print("\nPERSISTENCE_THREE_ROUND_TEST_OK: "+json.dumps({"rounds":results,"status":"PASS"}))

    def test_persistence_negative_score_lt_1(self):
        """Negative case: score < 1 must fail and block confirmation."""
        with tempfile.TemporaryDirectory() as td:
            db=shadow_ab.dbopen(Path(td)/"x.sqlite3")
            p,d=shadow_ab.persistence_for_token(db,"solana","test-token",time.time(),0.5,True,True,True,return_diag=True)
            self.assertLess(p.streak,1,"Streak must be <1 when score<1")
            self.assertFalse(p.confirmed,"Confirmed must be False when score<1")
            self.assertIn("SCORE_LT_1",d.get("failed",""),"Must report SCORE_LT_1 failure")

    def test_persistence_negative_exit_false(self):
        """Negative case: exit=False must block confirmation."""
        with tempfile.TemporaryDirectory() as td:
            db=shadow_ab.dbopen(Path(td)/"x.sqlite3")
            p,d=shadow_ab.persistence_for_token(db,"solana","test-token",time.time(),2.0,False,True,True,return_diag=True)
            self.assertFalse(p.confirmed,"Confirmed must be False when exit=False")
            self.assertIn("EXIT_FAIL",d.get("failed",""),"Must report EXIT_FAIL when exit=False")

    def test_persistence_negative_risk_false(self):
        """Negative case: risk=False must block confirmation."""
        with tempfile.TemporaryDirectory() as td:
            db=shadow_ab.dbopen(Path(td)/"x.sqlite3")
            p,d=shadow_ab.persistence_for_token(db,"solana","test-token",time.time(),2.0,True,False,True,return_diag=True)
            self.assertFalse(p.confirmed,"Confirmed must be False when risk=False")
            self.assertIn("RISK_FAIL",d.get("failed",""),"Must report RISK_FAIL when risk=False")

    def test_persistence_negative_liquidity_false(self):
        """Negative case: liquidity=False must block confirmation."""
        with tempfile.TemporaryDirectory() as td:
            db=shadow_ab.dbopen(Path(td)/"x.sqlite3")
            p,d=shadow_ab.persistence_for_token(db,"solana","test-token",time.time(),2.0,True,True,False,return_diag=True)
            self.assertFalse(p.confirmed,"Confirmed must be False when liquidity=False")
            self.assertIn("LIQUIDITY_FAIL",d.get("failed",""),"Must report LIQUIDITY_FAIL when liquidity=False")

    def test_persistence_negative_different_chain_address(self):
        """Negative case: different chain or address must not count in streak."""
        with tempfile.TemporaryDirectory() as td:
            db=shadow_ab.dbopen(Path(td)/"x.sqlite3")
            db.execute("""INSERT INTO strategy_ab_observations(
                ts,chain,address,challenger_state,challenger_score) VALUES(?,?,?,?,?)""",
                (time.time()-60,"solana","token-a","WATCH",2.0))
            db.commit()
            # Query with different chain
            p,d=shadow_ab.persistence_for_token(db,"ethereum","token-a",time.time(),2.0,True,True,True,return_diag=True)
            self.assertEqual(d.get("key_matches",0),0,"Different chain must not match")
            # Query with different address
            p,d=shadow_ab.persistence_for_token(db,"solana","token-b",time.time(),2.0,True,True,True,return_diag=True)
            self.assertEqual(d.get("key_matches",0),0,"Different address must not match")

if __name__=="__main__":
    unittest.main()

