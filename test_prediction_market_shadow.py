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

    def test_current_updown_slug_and_labels(self):
        m={"id":"u1","conditionId":"cu1","question":"Bitcoin Up or Down - September 9, 8:15PM-8:30PM ET","slug":"btc-updown-15m-1788999300","description":"Resolves using the Chainlink BTC/USD TWAP data stream.","resolutionSource":"https://data.chain.link/streams/btc-usd-twap-60s-streams","outcomes":"[\"Up\",\"Down\"]","clobTokenIds":"[\"upTok\",\"dnTok\"]","endDate":"2030-01-01T00:00:00Z"}
        x=p.normalize_market(m,now=1_700_000_000)
        self.assertIsNotNone(x);self.assertEqual((x["yes_label"],x["no_label"]),("Up","Down"));self.assertEqual((x["yes_token"],x["no_token"]),("upTok","dnTok"))
        self.assertIsNone(x["strike"]);self.assertEqual(x["settlement_kind"],p.SETTLEMENT_CHAINLINK_TWAP)

    def test_public_search_event_flattens_market(self):
        def f(path,params):
            if path=="/public-search":
                return {"events":[{"title":"BTC Up or Down 15m","slug":"btc-updown-15m-123","description":"Chainlink TWAP rules","resolutionSource":"https://data.chain.link/streams/btc-usd-twap-60s-streams","markets":[{"id":"msearch","conditionId":"csearch","question":"Bitcoin Up or Down - September 9, 8:15PM-8:30PM ET","outcomes":"[\"Up\",\"Down\"]","clobTokenIds":"[\"u\",\"d\"]","endDate":"2030-01-01T00:00:00Z"}]}]}
            if path=="/markets":return []
            raise AssertionError(path)
        xs=p.discover_markets(f,now=1_700_000_000)
        self.assertEqual(len(xs),1);self.assertEqual(xs[0]["market_id"],"msearch");self.assertTrue(xs[0]["discovery_source"].startswith("GAMMA_PUBLIC_SEARCH"))

    def test_unknown_binary_labels_rejected(self):
        m={"id":"weird","question":"BTC 15m choice","slug":"btc-15m","outcomes":"[\"A\",\"B\"]","clobTokenIds":"[\"a\",\"b\"]"}
        self.assertIsNone(p.normalize_market(m,now=1_700_000_000))

    def test_binance_us_rest_fallback_same_venue(self):
        calls=[]
        def f(path,params):
            calls.append((path,params.get("symbol")))
            if path=="/ticker/price":return {"price":"81234.5"}
            if path=="/klines":return [[i*60000,"0","0","0",str(80000+i*3),"0"] for i in range(61)]
            raise AssertionError(path)
        with tempfile.TemporaryDirectory() as td:
            db=p.dbopen(Path(td)/"x.db")
            st=p.btc_market_state(db,now=2_000_000_000,rest_fetch=f,cache={})
            self.assertEqual(st["source"],p.SOURCE_BTC_REST);self.assertEqual(st["price"],81234.5);self.assertGreater(st["sigma_1m"],0)
            self.assertTrue(all(sym in {"BTCUSDT","BTCUSD"} for _,sym in calls if sym))
            db.close()

    def test_fresh_local_btc_wins_over_rest(self):
        def no_rest(*args,**kwargs):raise AssertionError("REST should not be needed")
        with tempfile.TemporaryDirectory() as td:
            db=p.dbopen(Path(td)/"x.db")
            db.execute("CREATE TABLE major_ticks(symbol TEXT,trade_id INTEGER,ts_ms INTEGER,price REAL,qty REAL,buyer_maker INTEGER,source TEXT)")
            now=2_000_000_000.0
            rows=[("BTC",i,int((now-(69-i)*60)*1000),80000+i*2,1,0,"TEST") for i in range(70)]
            db.executemany("INSERT INTO major_ticks VALUES(?,?,?,?,?,?,?)",rows);db.commit()
            st=p.btc_market_state(db,now=now,rest_fetch=no_rest,cache={})
            self.assertEqual(st["source"],p.SOURCE_BTC);self.assertEqual(st["price"],80138);self.assertLessEqual(st["age_s"],.001)
            db.close()

    def test_existing_v14_database_gets_additive_columns(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"x.db";raw=__import__("sqlite3").connect(path)
            raw.execute("CREATE TABLE prediction_markets(market_id TEXT PRIMARY KEY,condition_id TEXT,question TEXT NOT NULL,slug TEXT,end_ts REAL,yes_token TEXT NOT NULL,no_token TEXT NOT NULL,liquidity REAL,volume REAL,strike REAL,question_kind TEXT NOT NULL,last_seen REAL NOT NULL,source TEXT NOT NULL)")
            raw.execute("CREATE TABLE prediction_snapshots(id INTEGER PRIMARY KEY,ts REAL NOT NULL,market_id TEXT NOT NULL,question TEXT NOT NULL,yes_bid REAL,yes_ask REAL,no_bid REAL,no_ask REAL,yes_depth_usd REAL,no_depth_usd REAL,btc_spot REAL,hl_mid REAL,hl_funding REAL,hl_premium REAL,hl_open_interest REAL,hl_book_imbalance REAL,seconds_to_expiry REAL,model_yes REAL,model_edge_cents REAL,complement_cost REAL,complement_edge_cents REAL,raw_json TEXT NOT NULL)")
            raw.commit();raw.close();db=p.dbopen(path)
            mc={r[1] for r in db.execute("PRAGMA table_info(prediction_markets)")};sc={r[1] for r in db.execute("PRAGMA table_info(prediction_snapshots)")}
            self.assertTrue({"yes_label","no_label","settlement_kind","resolution_source","discovery_source"}<=mc);self.assertTrue({"btc_source","btc_age_s"}<=sc);db.close()

    def test_updown_cycle_complement_only_no_model_guess(self):
        with tempfile.TemporaryDirectory() as td:
            db=p.dbopen(Path(td)/"x.db");now=2_000_000_000.0
            def mf(path,params):
                if path=="/public-search":return {"events":[{"title":"BTC Up or Down 15m","slug":"btc-updown-15m-x","description":"Chainlink BTC/USD TWAP","resolutionSource":"https://data.chain.link/streams/btc-usd-twap-60s-streams","markets":[{"id":"um","question":"Bitcoin Up or Down - 8:15PM-8:30PM ET","outcomes":"[\"Up\",\"Down\"]","clobTokenIds":"[\"u\",\"d\"]","endDate":"2033-05-18T03:33:20Z"}]}]}
                return []
            def bf(path,params):return {"bids":[{"price":"0.40","size":"100"}],"asks":[{"price":"0.45" if params["token_id"]=="u" else "0.50","size":"100"}]}
            def hf(body):
                if body["type"]=="metaAndAssetCtxs":return [{"universe":[{"name":"BTC"}]},[{"midPx":"80000","funding":"0","premium":"0","openInterest":"100"}]]
                return {"levels":[[{"px":"79999","sz":"1"}],[{"px":"80001","sz":"1"}]]}
            def rf(path,params):
                if path=="/ticker/price":return {"price":"80000"}
                return [[i*60000,"0","0","0",str(80000+i),"0"] for i in range(61)]
            out=p.cycle(db,now=now,market_fetch=mf,book_fetch=bf,hyper_fetch=hf,btc_rest_fetch=rf,force_refresh=True,btc_cache={})
            self.assertEqual(out["observed"],1);self.assertGreaterEqual(out["signals"],1);self.assertEqual(out["unresolved_reference"],1)
            snap=db.execute("SELECT model_yes,raw_json FROM prediction_snapshots ORDER BY id DESC LIMIT 1").fetchone();self.assertIsNone(snap[0]);self.assertIn(p.SETTLEMENT_CHAINLINK_TWAP,snap[1])
            kinds=[r[0] for r in db.execute("SELECT signal_type FROM prediction_signals")];self.assertEqual(set(kinds),{"YES_NO_COMPLEMENT"});db.close()

    def test_model_signal_source_keeps_btc_provenance(self):
        self.assertNotEqual(p.SOURCE_BTC,p.SOURCE_BTC_REST)

if __name__=='__main__':unittest.main()
