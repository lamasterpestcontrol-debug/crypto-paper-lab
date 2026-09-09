"""Targeted true on-chain OHLCV collector for v0.8 paper research.

Uses GeckoTerminal's free public API. It only follows a small number of recent/high-priority
shadow candidates to stay inside public rate limits. No wallet/order APIs.
"""
from __future__ import annotations
import json, math, os, sqlite3, time, urllib.parse, urllib.request
from pathlib import Path

GT="https://api.geckoterminal.com/api/v2"
VERSION="live-ohlcv-0.1.0"
MIN_CALL_INTERVAL=max(2.2,float(os.getenv("LIVE_OHLCV_MIN_CALL_INTERVAL","2.2")))
MAX_TOKENS=max(1,min(8,int(os.getenv("LIVE_OHLCV_MAX_TOKENS","6"))))
NETWORK_MAP={"solana":"solana","ethereum":"eth","base":"base","bsc":"bsc","arbitrum":"arbitrum","polygon":"polygon_pos"}
EVM={"ethereum","base","bsc","arbitrum","polygon"}

def emit(event,**fields):
    print(json.dumps({"event":event,"version":VERSION,**fields},separators=(",",":"),allow_nan=False),flush=True)

def _num(x):
    v=float(x)
    if not math.isfinite(v): raise ValueError("non-finite number")
    return v

def _same_addr(chain,a,b):
    return str(a).lower()==str(b).lower() if chain in EVM else str(a)==str(b)

def _id_addr(value):
    s=str(value or "")
    return s.rsplit("_",1)[1] if "_" in s else s

def gt_json(path,params=None):
    if not path.startswith("/networks/"): raise ValueError("unsupported endpoint")
    url=GT+path+("?"+urllib.parse.urlencode(params) if params else "")
    req=urllib.request.Request(url,headers={"User-Agent":"crypto-paper-lab-live-ohlcv/0.1","Accept":"application/json;version=20230203"})
    with urllib.request.urlopen(req,timeout=15) as r:
        raw=r.read(3_000_001)
        if len(raw)>3_000_000: raise ValueError("oversize response")
    body=json.loads(raw)
    if not isinstance(body,dict): raise ValueError("malformed response")
    return body

def dbopen(path:Path):
    db=sqlite3.connect(path,timeout=30);db.row_factory=sqlite3.Row;db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS live_pool_map(
      chain TEXT NOT NULL,address TEXT NOT NULL,network TEXT NOT NULL,pool_address TEXT NOT NULL,
      token_side TEXT NOT NULL,reserve_usd REAL,resolved_at REAL NOT NULL,
      PRIMARY KEY(chain,address));
    CREATE TABLE IF NOT EXISTS live_ohlcv(
      chain TEXT NOT NULL,address TEXT NOT NULL,network TEXT NOT NULL,pool_address TEXT NOT NULL,
      timeframe TEXT NOT NULL,ts INTEGER NOT NULL,open REAL NOT NULL,high REAL NOT NULL,low REAL NOT NULL,
      close REAL NOT NULL,volume REAL NOT NULL,source TEXT NOT NULL,
      PRIMARY KEY(chain,address,timeframe,ts));
    CREATE INDEX IF NOT EXISTS idx_live_ohlcv_token_ts ON live_ohlcv(chain,address,timeframe,ts);
    """);db.commit();return db

def select_candidates(db,now,limit=MAX_TOKENS):
    # Merge the original DexScreener research feed with GMGN early-discovery candidates.
    # GMGN can surface a token before it appears in the profile feed; only chains that
    # GeckoTerminal can resolve are sent to this collector.
    items={}
    for r in db.execute("""SELECT chain,address,MAX(symbol) symbol,MAX(fast_priority) priority,MAX(ts) last_ts
      FROM strategy_ab_observations WHERE ts>=? AND address IS NOT NULL
      GROUP BY chain,address""",(now-3600,)).fetchall():
        k=(str(r["chain"]),str(r["address"]));items[k]={"chain":k[0],"address":k[1],"symbol":r["symbol"],"priority":float(r["priority"] or 0),"last_ts":float(r["last_ts"] or 0)}
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gmgn_discovery_candidates'").fetchone()
    if exists:
        for r in db.execute("""SELECT chain,address,symbol,priority,last_seen FROM gmgn_discovery_candidates
          WHERE last_seen>=? ORDER BY priority DESC,last_seen DESC LIMIT ?""",(now-3600,limit*4)).fetchall():
            chain=str(r["chain"]);address=str(r["address"]);
            if chain not in NETWORK_MAP:continue
            k=(chain,address);candidate={"chain":chain,"address":address,"symbol":r["symbol"],"priority":float(r["priority"] or 0),"last_ts":float(r["last_seen"] or 0)}
            if k not in items or (candidate["priority"],candidate["last_ts"])>(items[k]["priority"],items[k]["last_ts"]):items[k]=candidate
    return sorted(items.values(),key=lambda x:(x["priority"],x["last_ts"]),reverse=True)[:limit]

def parse_top_pool(chain,address,body):
    data=body.get("data")
    if not isinstance(data,list): raise ValueError("top-pools response missing data")
    for item in data:
        if not isinstance(item,dict): continue
        attrs=item.get("attributes") or {}; rel=item.get("relationships") or {}
        pool=str(attrs.get("address") or _id_addr(item.get("id")))
        if not pool: continue
        base=_id_addr((((rel.get("base_token") or {}).get("data") or {}).get("id")))
        quote=_id_addr((((rel.get("quote_token") or {}).get("data") or {}).get("id")))
        side="base" if _same_addr(chain,base,address) else "quote" if _same_addr(chain,quote,address) else None
        if side:
            reserve=_num(attrs.get("reserve_in_usd") or 0)
            return {"pool_address":pool,"token_side":side,"reserve_usd":max(0.0,reserve)}
    return None

def parse_ohlcv(body):
    try: rows=body["data"]["attributes"]["ohlcv_list"]
    except (KeyError,TypeError): raise ValueError("OHLCV response missing rows")
    if not isinstance(rows,list): raise ValueError("OHLCV rows not list")
    out=[]
    for i,row in enumerate(rows):
        if not isinstance(row,list) or len(row)<6: raise ValueError(f"bad row {i}")
        ts=_num(row[0]); vals=tuple(_num(x) for x in row[1:6])
        if ts<=0 or not ts.is_integer(): raise ValueError("invalid timestamp")
        o,h,l,c,v=vals
        if min(o,h,l,c)<=0 or v<0 or h<max(o,l,c) or l>min(o,h,c): raise ValueError("invalid OHLCV bounds")
        out.append((int(ts),o,h,l,c,v))
    return out

def save_pool(db,chain,address,network,info,now):
    with db: db.execute("""INSERT INTO live_pool_map(chain,address,network,pool_address,token_side,reserve_usd,resolved_at)
      VALUES(?,?,?,?,?,?,?) ON CONFLICT(chain,address) DO UPDATE SET network=excluded.network,pool_address=excluded.pool_address,
      token_side=excluded.token_side,reserve_usd=excluded.reserve_usd,resolved_at=excluded.resolved_at""",
      (chain,address,network,info["pool_address"],info["token_side"],info["reserve_usd"],now))

def save_bars(db,chain,address,network,pool,rows):
    now=int(time.time())
    valid=[r for r in rows if r[0]<=now+5]
    with db:
        before=db.total_changes
        db.executemany("""INSERT OR REPLACE INTO live_ohlcv(chain,address,network,pool_address,timeframe,ts,open,high,low,close,volume,source)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",[(chain,address,network,pool,"minute",*r,"GECKOTERMINAL_PUBLIC_ONCHAIN") for r in valid])
        return db.total_changes-before

