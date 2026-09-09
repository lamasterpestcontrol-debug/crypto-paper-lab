import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from history_replay import db_connect, catalog_response, parse_ohlcv, save_ohlcv

class HistoryTests(unittest.TestCase):
    def test_catalog_tags_survivor_bias(self):
        with tempfile.TemporaryDirectory() as d, closing(db_connect(Path(d)/"x.db")) as db:
            body={"data":[{"id":"eth_0xabc","attributes":{"name":"AAA/ETH","pool_created_at":"2021-01-01T00:00:00Z"}}]}
            self.assertEqual(catalog_response(db,body,"current_top_pool_survivor_biased",1.0),1)
            r=db.execute("select * from history_pool_catalog").fetchone()
            self.assertEqual(r["cohort"],"current_top_pool_survivor_biased")

    def test_ohlcv_round_trip(self):
        body={"data":{"attributes":{"ohlcv_list":[[1000,1,2,0.5,1.5,99]]}}}
        rows=parse_ohlcv(body)
        self.assertEqual(rows[0][0],1000)
        with tempfile.TemporaryDirectory() as d, closing(db_connect(Path(d)/"x.db")) as db:
            self.assertEqual(save_ohlcv(db,"eth","0xabc","day",rows),1)
            self.assertEqual(save_ohlcv(db,"eth","0xabc","day",rows),0)

    def test_invalid_ohlcv_rejected(self):
        with self.assertRaises(ValueError):
            parse_ohlcv({"x":1})

import io,json
from contextlib import redirect_stdout
from unittest.mock import patch
import history_replay as hr

class BackfillRegressionTests(unittest.TestCase):
    def setUp(self):
        self.db=hr.db_connect(Path(":memory:"));self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO history_backfill_state(network,pool_address) VALUES(?,?)",("eth","0xabc"));self.db.commit()
        self.worker=hr.Worker(self.db)
    def body(self,rows):return {"data":{"attributes":{"ohlcv_list":rows}}}
    def state(self):return self.db.execute("SELECT * FROM history_backfill_state").fetchone()
    def test_transient_error_after_many_attempts_not_done(self):
        self.db.execute("UPDATE history_backfill_state SET attempts=100");self.db.commit()
        with patch.object(self.worker,"call",side_effect=TimeoutError("test timeout")):
            with self.assertRaises(TimeoutError):self.worker.backfill()
        row=self.state();self.assertEqual(row["done"],0);self.assertEqual(row["consecutive_errors"],1)
        self.assertGreater(row["retry_after"],row["last_attempt"])
    def test_failed_pool_has_retry_backoff(self):
        with patch.object(self.worker,"call",side_effect=TimeoutError("test")):
            with self.assertRaises(TimeoutError):self.worker.backfill()
        self.assertIsNone(self.worker.target())
    def test_success_resets_errors_without_claiming_done(self):
        self.db.execute("UPDATE history_backfill_state SET consecutive_errors=4,last_error='old',retry_after=0");self.db.commit()
        with patch.object(self.worker,"call",return_value=self.body([[1000,1,2,0.5,1.5,99]])):
            self.worker.backfill()
        row=self.state();self.assertEqual(row["consecutive_errors"],0);self.assertIsNone(row["last_error"]);self.assertEqual(row["done"],0)
    def test_only_two_valid_empty_pages_complete(self):
        with patch.object(self.worker,"call",return_value=self.body([])):
            self.worker.backfill();self.assertEqual(self.state()["done"],0)
            self.worker.backfill();self.assertEqual(self.state()["done"],1)
    def test_malformed_page_does_not_count_as_empty(self):
        with patch.object(self.worker,"call",return_value=self.body({"error":"not rows"})):
            with self.assertRaises(ValueError):self.worker.backfill()
        self.assertEqual(self.state()["empty_pages"],0);self.assertEqual(self.state()["done"],0)
    def test_bad_numeric_row_rejected(self):
        for value in (float("nan"),float("inf"),-1,"bad"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):hr.parse_ohlcv(self.body([[1000,1,2,0.5,1.5,value]]))
    def test_inconsistent_price_bounds_rejected(self):
        with self.assertRaises(ValueError):hr.parse_ohlcv(self.body([[1000,3,2,0.5,1.5,99]]))
    def test_legacy_suspect_done_is_visible_not_silently_rewritten(self):
        self.db.execute("UPDATE history_backfill_state SET done=1,last_error='timeout',empty_pages=0");self.db.commit()
        self.assertEqual(hr.status(self.db)["suspect_completed_pools"],1)
        self.assertEqual(self.state()["done"],1)
    def test_cycle_error_is_logged_to_stdout(self):
        with patch.object(self.worker,"call",side_effect=TimeoutError("test")),redirect_stdout(io.StringIO()) as out:
            self.worker.cycle(2)
        self.assertIn("HISTORY_JOB_ERROR",out.getvalue())
    def test_schema_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"old.db"
            db=sqlite3.connect(path)
            db.execute("CREATE TABLE history_backfill_state(network TEXT NOT NULL,pool_address TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,empty_pages INTEGER NOT NULL DEFAULT 0,last_attempt REAL,last_success REAL,done INTEGER NOT NULL DEFAULT 0,last_error TEXT,PRIMARY KEY(network,pool_address))")
            db.commit();db.close()
            for _ in range(2):
                db=hr.db_connect(path)
                try:self.assertIn("retry_after",{row[1] for row in db.execute("PRAGMA table_info(history_backfill_state)")})
                finally:db.close()

class BackfillBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.db=hr.db_connect(Path(":memory:"));self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO history_backfill_state(network,pool_address) VALUES(?,?)",("eth","0xabc"))
        self.db.commit();self.worker=hr.Worker(self.db)
    def body(self,rows):return {"data":{"attributes":{"ohlcv_list":rows}}}
    def state(self):return self.db.execute("SELECT * FROM history_backfill_state").fetchone()
    def test_underscore_in_network_id_is_preserved(self):
        row=hr.parse_pool({"id":"polygon_pos_0xabc","attributes":{"address":"0xabc"}})
        self.assertEqual(row[:2],("polygon_pos","0xabc"))
    def test_contradictory_address_is_rejected(self):
        self.assertIsNone(hr.parse_pool({"id":"eth_0xabc","attributes":{"address":"0xdef"}}))
    def test_catalog_does_not_mangle_network_id(self):
        body={"data":[{"id":"polygon_pos_0xdef","attributes":{"address":"0xdef"}}]}
        hr.catalog_response(self.db,body,"test",1700000000)
        row=self.db.execute("SELECT network,pool_address FROM history_pool_catalog").fetchone()
        self.assertEqual(tuple(row),("polygon_pos","0xdef"))
    def test_nonempty_repeated_page_is_not_progress_or_completion(self):
        hr.save_ohlcv(self.db,"eth","0xabc","day",[(1000,1,2,0.5,1.5,99)])
        with patch.object(self.worker,"call",return_value=self.body([[1000,1,2,0.5,1.5,99]])),redirect_stdout(io.StringIO()) as out:
            with self.assertRaisesRegex(ValueError,"NO_BACKFILL_PROGRESS"):self.worker.backfill()
        self.assertEqual(self.state()["done"],0)
        self.assertEqual(self.state()["empty_pages"],0)
        self.assertIn("HISTORY_BACKFILL_RETRY",out.getvalue())
        self.assertIsNone(self.state()["last_success"])
    def test_inclusive_boundary_does_not_discard_older_candles(self):
        hr.save_ohlcv(self.db,"eth","0xabc","day",[(1000,1,2,0.5,1.5,99)])
        with patch.object(self.worker,"call",return_value=self.body([[1000,1,2,0.5,1.5,99],[900,1,2,0.5,1.5,99]])):
            result=self.worker.backfill()
        self.assertEqual(result["inserted"],1);self.assertEqual(result["earliest_after"],900)
        self.assertEqual(self.state()["done"],0)
    def test_future_candle_is_not_saved(self):
        with patch.object(hr.time,"time",return_value=1700000000),patch.object(self.worker,"call",return_value=self.body([[1700000001,1,2,0.5,1.5,99]])),redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError,"FUTURE_OHLCV_TIMESTAMP"):self.worker.backfill()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM history_ohlcv").fetchone()[0],0)
        self.assertEqual(self.state()["done"],0)
    def test_retry_budget_does_not_starve_other_pool(self):
        self.db.execute("INSERT INTO history_backfill_state(network,pool_address,last_attempt) VALUES(?,?,?)",("eth","0xother",1));self.db.commit()
        with patch.object(self.worker,"call",side_effect=TimeoutError("test")),redirect_stdout(io.StringIO()):
            with self.assertRaises(TimeoutError):self.worker.backfill()
        self.assertEqual(self.worker.target()["pool_address"],"0xother")
    def test_bad_row_rejects_page_without_partial_save(self):
        with patch.object(self.worker,"call",return_value=self.body([[1000,1,2,0.5,1.5,99],[900,1,2,0.5,1.5,float("nan")]])),redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):self.worker.backfill()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM history_ohlcv").fetchone()[0],0)
    def test_existing_error_metadata_preserved_by_migration(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"state.db";db=hr.db_connect(path)
            db.execute("INSERT INTO history_backfill_state(network,pool_address,attempts,done,last_error) VALUES(?,?,?,?,?)",("eth","0xabc",8,1,"original error"))
            db.commit();db.close();db=hr.db_connect(path)
            try:
                row=db.execute("SELECT attempts,done,last_error FROM history_backfill_state").fetchone()
                self.assertEqual(tuple(row),(8,1,"original error"))
                self.assertEqual(hr.status(db)["suspect_completed_pools"],1)
            finally:db.close()
    def test_retry_event_includes_pool_and_error_type(self):
        with patch.object(self.worker,"call",side_effect=TimeoutError("test")),redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(TimeoutError):self.worker.backfill()
        event=json.loads(out.getvalue())
        self.assertEqual((event["network"],event["pool"],event["error_type"]),("eth","0xabc","TimeoutError"))
        self.assertFalse(event["done"])


class SharedQuotaIntegrationTests(unittest.TestCase):
    def test_history_worker_uses_history_quota_role(self):
        class Q:
            def __init__(self):self.roles=[]
            def acquire(self,role):self.roles.append(role)
            def penalize(self,_):pass
        with tempfile.TemporaryDirectory() as td:
            db=hr.db_connect(Path(td)/"x.db");q=Q();w=hr.Worker(db,quota=q)
            with patch.object(hr,"gt_json",return_value={"data":[]}):self.assertEqual(w.call("/networks/new_pools"),{"data":[]})
            self.assertEqual(q.roles,["history"]);db.close()

if __name__=="__main__":unittest.main()
