import sqlite3
import tempfile
import unittest
from pathlib import Path
from history_replay import db_connect, catalog_response, parse_ohlcv, save_ohlcv

class HistoryTests(unittest.TestCase):
    def test_catalog_tags_survivor_bias(self):
        with tempfile.TemporaryDirectory() as d:
            db=db_connect(Path(d)/"x.db")
            body={"data":[{"id":"eth_0xabc","attributes":{"name":"AAA/ETH","pool_created_at":"2021-01-01T00:00:00Z"}}]}
            self.assertEqual(catalog_response(db,body,"current_top_pool_survivor_biased",1.0),1)
            r=db.execute("select * from history_pool_catalog").fetchone()
            self.assertEqual(r["cohort"],"current_top_pool_survivor_biased")

    def test_ohlcv_round_trip(self):
        body={"data":{"attributes":{"ohlcv_list":[[1000,1,2,0.5,1.5,99]]}}}
        rows=parse_ohlcv(body)
        self.assertEqual(rows[0][0],1000)
        with tempfile.TemporaryDirectory() as d:
            db=db_connect(Path(d)/"x.db")
            self.assertEqual(save_ohlcv(db,"eth","0xabc","day",rows),1)
            self.assertEqual(save_ohlcv(db,"eth","0xabc","day",rows),0)

    def test_invalid_ohlcv_rejected(self):
        with self.assertRaises(ValueError):
            parse_ohlcv({"x":1})

if __name__=="__main__":
    unittest.main()
