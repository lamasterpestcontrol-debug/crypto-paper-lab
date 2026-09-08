import io
import subprocess
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch,Mock
import launcher

class LauncherTests(unittest.TestCase):
    def worker(self, code=None):
        p=Mock();p.poll.return_value=code;p.pid=123;p.wait.return_value=0
        return p
    def test_dead_worker_fails_whole_service(self):
        workers=[self.worker(),self.worker(1),self.worker()]
        with patch.object(launcher.subprocess,"Popen",side_effect=workers),redirect_stdout(io.StringIO()) as log:
            self.assertEqual(launcher.supervise(threading.Event()),1)
        self.assertIn('WORKER_EXITED',log.getvalue())
        workers[0].terminate.assert_called_once();workers[2].terminate.assert_called_once()
    def test_unexpected_clean_exit_also_fails(self):
        workers=[self.worker(),self.worker(),self.worker(0)]
        with patch.object(launcher.subprocess,"Popen",side_effect=workers),redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.supervise(threading.Event()),1)
    def test_shutdown_stops_all_workers(self):
        workers=[self.worker() for _ in range(3)]
        stop=threading.Event();stop.set()
        with patch.object(launcher.subprocess,"Popen",side_effect=workers),redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.supervise(stop),0)
        for p in workers:p.terminate.assert_called_once();p.wait.assert_called_once()
    def test_partial_spawn_failure_cleans_up(self):
        worker=self.worker()
        with patch.object(launcher.subprocess,"Popen",side_effect=[worker,OSError("spawn failed")]),redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.supervise(threading.Event()),1)
        worker.terminate.assert_called_once()
    def test_force_kill_is_reaped(self):
        worker=self.worker();worker.wait.side_effect=[subprocess.TimeoutExpired("test",0.1),0]
        with redirect_stdout(io.StringIO()):launcher.stop_workers([("worker",worker)],0.1)
        worker.kill.assert_called_once();self.assertEqual(worker.wait.call_count,2)

if __name__=="__main__":unittest.main()