def load_true_bars(db,chain,address,limit=60):
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_ohlcv'").fetchone()
    if not exists:return []
    return db.execute("""SELECT ts,open,high,low,close,volume FROM live_ohlcv
      WHERE chain=? AND address=? AND timeframe='minute' ORDER BY ts DESC LIMIT ?""",(chain,address,limit)).fetchall()[::-1]

class Worker:
    def __init__(self,db):self.db=db;self.last_call=0.0
    def call(self,path,params=None):
        wait=MIN_CALL_INTERVAL-(time.monotonic()-self.last_call)
        if wait>0:time.sleep(wait)
        try:return gt_json(path,params)
        finally:self.last_call=time.monotonic()
    def pool_for(self,chain,address,now):
        row=self.db.execute("SELECT * FROM live_pool_map WHERE chain=? AND address=?",(chain,address)).fetchone()
        if row and now-float(row["resolved_at"])<900:return row
        network=NETWORK_MAP.get(chain)
        if not network:return None
        body=self.call(f"/networks/{network}/tokens/{address}/pools",{"page":1,"sort":"h24_volume_usd_liquidity_desc"})
        info=parse_top_pool(chain,address,body)
        if not info:return None
        save_pool(self.db,chain,address,network,info,now)
        return self.db.execute("SELECT * FROM live_pool_map WHERE chain=? AND address=?",(chain,address)).fetchone()
    def collect_one(self,chain,address,now):
        pm=self.pool_for(chain,address,now)
        if not pm:return {"chain":chain,"address":address,"status":"NO_POOL"}
        body=self.call(f"/networks/{pm['network']}/pools/{pm['pool_address']}/ohlcv/minute",
            {"aggregate":1,"limit":60,"currency":"usd","token":pm["token_side"],"include_empty_intervals":"false"})
        rows=parse_ohlcv(body); inserted=save_bars(self.db,chain,address,pm["network"],pm["pool_address"],rows)
        return {"chain":chain,"address":address,"status":"OK","bars":len(rows),"written":inserted,"pool":pm["pool_address"],"side":pm["token_side"]}
    def cycle(self):
        now=time.time();cands=select_candidates(self.db,now);results=[];errors=0
        for c in cands:
            try:results.append(self.collect_one(str(c["chain"]),str(c["address"]),now))
            except Exception as exc:
                errors+=1;results.append({"chain":c["chain"],"address":c["address"],"status":"ERROR","error_type":type(exc).__name__})
        emit("LIVE_OHLCV_CYCLE_OK" if not errors else "LIVE_OHLCV_CYCLE_DEGRADED",observed=len(cands),errors=errors,sample=results[:3],source="GECKOTERMINAL_PUBLIC",no_trade=True)
        return {"observed":len(cands),"errors":errors}

def main():
    data=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"));data.mkdir(parents=True,exist_ok=True)
    db=dbopen(data/"discovery.sqlite3");w=Worker(db)
    try:
        while True:
            started=time.monotonic()
            try:w.cycle()
            except Exception as exc:emit("LIVE_OHLCV_CYCLE_ERROR",error_type=type(exc).__name__,error=str(exc)[:180])
            time.sleep(max(5,60-(time.monotonic()-started)))
    finally:db.close()
if __name__=="__main__":main()
