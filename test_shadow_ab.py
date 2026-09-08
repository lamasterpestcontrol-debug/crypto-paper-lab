import tempfile, unittest, time
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

if __name__=="__main__":
    unittest.main()
