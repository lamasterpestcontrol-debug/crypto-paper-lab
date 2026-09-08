"""Supervise all paper-only workers; a dead worker must not leave a green service."""
from __future__ import annotations
import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

ROOT=Path(__file__).resolve().parent
COMMANDS=(("discovery.py",), ("history_replay.py","--loop"), ("shadow_ab.py",))

def emit(event: str, **fields) -> None:
    print(json.dumps({"event":event, **fields},separators=(",",":")),flush=True)

def stop_workers(workers: list[tuple[str,subprocess.Popen]], grace_seconds: float=15) -> None:
    # Signal every worker first; share one grace period rather than waiting 15s each.
    deadline=time.monotonic()+grace_seconds
    for _,worker in workers:
        if worker.poll() is None:
            try: worker.terminate()
            except ProcessLookupError: pass
    for name,worker in workers:
        try:
            worker.wait(timeout=max(0.05,deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            emit("WORKER_FORCED_STOP",worker=name)
            try: worker.kill()
            except ProcessLookupError: pass
            worker.wait()

def supervise(stop: threading.Event, commands: Sequence[Sequence[str]]=COMMANDS) -> int:
    workers=[]
    try:
        for command in commands:
            worker=subprocess.Popen([sys.executable,"-u",*command],cwd=ROOT)
            workers.append((command[0],worker))
            emit("WORKER_STARTED",worker=command[0],pid=worker.pid)
        while not stop.is_set():
            for name,worker in workers:
                code=worker.poll()
                if code is not None:
                    # Even exit 0 is unexpected for a continuously running worker.
                    emit("WORKER_EXITED",worker=name,exit_code=code)
                    return 1
            stop.wait(0.5)
        return 0
    except Exception as exc:
        emit("SUPERVISOR_ERROR",error_type=type(exc).__name__,error=str(exc)[:160])
        return 1
    finally:
        stop_workers(workers)

def main() -> int:
    stop=threading.Event()
    def shutdown(signum, frame):
        emit("SUPERVISOR_SHUTDOWN",signal=signum)
        stop.set()
    for signum in (signal.SIGTERM,signal.SIGINT):
        signal.signal(signum,shutdown)
    return supervise(stop)

if __name__=="__main__":
    raise SystemExit(main())
