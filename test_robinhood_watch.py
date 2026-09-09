import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import robinhood_watch as rw


class RobinhoodWatchTests(unittest.TestCase):
    def test_event_signature_matches_v2_source_shape(self):
        self.assertEqual(rw.TOKEN_LAUNCHED_SIGNATURE, "TokenLaunched(address,address,address,address,uint256,uint256)")

    def test_address_from_indexed_topic(self):
        addr = "0x7dbf38976f6d3b9c529e7d9484a71898b409ee6a"
        topic = "0x" + "00" * 12 + addr[2:]
        self.assertEqual(rw.address_from_topic(topic), addr)

    def test_decode_standard_abi_string(self):
        value = b"ZZZ"
        data = (32).to_bytes(32, "big") + len(value).to_bytes(32, "big") + value + b"\x00" * 29
        self.assertEqual(rw._decode_abi_text("0x" + data.hex()), "ZZZ")

    def test_momentum_is_hot_for_early_high_turnover_token(self):
        pair = {"liquidity": {"usd": 100000}, "volume": {"h24": 2000000}, "txns": {"h1": {"buys": 30, "sells": 20}}}
        level, score, reason = rw.momentum(pair, 1000, 1000 + 6 * 3600)
        self.assertEqual(level, "HOT")
        self.assertGreaterEqual(score, 8)
        self.assertIn("vol/liq>=3x", reason)

    def test_meme_watch_does_not_create_position(self):
        with tempfile.TemporaryDirectory() as d:
            db = rw.db_connect(Path(d) / "x.db")
            db.execute("""CREATE TABLE candidates(chain TEXT,address TEXT,symbol TEXT,name TEXT,pair_address TEXT,
              first_seen REAL,last_seen REAL,entry_price REAL,current_price REAL,market_cap REAL,liquidity REAL,
              volume24 REAL,score INTEGER,evidence TEXT,dex_url TEXT,max_return REAL DEFAULT 0,PRIMARY KEY(chain,address))""")
            db.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY, chain TEXT, address TEXT, status TEXT)")
            rw.upsert_launch(db, "0x7dbf38976f6d3b9c529e7d9484a71898b409ee6a", 1, "0xabc", 1000, "ZZZ", "ZZZ")
            fake_pair = {"chainId": "robinhood", "baseToken": {"address": "0x7dbf38976f6d3b9c529e7d9484a71898b409ee6a", "symbol": "ZZZ", "name": "ZZZ"},
                         "priceUsd": "0.01", "marketCap": 8400000, "liquidity": {"usd": 100000}, "volume": {"h24": 2000000},
                         "txns": {"h1": {"buys": 30, "sells": 20}}, "url": "https://dex"}
            with patch.object(rw, "dex_pairs", return_value=[fake_pair]):
                result = rw.enrich_one(db, "0x7dbf38976f6d3b9c529e7d9484a71898b409ee6a", now=1000 + 3600)
            self.assertEqual(result["alert_level"], "HOT")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 0)
            c = db.execute("SELECT evidence,score FROM candidates WHERE chain='robinhood'").fetchone()
            self.assertIn("WATCH_ONLY:robinhood_pons", c["evidence"])
            self.assertEqual(c["score"], 0)
            db.close()

    def test_zzz_fixture_is_seeded_and_live_watched(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cases.json"
            path.write_text(json.dumps([{
                "case_id": "zzz", "chain": "robinhood", "address": "0x7dbf38976f6d3b9c529e7d9484a71898b409ee6a",
                "symbol": "ZZZ", "platform": "Pons V2", "root_causes": ["x"], "desired_detection": ["y"]
            }]), encoding="utf-8")
            db = rw.db_connect(Path(d) / "x.db")
            self.assertEqual(rw.seed_missed_cases(db, path), 1)
            case = db.execute("SELECT * FROM missed_opportunity_cases WHERE case_id='zzz'").fetchone()
            watch = db.execute("SELECT * FROM robinhood_launch_watch WHERE address=?", ("0x7dbf38976f6d3b9c529e7d9484a71898b409ee6a",)).fetchone()
            self.assertIsNotNone(case); self.assertEqual(watch["symbol"], "ZZZ")
            db.close()


if __name__ == "__main__":
    unittest.main()
