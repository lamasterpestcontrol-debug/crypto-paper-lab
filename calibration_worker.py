"""Bounded walk-forward calibration for adaptive MEME and utility-token paper strategies.

The worker never edits strategy parameters and never touches real orders.  It only
chooses among the three fixed A/B variants after enough *cost-after* closed paper
trades exist in the same entry market regime.  Hard risk states in the Decision
Engine always override this recommendation.
"""
from __future__ import annotations
import json, math, os, sqlite3, statistics, time
from pathlib import Path

VERSION="calibration-worker-0.2.0"
MIN_TRADES=max(5,int(os.getenv("CALIBRATION_MIN_TRADES","20")))
MAX_TRADES=max(MIN_TRADES,int(os.getenv("CALIBRATION_MAX_TRADES","200")))
MIN_PF=max(1.0,float(os.getenv("CALIBRATION_MIN_PF","1.05")))
SWITCH_MARGIN=max(0.0,float(os.getenv("CALIBRATION_SWITCH_MARGIN","0.002")))
ONE_SIDED_Z=max(0.0,float(os.getenv("CALIBRATION_LOWER_BOUND_Z","1.28")))
POLL_SECONDS=max(30.0,float(os.getenv("CALIBRATION_POLL_SECONDS","300")))

ALLOWED={
    "RISK_OFF":("conservative",),
    "NEUTRAL":("conservative","balanced"),
    "RISK_ON":("conservative","balanced","aggressive"),
}
DEFAULT={"RISK_OFF":"conservative","NEUTRAL":"balanced","RISK_ON":"aggressive"}


def emit(event,**fields):
    print(json.dumps({"event":event,"version":VERSION,**fields},separators=(",",":"),allow_nan=False),flush=True)


