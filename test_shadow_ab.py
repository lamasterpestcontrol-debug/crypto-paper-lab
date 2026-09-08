import tempfile, unittest, time
from pathlib import Path
import shadow_ab
from strategy_v03 import MarketSnapshot

class T(unittest.TestCase):
    def test_db_schema(self):
        with tempfile.TemporaryDirectory() as td:
            db=shadow_ab.dbopen(Path(td)/"x.sqlite3")
            names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("strategy_ab_observations",names)

    def test_control(self):
        p={"marketCap":100000,"liquidity":{"usd":50000},"volume":{"h24":50000},
           "txns":{"h1":{"buys":12,"sells":5}}}
        self.assertEqual(shadow_ab.control_state(p),"ELIGIBLE")

    def test_prev(self):
        self.assertIsNone(shadow_ab.mk_prev(None))

if __name__=="__main__":
    unittest.main()
