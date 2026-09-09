import tempfile,time,unittest
from pathlib import Path
import prediction_market_shadow as p

class T(unittest.TestCase):
    def test_market_filter_and_ambiguous_updown(self):
        m={"id":"1","conditionId":"c","question":"Bitcoin Up or Down - 5 Minutes","outcomes":"[\"Yes\",\"No\"]","clobTokenIds":"[\"y\",\"n\"]","endDate":"2030-01-01T00:00:00Z","liquidity":"1000"}
        x=p.normalize_market(m,now=1_700_000_000)
        self.assertIsNotNone(x);self.assertIsNone(x["strike"]);self.assertEqual(x["question_kind"],"UNRESOLVED_REFERENCE_PRICE")
    def test_fixed_strike_market(self):
        m={"id":"2","conditionId":"c2","question":"Will BTC be above $80,000 in 15 minutes?","outcomes":"[\"Yes\",\"No\"]","clobTokenIds":"[\"y2\",\"n2\"]","endDate":"2030-01-01T00:00:00Z"}
        x=p.normalize_market(m,now=1_700_000_000);self.assertEqual(x["strike"],80000);self.assertEqual(x["yes_token"],"y2")
    def test_non_btc_rejected(self):
        m={"id":"3","question":"ETH above $8000 in 15 minutes?","outcomes":"[\"Yes\",\"No\"]","clobTokenIds":"[\"y\",\"n\"]"}
        self.assertIsNone(p.normalize_market(m,now=time.time()))
    def test_orderbook_uses_executable_best(self):
        def f(path,params):return {"bids":[{"price":"0.42","size":"100"},{"price":"0.41","size":"200"}],"asks":[{"price":"0.44","size":"50"},{"price":"0.45","size":"100"}]}
        b=p.poly_book("x",f);self.assertEqual(b["bid"],.42);self.assertEqual(b["ask"],.44);self.assertGreater(b["ask_depth"],0)
    def test_yes_no_complement_signal(self):
        yes={"ask":.31,"ask_depth":100};no={"ask":.61,"ask_depth":100}
        s=p.evaluate(yes,no,None,min_comp_cents=1)
        self.assertEqual(len(s),1);self.assertEqual(s[0].signal_type,"YES_NO_COMPLEMENT");self.assertAlmostEqual(s[0].edge_cents,8)
    def test_no_complement_if_cost_over_one(self):
        self.assertEqual(p.evaluate({"ask":.53,"ask_depth":10},{"ask":.49,"ask_depth":10},None),[])
    def test_model_uses_ask_not_mid(self):
        s=p.evaluate({"ask":.55,"ask_depth":10},{"ask":.48,"ask_depth":10},.60,min_model_cents=2)
        self.assertTrue(any(x.signal_type=="MODEL_DISLOCATION" and x.direction=="YES" for x in s))
    def test_fair_prob_direction(self):
        a=p.fair_yes_probability(81000,80000,300,.002,{"premium":0,"book_imbalance":0})
        b=p.fair_yes_probability(79000,80000,300,.002,{"premium":0,"book_imbalance":0})
        self.assertGreater(a,.5);self.assertLess(b,.5)
    def test_hyper_public_parse(self):
        def f(body):
            if body["type"]=="metaAndAssetCtxs":return [{"universe":[{"name":"ETH"},{"name":"BTC"}]},[{"midPx":"1"},{"midPx":"80000","funding":"0.0001","premium":"0.0002","openInterest":"100"}]]
            return {"levels":[[{"px":"79999","sz":"2"}],[{"px":"80001","sz":"1"}]]}
        x=p.hyper_btc(f);self.assertEqual(x["mid"],80000);self.assertGreater(x["book_imbalance"],0)
    def test_cycle_read_only_writes_shadow(self):
        with tempfile.TemporaryDirectory() as td:
            db=p.dbopen(Path(td)/"x.db")
            # local BTC ticks with enough 1m samples for realized-vol model
            now=2_000_000_000.0
            db.executescript("CREATE TABLE major_ticks(symbol TEXT,trade_id INTEGER,ts_ms INTEGER,price REAL,qty REAL,buyer_maker INTEGER,source TEXT);")
            rows=[]
            for i in range(70):rows.append(("BTC",i,int((now-(69-i)*60)*1000),80000+i*2,1,0,"TEST"))
            db.executemany("INSERT INTO major_ticks VALUES(?,?,?,?,?,?,?)",rows);db.commit()
            def mf(path,params):return [{"id":"m","conditionId":"c","question":"Will BTC be above $80,000 in 15 minutes?","outcomes":"[\"Yes\",\"No\"]","clobTokenIds":"[\"y\",\"n\"]","endDate":"2033-05-18T03:33:20Z","liquidity":"10000"}]
            def bf(path,params):
                return {"bids":[{"price":"0.40","size":"100"}],"asks":[{"price":"0.45" if params["token_id"]=="y" else "0.50","size":"100"}]}
            def hf(body):
                if body["type"]=="metaAndAssetCtxs":return [{"universe":[{"name":"BTC"}]},[{"midPx":"80138","funding":"0","premium":"0","openInterest":"1000"}]]
                return {"levels":[[{"px":"80137","sz":"2"}],[{"px":"80139","sz":"2"}]]}
            out=p.cycle(db,now=now,market_fetch=mf,book_fetch=bf,hyper_fetch=hf,force_refresh=True)
            self.assertEqual(out["observed"],1);self.assertTrue(out["btc_available"]);self.assertTrue(out["hyper_available"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM prediction_snapshots").fetchone()[0],1)
            self.assertGreaterEqual(db.execute("SELECT COUNT(*) FROM prediction_signals").fetchone()[0],1)
            # The module never creates order/trade tables or wallet configuration.
            names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("orders",names);self.assertNotIn("wallets",names)
            db.close()

if __name__=='__main__':unittest.main()
