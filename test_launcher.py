import io,threading,unittest
from contextlib import redirect_stdout
from unittest.mock import patch,Mock
import launcher
class T(unittest.TestCase):
    def worker(self,code=None):
        p=Mock();p.poll.return_value=code;p.pid=123;p.wait.return_value=0;return p
    def test_eight_workers_configured(self):
        self.assertEqual(len(launcher.COMMANDS),8)
        self.assertIn(("market_regime.py",),launcher.COMMANDS)
        self.assertIn(("live_ohlcv.py",),launcher.COMMANDS)
        self.assertIn(("v08_paper.py",),launcher.COMMANDS)
        self.assertEqual(launcher.COMMANDS[-1][0],"intelligence_hub.py")
    def test_dead_worker_fails(self):
        ws=[self.worker() for _ in launcher.COMMANDS];ws[1].poll.return_value=1
        with patch.object(launcher.subprocess,"Popen",side_effect=ws),redirect_stdout(io.StringIO()):self.assertEqual(launcher.supervise(threading.Event()),1)
        for i,w in enumerate(ws):
            if i!=1:w.terminate.assert_called_once()
    def test_shutdown(self):
        ws=[self.worker() for _ in launcher.COMMANDS];stop=threading.Event();stop.set()
        with patch.object(launcher.subprocess,"Popen",side_effect=ws),redirect_stdout(io.StringIO()):self.assertEqual(launcher.supervise(stop),0)
        for w in ws:w.terminate.assert_called_once()
if __name__=='__main__':unittest.main()
