"""Supervise the fast intelligence/decision threads inside one Railway process.

This reduces process/memory overhead while preserving independent loops for:
external events, cross-asset confirmation, major-coin microstructure, unified
decisions, and major long/short paper accounting. A dead component fails the hub
so launcher.py can fail the service instead of leaving a false-green deployment.
"""
from __future__ import annotations
import json, threading, time
import calibration_worker, cross_asset, decision_worker, external_events, gmgn_intel, major_microstructure, major_paper, prediction_market_shadow

VERSION="intelligence-hub-0.2.0"
COMPONENTS=(
    ("external_events",external_events.main),
    ("gmgn_intel",gmgn_intel.main),
    ("cross_asset",cross_asset.main),
    ("major_microstructure",major_microstructure.main),
    ("decision_worker",decision_worker.main),
    ("major_paper",major_paper.main),
    ("prediction_market_shadow",prediction_market_shadow.main),
    ("calibration_worker",calibration_worker.main),
)

def emit(event,**fields):
    print(json.dumps({"event":event,"version":VERSION,**fields},separators=(",",":"),allow_nan=False),flush=True)

def run(components=COMPONENTS,check_interval=.5):
    threads=[]
    def wrapped(name,target):
        try:target()
        except Exception as exc:emit("INTELLIGENCE_COMPONENT_ERROR",component=name,error_type=type(exc).__name__,error=str(exc)[:160])
    for name,target in components:
        t=threading.Thread(target=wrapped,args=(name,target),name=name,daemon=True);t.start();threads.append((name,t));emit("INTELLIGENCE_COMPONENT_STARTED",component=name)
    while True:
        for name,t in threads:
            if not t.is_alive():
                emit("INTELLIGENCE_COMPONENT_EXITED",component=name)
                return 1
        time.sleep(check_interval)

def main():return run()
if __name__=="__main__":raise SystemExit(main())
