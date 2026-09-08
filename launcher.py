"""Launch live discovery and low-priority historical collector in one Railway service."""
from __future__ import annotations
import os
import signal
import subprocess
import sys
import time

def main():
    hist = subprocess.Popen([sys.executable, "-u", "history_replay.py", "--loop"])
    try:
        import discovery
        discovery.main()
    finally:
        if hist.poll() is None:
            hist.terminate()
            try:
                hist.wait(timeout=15)
            except subprocess.TimeoutExpired:
                hist.kill()

if __name__ == "__main__":
    main()
