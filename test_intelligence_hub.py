import io,threading,unittest
from contextlib import redirect_stdout
from unittest.mock import patch
import intelligence_hub as h

class T(unittest.TestCase):
    def test_expected_components_present(self):
        self.assertEqual([x[0] for x in h.COMPONENTS],["external_events","gmgn_intel","cross_asset","major_microstructure","decision_worker","major_paper","calibration_worker"])
    def test_dead_component_fails_hub(self):
        gate=threading.Event()
        def alive():gate.wait(2)
        def dead():return
        comps=(("alive",alive),("dead",dead))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(h.run(comps,check_interval=.01),1)
        gate.set()
    def test_component_exception_becomes_dead_thread(self):
        def bad():raise RuntimeError("synthetic")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(h.run((("bad",bad),),check_interval=.01),1)
if __name__=='__main__':unittest.main()
