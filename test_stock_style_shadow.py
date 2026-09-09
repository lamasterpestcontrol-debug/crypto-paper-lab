import tempfile,time,unittest
from pathlib import Path
import stock_style_shadow as s

class T(unittest.TestCase):
    def seed(self,db,n=25):
        db.execute("""CREATE TABLE strategy_ab_observations(
          id INTEGER PRIMARY KEY,ts REAL,chain TEXT,address TEXT,symbol TEXT,price REAL,volume_h1 REAL)""")
        base=time.time()-n*60
        for i in range(n):
            p=1+i*.01; db.execute("INSERT INTO strategy_ab_observations(ts,chain,address,symbol,price,volume_h1) VALUES(?,?,?,?,?,?)",
                (base+i*60,"solana","abc","ABC",p,1000+i*100))
        db.commit()
    def test_insufficient(self):
        with tempfile.TemporaryDirectory() as td:
            db=s.dbopen(Path(td)/"x.sqlite3");self.seed(db,10)
            r=s.evaluate(db,"solana","abc","ABC",time.time());self.assertFalse(r["entry_ready"]);self.assertIn("INSUFFICIENT",r["reason"])
    def test_cycle_writes_separate_table(self):
        with tempfile.TemporaryDirectory() as td:
            db=s.dbopen(Path(td)/"x.sqlite3");self.seed(db,25)
            out=s.cycle(db);self.assertEqual(out["observed"],1)
            row=db.execute("SELECT bar_source,volume_source FROM strategy_v08_observations").fetchone()
            self.assertIn("PROXY",row[0]);self.assertIn("PROXY",row[1])
    def test_does_not_touch_positions(self):
        with tempfile.TemporaryDirectory() as td:
            db=s.dbopen(Path(td)/"x.sqlite3");self.seed(db,25);db.execute("CREATE TABLE positions(id INTEGER PRIMARY KEY,status TEXT)");db.execute("INSERT INTO positions VALUES(1,'OPEN')");db.commit()
            s.cycle(db);self.assertEqual(db.execute("SELECT COUNT(*) FROM positions").fetchone()[0],1)

if __name__=='__main__':unittest.main()
