"""Supervise all paper-only workers; a dead worker must not leave a green service."""
from __future__ import annotations
import json,signal,subprocess,sys,threading,time
from pathlib import Path
from typing import Sequence
ROOT=Path(__file__).resolve().parent
COMMANDS=(("discovery.py",), ("robinhood_watch.py",), ("history_replay.py","--loop"), ("shadow_ab.py",), ("market_regime.py",), ("live_ohlcv.py",), ("stock_style_shadow.py",), ("v08_paper.py",), ("intelligence_hub.py",))
def emit(event: str, **fields): print(json.dumps({"event":event,**fields},separators=(",",":")),flush=True)
def stop_workers(workers,grace_seconds=15):
    deadline=time.monotonic()+grace_seconds
    for _,w in workers:
        if w.poll() is None:
            try:w.terminate()
            except ProcessLookupError:pass
    for name,w in workers:
        try:w.wait(timeout=max(.05,deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            emit("WORKER_FORCED_STOP",worker=name)
            try:w.kill()
            except ProcessLookupError:pass
            w.wait()
def supervise(stop:threading.Event,commands:Sequence[Sequence[str]]=COMMANDS):
    workers=[]
    try:
        for command in commands:
            w=subprocess.Popen([sys.executable,"-u",*command],cwd=ROOT);workers.append((command[0],w));emit("WORKER_STARTED",worker=command[0],pid=w.pid)
        while not stop.is_set():
            for name,w in workers:
                code=w.poll()
                if code is not None:emit("WORKER_EXITED",worker=name,exit_code=code);return 1
            stop.wait(.5)
        return 0
    except Exception as exc:emit("SUPERVISOR_ERROR",error_type=type(exc).__name__,error=str(exc)[:160]);return 1
    finally:stop_workers(workers)
def main():
    stop=threading.Event()
    def shutdown(signum,frame):emit("SUPERVISOR_SHUTDOWN",signal=signum);stop.set()
    for signum in (signal.SIGTERM,signal.SIGINT):signal.signal(signum,shutdown)
    return supervise(stop)
if __name__=="__main__":raise SystemExit(main())
