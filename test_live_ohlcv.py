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

class SharedQuotaTests(unittest.TestCase):
    class Clock:
        def __init__(self,now=1000.0):self.now=float(now)
        def __call__(self):return self.now
        def sleep(self,seconds):self.now+=float(seconds)
    def test_realtime_priority_makes_history_yield(self):
        with tempfile.TemporaryDirectory() as td:
            c=self.Clock();q1=l.SharedGTQuota(Path(td),min_interval=5,realtime_hold=12,clock=c,sleeper=c.sleep);q2=l.SharedGTQuota(Path(td),min_interval=5,realtime_hold=12,clock=c,sleeper=c.sleep)
            q1.acquire('realtime');self.assertEqual(c.now,1000.0)
            q2.acquire('history');self.assertGreaterEqual(c.now,1012.0)
            state=q2.snapshot();self.assertEqual(state['last_role'],'history');self.assertGreaterEqual(state['next_allowed'],1017.0)
    def test_429_penalty_is_shared_between_instances(self):
        with tempfile.TemporaryDirectory() as td:
            c=self.Clock();q1=l.SharedGTQuota(Path(td),min_interval=5,realtime_hold=0,clock=c,sleeper=c.sleep);q2=l.SharedGTQuota(Path(td),min_interval=5,realtime_hold=0,clock=c,sleeper=c.sleep)
            q1.acquire('realtime');q1.penalize(30);q2.acquire('realtime')
            self.assertGreaterEqual(c.now,1030.0)

    def test_overdue_history_ignores_realtime_hold_and_gets_slot(self):
        with tempfile.TemporaryDirectory() as td:
            c=self.Clock();q=l.SharedGTQuota(Path(td),min_interval=5,realtime_hold=60,history_max_wait=10,reservation_seconds=5,clock=c,sleeper=c.sleep)
            def seed(state,now):
                state["next_allowed"]=now
                state["realtime_until"]=now+60
                state["history_wait_since"]=now-11
            q._locked(seed)
            q.acquire("history")
            self.assertEqual(c.now,1000.0)
            state=q.snapshot();self.assertEqual(state["last_role"],"history");self.assertEqual(state["history_wait_since"],0.0)

    def test_cycle_exposes_http_status_for_rate_limit_diagnostics(self):
        import io,json,urllib.error
        from contextlib import redirect_stdout
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            db=l.dbopen(Path(td)/"x.db");w=l.Worker(db)
            err=urllib.error.HTTPError("https://example.test",429,"Too Many Requests",{},None)
            with patch.object(l,"select_candidates",return_value=[{"chain":"solana","address":"x"}]),patch.object(w,"collect_one",side_effect=err),redirect_stdout(io.StringIO()) as out:
                w.cycle()
            event=json.loads(out.getvalue())
            self.assertEqual(event["sample"][0]["http_code"],429);db.close()
    def test_worker_uses_realtime_shared_quota(self):
        class Q:
            def __init__(self):self.roles=[]
            def acquire(self,role):self.roles.append(role)
            def penalize(self,_):pass
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            db=l.dbopen(Path(td)/'x.db');q=Q();w=l.Worker(db,quota=q)
            with patch.object(l,'gt_json',return_value={'data':[]}):self.assertEqual(w.call('/networks/x/pools'),{'data':[]})
            self.assertEqual(q.roles,['realtime']);db.close()
