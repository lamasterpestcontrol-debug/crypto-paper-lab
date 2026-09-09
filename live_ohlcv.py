"""Targeted true on-chain OHLCV collector for v0.8 paper research.

Uses GeckoTerminal's free public API. It only follows a small number of recent/high-priority
shadow candidates to stay inside public rate limits. No wallet/order APIs.
"""
from __future__ import annotations
import fcntl, json, math, os, sqlite3, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

GT="https://api.geckoterminal.com/api/v2"
VERSION="live-ohlcv-0.2.1"
MIN_CALL_INTERVAL=max(2.2,float(os.getenv("LIVE_OHLCV_MIN_CALL_INTERVAL","2.2")))
MAX_TOKENS=max(1,min(8,int(os.getenv("LIVE_OHLCV_MAX_TOKENS","6"))))
NETWORK_MAP={"solana":"solana","ethereum":"eth","base":"base","bsc":"bsc","arbitrum":"arbitrum","polygon":"polygon_pos"}
EVM={"ethereum","base","bsc","arbitrum","polygon"}
SHARED_MIN_INTERVAL=max(3.0,float(os.getenv("GT_SHARED_MIN_INTERVAL","6.0")))
REALTIME_HOLD_SECONDS=max(SHARED_MIN_INTERVAL,float(os.getenv("GT_REALTIME_HOLD_SECONDS","14.0")))
HISTORY_MAX_WAIT=max(SHARED_MIN_INTERVAL*2,float(os.getenv("GT_HISTORY_MAX_WAIT","30.0")))
HISTORY_RESERVATION_SECONDS=max(SHARED_MIN_INTERVAL,float(os.getenv("GT_HISTORY_RESERVATION_SECONDS","12.0")))

class SharedGTQuota:
    """Cross-process GeckoTerminal quota coordinator.

    The live OHLC worker gets priority over history replay. A single state file under
    the shared /data volume serializes calls across processes. HTTP 429 penalties are
    also shared so one worker cannot keep hammering while another is backing off.
    """
    def __init__(self,data_dir:Path,min_interval=SHARED_MIN_INTERVAL,realtime_hold=REALTIME_HOLD_SECONDS,history_max_wait=HISTORY_MAX_WAIT,reservation_seconds=HISTORY_RESERVATION_SECONDS,clock=None,sleeper=None):
        self.path=Path(data_dir)/".geckoterminal_quota.json"
        self.min_interval=max(0.1,float(min_interval));self.realtime_hold=max(0.0,float(realtime_hold))
        self.history_max_wait=max(self.min_interval,float(history_max_wait));self.reservation_seconds=max(self.min_interval,float(reservation_seconds))
        self.clock=clock or time.time;self.sleeper=sleeper or time.sleep
        self.path.parent.mkdir(parents=True,exist_ok=True)
    def _locked(self,mutator):
        with self.path.open("a+",encoding="utf-8") as f:
            fcntl.flock(f.fileno(),fcntl.LOCK_EX)
            try:
                f.seek(0);raw=f.read().strip()
                try:state=json.loads(raw) if raw else {}
                except Exception:state={}
                now=float(self.clock())
                # Ignore stale/corrupt far-future state after a container restart or clock anomaly.
                for k in ("next_allowed","blocked_until","realtime_until","history_wait_since","history_reserved_until"):
                    v=state.get(k,0)
                    if not isinstance(v,(int,float)) or not math.isfinite(float(v)) or float(v)<0 or float(v)>now+3600:
                        state[k]=0.0
                result=mutator(state,now)
                f.seek(0);f.truncate();json.dump(state,f,separators=(",",":"),allow_nan=False);f.flush();os.fsync(f.fileno())
                return result
            finally:fcntl.flock(f.fileno(),fcntl.LOCK_UN)
    def acquire(self,role):
        role=str(role).lower()
        if role not in {"realtime","history"}:raise ValueError("invalid quota role")
        while True:
            def inspect(state,now):
                gate=max(float(state.get("next_allowed",0)),float(state.get("blocked_until",0)))
                waiting_since=float(state.get("history_wait_since",0) or 0)
                reserved_until=float(state.get("history_reserved_until",0) or 0)
                if role=="history":
                    if waiting_since<=0:
                        waiting_since=now;state["history_wait_since"]=now
                    waited=max(0.0,now-waiting_since)
                    # Realtime normally has priority, but it may not starve history forever.
                    if waited<self.history_max_wait:
                        gate=max(gate,float(state.get("realtime_until",0)))
                    else:
                        state["history_reserved_until"]=max(reserved_until,now+self.reservation_seconds)
                    if gate>now+0.002:return (False,gate-now)
                    state["next_allowed"]=now+self.min_interval
                    state["history_wait_since"]=0.0;state["history_reserved_until"]=0.0
                    state["last_history_at"]=now
                else:
                    # Once a history waiter has exceeded its maximum wait, reserve the
                    # next available slot for it instead of continuously renewing realtime.
                    if waiting_since>0 and now-waiting_since>=self.history_max_wait:
                        if reserved_until<=now:
                            reserved_until=now+self.reservation_seconds;state["history_reserved_until"]=reserved_until
                        gate=max(gate,reserved_until)
                    if gate>now+0.002:return (False,gate-now)
                    state["next_allowed"]=now+self.min_interval
                    state["realtime_until"]=max(float(state.get("realtime_until",0)),now+self.realtime_hold)
                    state["last_realtime_at"]=now
                state["last_role"]=role;state["last_call_at"]=now
                return (True,0.0)
            ok,wait=self._locked(inspect)
            if ok:return
            self.sleeper(max(0.001,min(float(wait),5.0)))
    def penalize(self,seconds=60.0):
        penalty=max(5.0,min(600.0,float(seconds)))
        def apply(state,now):
            until=now+penalty
            state["blocked_until"]=max(float(state.get("blocked_until",0)),until)
            state["next_allowed"]=max(float(state.get("next_allowed",0)),until)
            state["last_429_at"]=now
            return until
        return self._locked(apply)
    def snapshot(self):
        return self._locked(lambda state,now:dict(state))

