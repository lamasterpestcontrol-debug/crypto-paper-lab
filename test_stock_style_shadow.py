import tempfile,time,unittest
from pathlib import Path
import stock_style_shadow as s
import market_regime as m
import live_ohlcv as l

class T(unittest.TestCase):
    def seed(self,db,n=25,trend=.01):
        db.execute("""CREATE TABLE strategy_ab_observations(
          id INTEGER PRIMARY KEY,ts REAL,chain TEXT,address TEXT,symbol TEXT,price REAL,volume_h1 REAL)""")
        base=time.time()-n*60
        for i in range(n):
            p=1+i*trend; db.execute("INSERT INTO strategy_ab_observations(ts,chain,address,symbol,price,volume_h1) VALUES(?,?,?,?,?,?)",
                (base+i*60,"solana","abc","ABC",p,1000+i*100))
        db.commit()
    def test_insufficient(self):
        with tempfile.TemporaryDirectory() as td:
            db=s.dbopen(Path(td)/"x.sqlite3");self.seed(db,10)
            r=s.evaluate(db,"solana","abc","ABC",time.time());self.assertFalse(r["entry_ready"]);self.assertIn("INSUFFICIENT",r["reason"]);db.close()
    def test_cycle_writes_regime_and_variant(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"x.sqlite3";db=s.dbopen(path);self.seed(db,25)
            mdb=m.dbopen(path); rr={x:{"5m":.01,"15m":.01,"60m":.02} for x in m.COINS}; m.save_state(mdb,time.time(),m.classify(rr));mdb.close()
            out=s.cycle(db);self.assertEqual(out["market_regime"],"RISK_ON")
            row=db.execute("SELECT bar_source,volume_source,market_regime,recommended_variant FROM strategy_v08_observations").fetchone()
            self.assertIn("PROXY",row[0]);self.assertIn("PROXY",row[1]);self.assertEqual(row[2],"RISK_ON");self.assertEqual(row[3],"aggressive");db.close()
    def test_true_ohlcv_preferred_when_available(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"x.sqlite3";db=s.dbopen(path);self.seed(db,25)
            ldb=l.dbopen(path);now=int(time.time())
            rows=[]
            for i in range(25):
                c=1+i*.01;rows.append((now-(24-i)*60,c-.005,c+.01,c-.01,c,1000+i*100))
            l.save_bars(ldb,"solana","abc","solana","pool",rows);ldb.close()
            r=s.evaluate(db,"solana","abc","ABC",time.time()+1)
            self.assertEqual(r["bar_source"],"GECKOTERMINAL_ONCHAIN_OHLCV_MINUTE")
            self.assertEqual(r["volume_source"],"GECKOTERMINAL_ONCHAIN_CANDLE_VOLUME");db.close()

    def test_missing_regime_defaults_balanced(self):
        with tempfile.TemporaryDirectory() as td:
            db=s.dbopen(Path(td)/"x.sqlite3");self.seed(db,25);s.cycle(db)
            row=db.execute("SELECT market_regime,recommended_variant FROM strategy_v08_observations").fetchone();self.assertEqual(tuple(row),("NEUTRAL","balanced"));db.close()
    def test_does_not_touch_positions(self):
        with tempfile.TemporaryDirectory() as td:
            db=s.dbopen(Path(td)/"x.sqlite3");self.seed(db,25);db.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY,status TEXT)");db.execute("INSERT INTO positions VALUES(1,'OPEN')");db.commit()
            s.cycle(db);self.assertEqual(db.execute("SELECT COUNT(*) FROM positions").fetchone()[0],1);db.close()

if __name__=='__main__':unittest.main()

class GMGNDiscoveryTests(unittest.TestCase):
    def test_recent_keys_includes_gmgn_early_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            db=s.dbopen(Path(td)/"g.db")
            db.execute("CREATE TABLE strategy_ab_observations(id INTEGER PRIMARY KEY,ts REAL,chain TEXT,address TEXT,symbol TEXT,price REAL,volume_h1 REAL)")
            db.execute("CREATE TABLE gmgn_discovery_candidates(chain TEXT,address TEXT,symbol TEXT,priority REAL,last_seen REAL)")
            now=time.time();db.execute("INSERT INTO gmgn_discovery_candidates VALUES(?,?,?,?,?)",("solana","EARLYGMGN","EG",99,now));db.commit()
            rows=s.recent_keys(db,now,10);self.assertTrue(any(r["address"]=="EARLYGMGN" for r in rows));db.close()
