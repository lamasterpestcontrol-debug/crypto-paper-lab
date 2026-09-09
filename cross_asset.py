"""Free secondary cross-asset confirmation for crypto market regime.

Research/paper only. Uses Yahoo public chart endpoints without credentials as a
best-effort secondary market-confirmation source. Because this endpoint is not an
official exchange feed, it may never be the sole trigger for a trade.
"""
from __future__ import annotations
import json, math, os, sqlite3, time, urllib.parse, urllib.request
from pathlib import Path

VERSION="cross-asset-0.1.0"
BASE="https://query1.finance.yahoo.com/v8/finance/chart"
ASSETS={
    "SP500":"^GSPC",
    "NASDAQ":"^IXIC",
    "VIX":"^VIX",
    "DXY":"DX-Y.NYB",
    "US10Y":"^TNX",
    "OIL":"CL=F",
}
SOURCE="YAHOO_PUBLIC_CHART_UNOFFICIAL_SECONDARY"


def emit(event,**fields):
    print(json.dumps({"event":event,"version":VERSION,**fields},separators=(",",":"),allow_nan=False),flush=True)


def _num(x):
    v=float(x)
    if not math.isfinite(v): raise ValueError("non-finite number")
    return v


def fetch_chart(ticker:str,interval="1m",range_="1d"):
    if ticker not in ASSETS.values(): raise ValueError("unsupported ticker")
    url=f"{BASE}/{urllib.parse.quote(ticker,safe='')}?"+urllib.parse.urlencode({"interval":interval,"range":range_,"includePrePost":"true"})
    req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 crypto-paper-lab/0.1","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=10) as r:
        raw=r.read(2_000_001)
        if len(raw)>2_000_000: raise ValueError("oversize response")
    body=json.loads(raw)
    try: result=body["chart"]["result"][0]
    except (KeyError,TypeError,IndexError): raise ValueError("malformed chart response")
    ts=result.get("timestamp"); q=((result.get("indicators") or {}).get("quote") or [{}])[0]; closes=q.get("close")
    if not isinstance(ts,list) or not isinstance(closes,list) or len(ts)!=len(closes): raise ValueError("malformed chart series")
    out=[]
    for t,p in zip(ts,closes):
        if p is None: continue
        out.append((int(t),_num(p)))
    if not out: raise ValueError("empty chart series")
    return out


def dbopen(path:Path):
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30);db.row_factory=sqlite3.Row;db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS cross_asset_states(
      id INTEGER PRIMARY KEY,ts REAL NOT NULL,risk_score REAL NOT NULL,shock INTEGER NOT NULL,
      data_quality REAL NOT NULL,returns_json TEXT NOT NULL,source TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_cross_asset_ts ON cross_asset_states(ts);
    """);db.commit();return db


def _ret(series,minutes):
    if len(series)<2:return None
    latest_ts,cur=series[-1]; target=latest_ts-minutes*60; old=None
    for ts,p in reversed(series):
        if ts<=target:old=p;break
    return (cur/old-1) if old else None


def classify(series_by):
    returns={}
    for name,series in series_by.items():
        returns[name]={"5m":_ret(series,5),"15m":_ret(series,15),"60m":_ret(series,60)}
    usable=sum(returns.get(a,{}).get("15m") is not None for a in ASSETS)
    quality=usable/len(ASSETS)
    score=0.0
    # Risk-assets down = risk-off; VIX/DXY/yields/oil up = risk-off. Candidate
    # weights are intentionally modest because the source is secondary/unofficial.
    def r(a,h="15m"): return returns.get(a,{}).get(h)
    if r("SP500") is not None: score += max(-25,min(25,r("SP500")*2500))
    if r("NASDAQ") is not None: score += max(-25,min(25,r("NASDAQ")*2200))
    if r("VIX") is not None: score -= max(-20,min(20,r("VIX")*500))
    if r("DXY") is not None: score -= max(-10,min(10,r("DXY")*1800))
    if r("US10Y") is not None: score -= max(-10,min(10,r("US10Y")*1000))
    if r("OIL") is not None: score -= max(-10,min(10,r("OIL")*600))
    score=max(-100.0,min(100.0,score))
    shock=False
    if quality>=.66:
        sp=r("SP500","5m");nq=r("NASDAQ","5m");vx=r("VIX","5m")
        shock=bool((sp is not None and nq is not None and sp<=-.012 and nq<=-.015) or (vx is not None and vx>=.08))
    return score,shock,quality,returns


def latest_state(db,now=None,max_age=180.0):
    row=db.execute("SELECT * FROM cross_asset_states ORDER BY ts DESC,id DESC LIMIT 1").fetchone()
    if not row:return None
    now=time.time() if now is None else float(now)
    if now-float(row["ts"])>max_age or float(row["ts"])>now+5:return None
    try:return {"ts":float(row["ts"]),"risk_score":float(row["risk_score"]),"shock":bool(row["shock"]),"data_quality":float(row["data_quality"]),"returns":json.loads(row["returns_json"]),"source":row["source"]}
    except Exception:return None


def cycle(db,now=None,fetcher=fetch_chart):
    now=time.time() if now is None else float(now);series={};errors={}
    for name,ticker in ASSETS.items():
        try: series[name]=fetcher(ticker)
        except Exception as exc: errors[name]=type(exc).__name__
    score,shock,quality,returns=classify(series)
    with db:db.execute("INSERT INTO cross_asset_states(ts,risk_score,shock,data_quality,returns_json,source) VALUES(?,?,?,?,?,?)",
      (now,score,int(shock),quality,json.dumps(returns,separators=(",",":"),allow_nan=False),SOURCE))
    emit("CROSS_ASSET_OK" if quality>=.5 else "CROSS_ASSET_DEGRADED",risk_score=round(score,3),shock=shock,data_quality=round(quality,3),errors=errors,source=SOURCE,no_trade=True)
    return {"risk_score":score,"shock":shock,"data_quality":quality,"returns":returns,"errors":errors}


def main():
    data=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"));data.mkdir(parents=True,exist_ok=True)
    db=dbopen(data/"discovery.sqlite3")
    try:
        while True:
            started=time.monotonic()
            try:cycle(db)
            except Exception as exc:emit("CROSS_ASSET_ERROR",error_type=type(exc).__name__,error=str(exc)[:180])
            time.sleep(max(15,60-(time.monotonic()-started)))
    finally:db.close()
if __name__=="__main__":main()
