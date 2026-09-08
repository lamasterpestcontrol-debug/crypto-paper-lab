import unittest, time
from strategy_v03 import *

class T(unittest.TestCase):
    def test_vbp(self):
        prev=MarketSnapshot(time.time()-60,1,50000,10000,100000,5,5,1)
        cur=MarketSnapshot(time.time(),1.02,50000,25000,120000,12,5,2)
        ok,strength=volume_before_price(cur,prev)
        self.assertTrue(ok)
        self.assertGreater(strength,0)

    def test_exit_first(self):
        ok,max_safe=exit_first_ok(10000,1000)
        self.assertFalse(ok)
        self.assertGreater(max_safe,0)

    def test_stale(self):
        now=time.time()
        cur=MarketSnapshot(now,1,100000,10000,100000,10,5,1,social_score=.8,social_ts=now-4000)
        d=build_decision(cur,None,100,1,1.2,.8,now)
        self.assertEqual(d.reason,"STALE_DATA")

    def test_social_noise(self):
        now=time.time()
        cur=MarketSnapshot(now,1,100000,10000,100000,5,10,1,social_score=.9,social_ts=now)
        d=build_decision(cur,None,100,1,1.2,.8,now)
        self.assertIn(d.state,("WATCH","REJECT"))
        self.assertIn("SOCIAL", d.reason)

    def test_recheck_entry(self):
        now=time.time()
        prev=MarketSnapshot(now-60,1,100000,10000,100000,5,5,1)
        cur=MarketSnapshot(now,0.9,100000,22000,100000,20,5,2)
        d=build_decision(cur,prev,100,1,1.2,.8,now)
        self.assertEqual(d.state,"ENTER")

if __name__=="__main__":
    unittest.main()
