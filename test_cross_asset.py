import tempfile,time,unittest
from pathlib import Path
import cross_asset as c

class T(unittest.TestCase):
    def series(self,start=100,n=80,step=.001): return [(1700000000+i*60,start*(1+step*i)) for i in range(n)]
    def test_risk_on_assets_score_positive(self):
        s={k:self.series(step=.0005) for k in c.ASSETS};s['VIX']=self.series(step=-.0005);s['DXY']=self.series(step=-.0002);s['US10Y']=self.series(step=-.0002);s['OIL']=self.series(step=0)
        score,shock,q,_=c.classify(s);self.assertGreater(score,0);self.assertFalse(shock);self.assertEqual(q,1)
    def test_risk_off_score_negative(self):
        s={k:self.series(step=-.0007) for k in c.ASSETS};s['VIX']=self.series(step=.003);s['DXY']=self.series(step=.0004);s['US10Y']=self.series(step=.0005);s['OIL']=self.series(step=.001)
        score,_,_,_=c.classify(s);self.assertLess(score,0)
    def test_missing_data_degrades_quality(self):
        score,shock,q,_=c.classify({'SP500':self.series()});self.assertLess(q,.5);self.assertFalse(shock)
    def test_cycle_persists(self):
        with tempfile.TemporaryDirectory() as td:
            db=c.dbopen(Path(td)/'x.db')
            def f(t): return self.series()
            out=c.cycle(db,now=1700005000,fetcher=f);self.assertEqual(out['data_quality'],1)
            self.assertIsNotNone(c.latest_state(db,now=1700005001));db.close()
if __name__=='__main__':unittest.main()
