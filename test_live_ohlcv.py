import tempfile,time,unittest
from pathlib import Path
import live_ohlcv as l
class T(unittest.TestCase):
    def test_parse_top_pool_base_and_quote(self):
        body={"data":[{"id":"solana_pool1","attributes":{"address":"pool1","reserve_in_usd":"50000"},"relationships":{"base_token":{"data":{"id":"solana_ABC"}},"quote_token":{"data":{"id":"solana_SOL"}}}}]}
        r=l.parse_top_pool("solana","ABC",body);self.assertEqual((r["pool_address"],r["token_side"]),("pool1","base"))
        r=l.parse_top_pool("solana","SOL",body);self.assertEqual(r["token_side"],"quote")
    def test_evm_address_case_insensitive(self):
        body={"data":[{"id":"eth_0xpool","attributes":{"address":"0xpool","reserve_in_usd":1},"relationships":{"base_token":{"data":{"id":"eth_0xAbC"}},"quote_token":{"data":{"id":"eth_0xDef"}}}}]}
        self.assertEqual(l.parse_top_pool("ethereum","0xabc",body)["token_side"],"base")
    def test_parse_real_ohlcv_shape(self):
        body={"data":{"attributes":{"ohlcv_list":[[1000,1,2,.5,1.5,100]]}}}
        self.assertEqual(l.parse_ohlcv(body)[0],(1000,1,2,.5,1.5,100))
    def test_invalid_bounds_rejected(self):
        with self.assertRaises(ValueError):l.parse_ohlcv({"data":{"attributes":{"ohlcv_list":[[1000,1,.9,.5,1.1,10]]}}})
    def test_save_and_load_true_bars(self):
        with tempfile.TemporaryDirectory() as td:
            db=l.dbopen(Path(td)/"x.db");l.save_bars(db,"solana","ABC","solana","pool",[(1000,1,2,.5,1.5,100),(1060,1.5,2,1.4,1.8,200)])
            rows=l.load_true_bars(db,"solana","ABC");self.assertEqual(len(rows),2);self.assertEqual(rows[-1]["close"],1.8);db.close()
    def test_candidate_selection_priority(self):
        with tempfile.TemporaryDirectory() as td:
            db=l.dbopen(Path(td)/"x.db");db.execute("CREATE TABLE strategy_ab_observations(ts REAL,chain TEXT,address TEXT,symbol TEXT,fast_priority REAL)");n=time.time();
            db.executemany("INSERT INTO strategy_ab_observations VALUES(?,?,?,?,?)",[(n,"solana","a","A",10),(n,"solana","b","B",90)]);db.commit()
            rows=l.select_candidates(db,n,1);self.assertEqual(rows[0]["address"],"b");db.close()
if __name__=='__main__':unittest.main()

class GMGNCandidateTests(unittest.TestCase):
    def test_gmgn_candidate_can_enter_true_ohlcv_collection(self):
        with tempfile.TemporaryDirectory() as td:
            db=l.dbopen(Path(td)/"g.db")
            db.execute("CREATE TABLE strategy_ab_observations(chain TEXT,address TEXT,symbol TEXT,fast_priority REAL,ts REAL)")
            db.execute("CREATE TABLE gmgn_discovery_candidates(chain TEXT,address TEXT,symbol TEXT,priority REAL,last_seen REAL)")
            now=time.time();db.execute("INSERT INTO gmgn_discovery_candidates VALUES(?,?,?,?,?)",("solana","GMGNADDR","GM",95,now));db.commit()
            rows=l.select_candidates(db,now,5);self.assertEqual(rows[0]["address"],"GMGNADDR");db.close()
