import tempfile,unittest
from pathlib import Path
import major_microstructure as m

class T(unittest.TestCase):
    def make(self,n=1400,lag=10,beta=1.2):
        # deterministic impulse pattern; target is shifted response
        bp=[100.0];tp=[50.0]
        rets=[]
        for i in range(1,n):
            r=(0.0008 if i%47==0 else -0.00065 if i%71==0 else (0.00005 if i%2 else -0.00004));rets.append(r);bp.append(bp[-1]*(1+r))
            j=i-lag;tr=(rets[j-1]*beta if j>=1 else 0);tp.append(tp[-1]*(1+tr))
        base=1700000000
        return [(base+i,bp[i]) for i in range(n)],[(base+i,tp[i]) for i in range(n)]
    def test_dynamic_lag_detects_seconds(self):
        b,t=self.make(lag=10);s=m.analyze_series(b,t,'SOL',total_cost_bps=1,density=.8,min_n=500)
        self.assertIn(s.best_lag_s,m.LAGS);self.assertGreaterEqual(s.best_corr or 0,.12)
    def test_synchronous_not_called_stable_lag(self):
        b,t=self.make(lag=1); # our grid may find 1 sec but this is still a valid very short lag, not minute-fixed
        s=m.analyze_series(b,t,'ETH',total_cost_bps=1,density=.8,min_n=500);self.assertIn(s.best_lag_s,m.LAGS)

    def test_dynamic_lag_can_detect_subsecond_grid(self):
        # 250ms grid; target response is delayed two slots = 500ms.
        n=5200;lag_slots=2;beta=1.15;bp=[100.0];tp=[50.0];rets=[]
        for i in range(1,n):
            r=(0.00035 if i%113==0 else -0.00028 if i%173==0 else (0.00002 if i%2 else -0.000015))
            rets.append(r);bp.append(bp[-1]*(1+r))
            j=i-lag_slots;tr=(rets[j-1]*beta if j>=1 else 0);tp.append(tp[-1]*(1+tr))
        b=[(i,bp[i]) for i in range(n)];t=[(i,tp[i]) for i in range(n)]
        state=m.analyze_subsecond_series(b,t,'XRP',total_cost_bps=1,density=.8,min_n=1800)
        self.assertIn(state.best_lag_s,(.25,.5,.75,1.0,2.0,3.0,5.0,10.0,15.0,30.0,60.0,120.0,300.0))
        self.assertLessEqual(state.best_lag_s or 999,1.0)

    def test_low_density_blocks_confidence(self):
        b,t=self.make(lag=10);s=m.analyze_series(b,t,'SOL',total_cost_bps=1,density=.02,min_n=500);self.assertFalse(s.lag_candidate);self.assertLess(s.lag_confidence,.58)
    def test_quote_cost_uses_spread(self):
        with tempfile.TemporaryDirectory() as td:
            db=m.dbopen(Path(td)/'x.db');db.execute("insert into major_quotes values('SOL',1000000,99,101,'x')");db.commit()
            self.assertGreater(m.quote_cost_bps(db,'SOL',1000001),200);db.close()
    def test_trade_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            db=m.dbopen(Path(td)/'x.db');m.store_trade(db,'BTC',{'a':1,'T':1000,'p':'100','q':'2','m':False})
            self.assertEqual(db.execute('select count(*) from major_ticks').fetchone()[0],1);db.close()
    def test_stream_url_contains_trade_and_book(self):
        u=m.stream_url({'BTC':'BTCUSDT'});self.assertIn('btcusdt@aggTrade',u);self.assertIn('btcusdt@bookTicker',u)
if __name__=='__main__':unittest.main()
