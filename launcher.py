"""Launch discovery, historical collector, and shadow A/B evaluator in one Railway service."""
from __future__ import annotations
import subprocess, sys

def _stop(p):
    if p and p.poll() is None:
        p.terminate()
        try: p.wait(timeout=15)
        except subprocess.TimeoutExpired: p.kill()

def main():
    hist=subprocess.Popen([sys.executable,"-u","history_replay.py","--loop"])
    ab=subprocess.Popen([sys.executable,"-u","shadow_ab.py"])
    try:
        import discovery
        discovery.main()
    finally:
        _stop(ab); _stop(hist)

if __name__=="__main__":
    main()