def retry_after_seconds(exc,default=60.0):
    try:
        raw=exc.headers.get("Retry-After") if exc.headers else None
        value=float(raw) if raw is not None else float(default)
        return max(15.0,min(600.0,value))
    except Exception:return float(default)

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
    def __init__(self,db,quota=None):self.db=db;self.last_call=0.0;self.quota=quota
    def call(self,path,params=None):
        wait=MIN_CALL_INTERVAL-(time.monotonic()-self.last_call)
        if wait>0:time.sleep(wait)
        if self.quota:self.quota.acquire("realtime")
        try:return gt_json(path,params)
        except urllib.error.HTTPError as exc:
            if exc.code==429 and self.quota:self.quota.penalize(retry_after_seconds(exc))
            raise
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
                errors+=1
                item={"chain":c["chain"],"address":c["address"],"status":"ERROR","error_type":type(exc).__name__}
                if isinstance(exc,urllib.error.HTTPError):item["http_code"]=int(exc.code)
                results.append(item)
        emit("LIVE_OHLCV_CYCLE_OK" if not errors else "LIVE_OHLCV_CYCLE_DEGRADED",observed=len(cands),errors=errors,sample=results[:3],source="GECKOTERMINAL_PUBLIC",shared_quota=bool(self.quota),no_trade=True)
        return {"observed":len(cands),"errors":errors}

def main():
    data=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"));data.mkdir(parents=True,exist_ok=True)
    db=dbopen(data/"discovery.sqlite3");quota=SharedGTQuota(data);w=Worker(db,quota=quota)
    try:
        while True:
            started=time.monotonic()
            try:w.cycle()
            except Exception as exc:emit("LIVE_OHLCV_CYCLE_ERROR",error_type=type(exc).__name__,error=str(exc)[:180])
            time.sleep(max(5,60-(time.monotonic()-started)))
    finally:db.close()
if __name__=="__main__":main()
