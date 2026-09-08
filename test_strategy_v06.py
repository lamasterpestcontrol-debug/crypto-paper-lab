import unittest, time
from strategy_v06 import *

class T(unittest.TestCase):
    def test_persistence_wait(self):
        pts=[SignalPoint(time.time(),70,True,True,True) for _ in range(2)]
        r=persistence_check(pts,required=3)
        self.assertFalse(r.confirmed)
        self.assertEqual(r.streak,2)

    def test_persistence_confirm(self):
        pts=[SignalPoint(time.time(),70,True,True,True) for _ in range(3)]
        self.assertTrue(persistence_check(pts,3).confirmed)

    def test_metrics_costs(self):
        m=metrics([Trade(1,2,10,-4,2),Trade(1,2,-5,-8,1)])
        self.assertEqual(m["n"],2.0)
        self.assertLess(m["mean_net_pct"],3)

    def test_optimize(self):
        rows=list(range(100))
        def bt(data,p):
            edge=5 if p.persistence_ticks==3 else 1
            return [Trade(i,i+1,edge,-5,1) for i in range(40)]
        grid=[Params(persistence_ticks=2),Params(persistence_ticks=3)]
        b=optimize(rows,bt,grid)
        self.assertEqual(b.params.persistence_ticks,3)

    def test_walk_forward(self):
        rows=list(range(700))
        def bt(data,p):
            return [Trade(i,i+1,4 if p.min_score==65 else 1,-6,1) for i in range(min(50,len(data)))]
        folds=walk_forward(rows,bt,300,100,100,[Params(min_score=60),Params(min_score=65)])
        self.assertGreaterEqual(len(folds),3)
        self.assertGreater(walk_forward_summary(folds)["positive_folds_pct"],0)

if __name__=="__main__": unittest.main()
