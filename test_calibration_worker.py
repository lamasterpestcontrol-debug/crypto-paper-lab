import tempfile,unittest
from pathlib import Path
import calibration_worker as c

class T(unittest.TestCase):
    def db(self,td):
        db=c.dbopen(Path(td)/"x.db")
        db.execute("""CREATE TABLE IF NOT EXISTS v08_paper_positions(
          id INTEGER PRIMARY KEY,variant TEXT,status TEXT,entry_regime TEXT,asset_scope TEXT DEFAULT 'MEME',net_pnl REAL,cash_outflow REAL,closed_at REAL)""")
        db.commit();return db
    def seed(self,db,regime,variant,rets,n=None,asset_scope="MEME"):
        values=list(rets);n=len(values) if n is None else n
        for i in range(n):
            r=values[i%len(values)];cost=100.0
            db.execute("INSERT INTO v08_paper_positions(variant,status,entry_regime,asset_scope,net_pnl,cash_outflow,closed_at) VALUES(?,?,?,?,?,?,?)",
                       (variant,"CLOSED",regime,asset_scope,r*cost,cost,1000+i))
        db.commit()
    def test_insufficient_sample_inactive(self):
        with tempfile.TemporaryDirectory() as td:
            db=self.db(td);self.seed(db,"NEUTRAL","conservative",[.02],5);self.seed(db,"NEUTRAL","balanced",[.03],5)
            self.assertFalse(c.recommend(db,"NEUTRAL")["active"]);db.close()
    def test_risk_on_can_select_safely_better_aggressive(self):
        with tempfile.TemporaryDirectory() as td:
            db=self.db(td)
            self.seed(db,"RISK_ON","conservative",[.010,.012,.008,.011],40)
            self.seed(db,"RISK_ON","balanced",[.014,.016,.012,.015],40)
            self.seed(db,"RISK_ON","aggressive",[.025,.028,.022,.027],40)
            r=c.recommend(db,"RISK_ON");self.assertTrue(r["active"]);self.assertEqual(r["recommended_variant"],"aggressive");db.close()
    def test_risk_off_never_recommends_aggressive(self):
        with tempfile.TemporaryDirectory() as td:
            db=self.db(td);self.seed(db,"RISK_OFF","conservative",[.02,.018,.022],40)
            self.seed(db,"RISK_OFF","aggressive",[.20],40)
            r=c.recommend(db,"RISK_OFF");self.assertEqual(r["recommended_variant"],"conservative");db.close()
    def test_negative_expectancy_does_not_activate(self):
        with tempfile.TemporaryDirectory() as td:
            db=self.db(td)
            for v in ("conservative","balanced"):
                self.seed(db,"NEUTRAL",v,[-.03,-.01,.005,-.02],40)
            self.assertFalse(c.recommend(db,"NEUTRAL")["active"]);db.close()
    def test_latest_active_respects_regime_allowlist(self):
        with tempfile.TemporaryDirectory() as td:
            db=self.db(td)
            db.execute("INSERT INTO strategy_calibration(ts,regime,recommended_variant,active,reason,min_trades,stats_json,worker_version) VALUES(?,?,?,?,?,?,?,?)",
                       (1000,"NEUTRAL","aggressive",1,"x",20,"{}",c.VERSION));db.commit()
            self.assertIsNone(c.latest_active(db,"NEUTRAL",1001));db.close()

    def test_scope_specific_calibration_does_not_mix_meme_and_utility(self):
        with tempfile.TemporaryDirectory() as td:
            db=self.db(td)
            for v in ("conservative","balanced"):
                self.seed(db,"NEUTRAL",v,[.03,.025,.02],40,asset_scope="MEME")
                self.seed(db,"NEUTRAL",v,[-.03,-.02,-.01],40,asset_scope="UTILITY_NEW_TOKEN")
            self.assertTrue(c.recommend(db,"NEUTRAL",asset_scope="MEME")["active"])
            self.assertFalse(c.recommend(db,"NEUTRAL",asset_scope="UTILITY_NEW_TOKEN")["active"]);db.close()

if __name__=='__main__':unittest.main()
