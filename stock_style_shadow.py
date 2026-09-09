"""Stock-style technical-analysis shadow worker for crypto-paper-lab.

Research-only. Reads the existing shadow observations and writes a separate v0.8
signal table. It never opens/closes positions and does not alter the control strategy.
Snapshot-derived bars are explicitly labelled as proxies, not true exchange candles.
"""
from __future__ import annotations
import json, os, sqlite3, time
from pathlib import Path
from strategy_v08 import Bar, analyze_early_crypto
VERSION="stock-style-shadow-0.1.0"


def emit(event, **fields):
    print(json.dumps({"event":event,"version":VERSION,**fields},separators=(",",":"),allow_nan=False),flush=True)


def dbopen(path:Path):
    db=sqlite3.connect(path,timeout=30); db.row_factory=sqlite3.Row; db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS strategy_v08_observations(
      id INTEGER PRIMARY KEY,ts REAL NOT NULL,chain TEXT NOT NULL,address TEXT NOT NULL,symbol TEXT,
      bar_count INTEGER NOT NULL,entry_ready INTEGER NOT NULL,reason TEXT NOT NULL,
      ema_fast REAL,ema_slow REAL,rsi14 REAL,atr_pct REAL,volume_ratio REAL,
      bar_source TEXT NOT NULL,volume_source TEXT NOT NULL,raw_json TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_v08_token_ts ON strategy_v08_observations(chain,address,ts)")
    db.commit(); return db


def recent_keys(db, now, limit=50):
    return db.execute("""SELECT chain,address,MAX(symbol) symbol,MAX(ts) last_ts
      FROM strategy_ab_observations WHERE ts>=? GROUP BY chain,address
      ORDER BY last_ts DESC LIMIT ?""",(now-7200,limit)).fetchall()


def bars_for(db, chain, address, now, limit=60):
    rows=db.execute("""SELECT ts,price,volume_h1 FROM strategy_ab_observations
      WHERE chain=? AND address=? AND ts<? AND price>0 ORDER BY ts DESC,id DESC LIMIT ?""",
      (chain,address,now,limit*3)).fetchall()
    unique={}
    for r in rows:
        unique.setdefault(float(r["ts"]),r)
    rows=[unique[k] for k in sorted(unique)][-limit:]
    bars=[]
    for r in rows:
        p=float(r["price"]); v=max(0.0,float(r["volume_h1"] or 0))
        bars.append(Bar(float(r["ts"]),p,p,p,p,v))
    return bars


def evaluate(db, chain, address, symbol, now):
    bars=bars_for(db,chain,address,now)
    base={"bar_count":len(bars),"bar_source":"MINUTE_SNAPSHOT_CLOSE_PROXY_NOT_TRUE_OHLC",
          "volume_source":"ROLLING_H1_VOLUME_PROXY_NOT_CANDLE_VOLUME"}
    if len(bars)<21:
        return {**base,"entry_ready":False,"reason":"INSUFFICIENT_21_MINUTE_HISTORY"}
    try:
        s=analyze_early_crypto(bars)
        return {**base,"entry_ready":bool(s.entry_ready),"reason":s.reason,
                "ema_fast":s.ema20,"ema_slow":s.ema50,"rsi14":s.rsi14,
                "atr_pct":s.atr_pct,"volume_ratio":s.volume_ratio,"breakout20":s.breakout20,
                "bullish_structure":s.bullish_structure,"overextended":s.overextended}
    except Exception as exc:
        return {**base,"entry_ready":False,"reason":"V08_ANALYSIS_ERROR","error_type":type(exc).__name__}


def cycle(db):
    now=time.time(); keys=recent_keys(db,now); ready=0; results=[]
    for k in keys:
        r=evaluate(db,k["chain"],k["address"],k["symbol"],now); ready+=int(r["entry_ready"]); results.append((k,r))
        raw=json.dumps(r,separators=(",",":"),allow_nan=False)
        with db:
            db.execute("""INSERT INTO strategy_v08_observations
              (ts,chain,address,symbol,bar_count,entry_ready,reason,ema_fast,ema_slow,rsi14,atr_pct,volume_ratio,bar_source,volume_source,raw_json)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (now,k["chain"],k["address"],k["symbol"],r["bar_count"],int(r["entry_ready"]),r["reason"],
               r.get("ema_fast"),r.get("ema_slow"),r.get("rsi14"),r.get("atr_pct"),r.get("volume_ratio"),
               r["bar_source"],r["volume_source"],raw))
    emit("STOCK_STYLE_SHADOW_CYCLE_OK",observed=len(results),entry_ready=ready,
         sample=[{"chain":k["chain"],"address":k["address"],"symbol":k["symbol"],**r} for k,r in results[:3]])
    return {"observed":len(results),"entry_ready":ready}


def main():
    data=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data")); data.mkdir(parents=True,exist_ok=True)
    db=dbopen(data/"discovery.sqlite3")
    try:
        while True:
            started=time.monotonic()
            try: cycle(db)
            except Exception as exc: emit("STOCK_STYLE_SHADOW_CYCLE_ERROR",error_type=type(exc).__name__,error=str(exc)[:160])
            time.sleep(max(5,60-(time.monotonic()-started)))
    finally: db.close()

if __name__=="__main__": main()
