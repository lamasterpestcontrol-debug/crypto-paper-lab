import tempfile, unittest, time
from pathlib import Path
import shadow_ab

class T(unittest.TestCase):
    def test_db_schema_v056(self):
        with tempfile.TemporaryDirectory() as td:
            db=shadow_ab.dbopen(Path(td)/"x.sqlite3")
            cols={r[1] for r in db.execute("PRAGMA table_info(strategy_ab_observations)")}
            self.assertIn("prediscovery_stage",cols)
            self.assertIn("persistence_streak",cols)

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
