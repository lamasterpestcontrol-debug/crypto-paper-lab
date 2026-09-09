import json, math, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import market_regime as m


def ret(v1=0.0,v5=0.0,v15=0.0,v60=0.0):
    return {s:{"1m":v1,"5m":v5,"15m":v15,"60m":v60} for s in m.COINS}

class T(unittest.TestCase):
    def test_parse_binance_us_kline_and_completion(self):
        rows=[[60_000,"1","2","0.5","1.5","99",119_999,"x","x","x","x","0"]]
        out=m.parse_klines(rows,120_000)
        self.assertEqual(out,[(60,1.0,2.0,.5,1.5,99.0,1)])

    def test_bad_ohlc_rejected(self):
        with self.assertRaises(ValueError):m.parse_klines([[60_000,"3","2","0.5","1.5","99",119_999]],120_000)

    def test_risk_on_selects_aggressive_when_chain_leader_confirms(self):
        x=m.classify(ret(.001,.01,.01,.02));self.assertEqual(x.regime,"RISK_ON");self.assertEqual(m.variant_for_chain(x,"solana"),"aggressive")

    def test_chain_leader_divergence_downgrades_risk_on(self):
        r=ret(.001,.01,.01,.02);r["SOL"]["15m"]=-.01;x=m.classify(r)
        self.assertEqual(x.regime,"RISK_ON");self.assertEqual(m.variant_for_chain(x,"solana"),"balanced")

    def test_risk_off_is_conservative(self):
        x=m.classify(ret(-.001,-.01,-.01,-.02));self.assertEqual(x.regime,"RISK_OFF");self.assertEqual(m.variant_for_chain(x,"base"),"conservative")

    def test_shock_halts_new_entries(self):
        x=m.classify(ret(-.02,-.04,-.04,-.04));self.assertEqual(x.regime,"SHOCK");self.assertEqual(m.variant_for_chain(x,"bsc"),"halt")

    def test_incomplete_history_is_neutral_not_fabricated(self):
        r=ret(.001,.01,.01,.02);r["BNB"]["60m"]=None;x=m.classify(r)
        self.assertEqual(x.regime,"NEUTRAL");self.assertLess(x.confidence,.5)

    def test_completed_bars_only_drive_returns(self):
        with tempfile.TemporaryDirectory() as td:
            db=m.dbopen(Path(td)/"x.sqlite3")
            for sym in m.COINS:
                m.save_symbol_bars(db,sym,[(1000,1,1,1,1,1,1),(1060,1,1.1,1,1.1,1,1),(1120,1.1,2,1.1,2,1,0)],1200)
            rr=m.returns_from_db(db,1200)
            self.assertAlmostEqual(rr["BTC"]["1m"],.1)
            db.close()

    def test_stale_regime_is_not_reused(self):
        with tempfile.TemporaryDirectory() as td:
            db=m.dbopen(Path(td)/"x.sqlite3");x=m.classify(ret())
            m.save_state(db,1000,x,{})
            self.assertIsNone(m.latest_result_from_db(db,now=1200,max_age=180))
            self.assertIsNotNone(m.latest_result_from_db(db,now=1100,max_age=180));db.close()

    def test_cycle_observation_only_without_network(self):
        with tempfile.TemporaryDirectory() as td:
            db=m.dbopen(Path(td)/"x.sqlite3");db.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY,status TEXT)");db.execute("INSERT INTO positions VALUES(1,'OPEN')")
            # seed enough complete bars to keep cycle entirely local
            for sym in m.COINS:
                p=100.0;rows=[]
                for i in range(80):
                    rows.append((1000+i*60,p,p,p,p,1,1));p*=1.0001
                m.save_symbol_bars(db,sym,rows,6000)
            m.cycle(db,now=6000,collector=False)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM positions").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM market_regime_states").fetchone()[0],1);db.close()

    def test_delayed_relationship_detects_two_minute_lag(self):
        t0=1_700_000_000//60*60;rs=[math.sin(i*.37)*.002+math.cos(i*.11)*.0012 for i in range(220)]
        bp=100.;ap=50.;btc=[];alt=[]
        for i,r in enumerate(rs):
            bp*=1+r;btc.append((t0+i*60,bp));ar=.8*rs[i-2] if i>=2 else 0;ap*=1+ar;alt.append((t0+i*60,ap))
        out=m.lead_lag_from_series(btc,alt,"ETH");self.assertEqual(out.best_lag_min,2);self.assertTrue(out.stable_lag);self.assertGreater(out.best_corr,.9)

    def test_synchronous_relationship_not_called_delayed(self):
        t0=1_700_000_000//60*60;rs=[math.sin(i*.29)*.002+math.cos(i*.13)*.0007 for i in range(200)]
        bp=ap=100.;btc=[];alt=[]
        for i,r in enumerate(rs):
            bp*=1+r;ap*=1+.9*r;btc.append((t0+i*60,bp));alt.append((t0+i*60,ap))
        out=m.lead_lag_from_series(btc,alt,"SOL");self.assertEqual(out.best_lag_min,0);self.assertFalse(out.stable_lag)

    def test_lead_lag_insufficient_history_fails_closed(self):
        s=[(1_700_000_000+i*60,100+i) for i in range(20)];out=m.lead_lag_from_series(s,s,"XRP")
        self.assertFalse(out.lag_candidate);self.assertEqual(out.reason,"INSUFFICIENT_HISTORY")

    def test_fetch_rejects_unsupported_symbol(self):
        with self.assertRaises(ValueError):m.fetch_symbol_klines("DOGEUSDT",1,120000)

    def test_major_pair_falls_back_to_usd_when_usdt_unavailable(self):
        good=[[60_000,"1","2","0.5","1.5","99",119_999]]
        def fake(symbol,limit,now_ms):
            if symbol=="XRPUSDT": raise ValueError("not listed")
            return m.parse_klines(good,now_ms)
        with patch.object(m,"fetch_symbol_klines",side_effect=fake):
            venue,rows=m.fetch_major_klines("XRP",2,120_000)
        self.assertEqual(venue,"XRPUSD");self.assertEqual(rows[0][4],1.5)

if __name__=='__main__':unittest.main()

class UnknownChainSafetyTests(unittest.TestCase):
    def test_unknown_chain_never_becomes_aggressive_without_leader(self):
        returns={c:{"5m":.01,"15m":.01,"60m":.02} for c in m.COINS}
        r=m.RegimeResult("RISK_ON",None,.8,5,5,5,.01,.01,.01,.02,returns)
        self.assertEqual(m.variant_for_chain(r,"robinhood"),"balanced")
        self.assertEqual(m.variant_for_chain(r,"unknownchain"),"balanced")