def dbopen(path:Path):
    db=sqlite3.connect(path,timeout=30);db.row_factory=sqlite3.Row;db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS strategy_calibration(
      id INTEGER PRIMARY KEY,ts REAL NOT NULL,regime TEXT NOT NULL,asset_scope TEXT NOT NULL DEFAULT 'ALL',recommended_variant TEXT,
      active INTEGER NOT NULL,reason TEXT NOT NULL,min_trades INTEGER NOT NULL,
      stats_json TEXT NOT NULL,worker_version TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_strategy_calibration_regime_ts ON strategy_calibration(regime,ts);
    """)
    cols={r[1] for r in db.execute("PRAGMA table_info(strategy_calibration)")}
    if "asset_scope" not in cols:db.execute("ALTER TABLE strategy_calibration ADD COLUMN asset_scope TEXT NOT NULL DEFAULT 'ALL'")
    db.commit();return db


def _finite(x):
    try:
        v=float(x);return v if math.isfinite(v) else None
    except (TypeError,ValueError,OverflowError):return None


def variant_stats(db,regime,variant,limit=MAX_TRADES,asset_scope=None):
    """Return cost-after trade return statistics, newest fixed-variant closes first."""
    try:
        if asset_scope:
            rows=db.execute("""SELECT net_pnl,cash_outflow FROM v08_paper_positions
              WHERE status='CLOSED' AND variant=? AND entry_regime=? AND asset_scope=? AND net_pnl IS NOT NULL
              AND cash_outflow>0 ORDER BY closed_at DESC,id DESC LIMIT ?""",(variant,regime,asset_scope,int(limit))).fetchall()
        else:
            rows=db.execute("""SELECT net_pnl,cash_outflow FROM v08_paper_positions
              WHERE status='CLOSED' AND variant=? AND entry_regime=? AND net_pnl IS NOT NULL
              AND cash_outflow>0 ORDER BY closed_at DESC,id DESC LIMIT ?""",(variant,regime,int(limit))).fetchall()
    except sqlite3.OperationalError:
        rows=[]
    rets=[];pnls=[]
    for r in rows:
        pnl=_finite(r["net_pnl"]);cost=_finite(r["cash_outflow"])
        if pnl is None or cost is None or cost<=0:continue
        rets.append(pnl/cost);pnls.append(pnl)
    n=len(rets);mean=statistics.fmean(rets) if rets else None
    sd=statistics.stdev(rets) if n>=2 else None
    se=(sd/math.sqrt(n)) if sd is not None else None
    lower=(mean-ONE_SIDED_Z*se) if mean is not None and se is not None else None
    wins=sum(x for x in pnls if x>0);losses=-sum(x for x in pnls if x<0)
    pf=(wins/losses) if losses>0 else (999.0 if wins>0 else None)
    return {"n":n,"mean_return":mean,"stdev_return":sd,"lower_bound":lower,
            "profit_factor":pf,"net_pnl":sum(pnls) if pnls else 0.0}


def recommend(db,regime,asset_scope=None):
    allowed=ALLOWED.get(regime,())
    if not allowed:
        return {"active":False,"recommended_variant":None,"reason":"REGIME_NOT_CALIBRATABLE","stats":{}}
    stats={v:variant_stats(db,regime,v,asset_scope=asset_scope) for v in allowed}
    if any(stats[v]["n"]<MIN_TRADES for v in allowed):
        return {"active":False,"recommended_variant":None,"reason":"INSUFFICIENT_BALANCED_AB_SAMPLE","stats":stats}
    qualified=[v for v in allowed if stats[v]["mean_return"] is not None and stats[v]["mean_return"]>0
               and stats[v]["lower_bound"] is not None and stats[v]["lower_bound"]>0
               and stats[v]["profit_factor"] is not None and stats[v]["profit_factor"]>=MIN_PF]
    if not qualified:
        return {"active":False,"recommended_variant":None,"reason":"NO_VARIANT_CLEARS_COST_AFTER_CONFIDENCE_GATE","stats":stats}
    best=max(qualified,key=lambda v:(stats[v]["lower_bound"],stats[v]["mean_return"],stats[v]["profit_factor"]))
    default=DEFAULT[regime]
    if best!=default:
        d=stats.get(default)
        # Switching away from the regime default requires a meaningful lower-bound advantage.
        if d and d["lower_bound"] is not None and stats[best]["lower_bound"] < d["lower_bound"]+SWITCH_MARGIN:
            if default in qualified:
                best=default
            else:
                return {"active":False,"recommended_variant":None,"reason":"SWITCH_MARGIN_NOT_MET","stats":stats}
    return {"active":True,"recommended_variant":best,"reason":"COST_AFTER_WALK_FORWARD_GATE_PASSED","stats":stats}


def save(db,regime,result,now,asset_scope="ALL"):
    safe_stats={}
    for v,s in result["stats"].items():
        safe_stats[v]={k:(round(x,8) if isinstance(x,float) and math.isfinite(x) else x) for k,x in s.items()}
    with db:
        db.execute("""INSERT INTO strategy_calibration(ts,regime,asset_scope,recommended_variant,active,reason,min_trades,stats_json,worker_version)
          VALUES(?,?,?,?,?,?,?,?,?)""",(now,regime,asset_scope,result["recommended_variant"],int(result["active"]),result["reason"],MIN_TRADES,
          json.dumps(safe_stats,separators=(",",":"),allow_nan=False),VERSION))


def latest_active(db,regime,now=None,max_age=900.0,asset_scope=None):
    now=time.time() if now is None else float(now)
    try:
        if asset_scope:
            row=db.execute("SELECT * FROM strategy_calibration WHERE regime=? AND asset_scope=? ORDER BY ts DESC,id DESC LIMIT 1",(regime,asset_scope)).fetchone()
        else:
            row=db.execute("SELECT * FROM strategy_calibration WHERE regime=? ORDER BY ts DESC,id DESC LIMIT 1",(regime,)).fetchone()
    except sqlite3.OperationalError:return None
    if not row or now-float(row["ts"])>max_age or not bool(row["active"]):return None
    v=row["recommended_variant"]
    return str(v) if v in ALLOWED.get(regime,()) else None


def cycle(db,now=None):
    now=time.time() if now is None else float(now);out={}
    for scope in ("MEME","UTILITY_NEW_TOKEN"):
        out[scope]={}
        for regime in ("RISK_OFF","NEUTRAL","RISK_ON"):
            r=recommend(db,regime,asset_scope=scope);save(db,regime,r,now,scope)
            out[scope][regime]={"active":r["active"],"variant":r["recommended_variant"],"reason":r["reason"]}
    emit("CALIBRATION_CYCLE_OK",recommendations=out,paper_only=True,self_modifying=False,scope_specific=True)
    return out


def main():
    data=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"));data.mkdir(parents=True,exist_ok=True)
    db=dbopen(data/"discovery.sqlite3")
    try:
        while True:
            started=time.monotonic()
            try:cycle(db)
            except Exception as exc:emit("CALIBRATION_CYCLE_ERROR",error_type=type(exc).__name__,error=str(exc)[:180])
            time.sleep(max(5.0,POLL_SECONDS-(time.monotonic()-started)))
    finally:db.close()
if __name__=="__main__":main()
