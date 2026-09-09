"""Sub-minute BTC lead/lag collector and research model for major coins.

Paper/research only. Uses Binance.US public aggregate-trade and bookTicker
WebSocket streams for BTC/ETH/XRP/SOL/BNB. The model does not assume a fixed lag:
it tests seconds through minutes and emits candidates only when a recent lag
relationship is statistically stronger than simultaneous response.
"""
from __future__ import annotations
import json, math, os, sqlite3, statistics, threading, time, urllib.parse, urllib.request
from dataclasses import dataclass,asdict
from pathlib import Path
try:
    import websocket
except Exception:  # tests can still exercise pure model code
    websocket=None

VERSION="major-microstructure-0.2.0"
REST="https://api.binance.us/api/v3"
WSS="wss://stream.binance.us:9443/stream?streams="
CANDIDATES={"BTC":("BTCUSDT","BTCUSD"),"ETH":("ETHUSDT","ETHUSD"),"XRP":("XRPUSDT","XRPUSD"),"SOL":("SOLUSDT","SOLUSD"),"BNB":("BNBUSDT","BNBUSD")}
TARGETS=("ETH","XRP","SOL","BNB")
LAGS_MS=(250,500,1000,2000,3000,5000,10000,15000,30000,60000,120000,300000)
LAGS=tuple(ms/1000 for ms in LAGS_MS)
BUCKET_MS=250
SOURCE="BINANCE_US_PUBLIC_AGGTRADE_WEBSOCKET"
FEE_BPS_PER_SIDE=max(0.0,float(os.getenv("MAJOR_PAPER_FEE_BPS_PER_SIDE","10")))
SLIPPAGE_BUFFER_BPS_PER_SIDE=max(0.0,float(os.getenv("MAJOR_PAPER_SLIPPAGE_BPS_PER_SIDE","2")))


def emit(event,**fields):
    print(json.dumps({"event":event,"version":VERSION,**fields},separators=(",",":"),allow_nan=False),flush=True)


def _f(x):
    v=float(x)
    if not math.isfinite(v):raise ValueError("non-finite")
    return v


def dbopen(path:Path):
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30,check_same_thread=False);db.row_factory=sqlite3.Row;db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS major_ticks(
      symbol TEXT NOT NULL,trade_id INTEGER NOT NULL,ts_ms INTEGER NOT NULL,price REAL NOT NULL,qty REAL NOT NULL,
      buyer_maker INTEGER NOT NULL,source TEXT NOT NULL,PRIMARY KEY(symbol,trade_id));
    CREATE INDEX IF NOT EXISTS idx_major_ticks_ts ON major_ticks(symbol,ts_ms);
    CREATE TABLE IF NOT EXISTS major_quotes(
      symbol TEXT PRIMARY KEY,ts_ms INTEGER NOT NULL,bid REAL NOT NULL,ask REAL NOT NULL,source TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS major_leadlag_states(
      id INTEGER PRIMARY KEY,ts REAL NOT NULL,target TEXT NOT NULL,direction INTEGER NOT NULL,
      best_lag_s REAL,best_corr REAL,corr0 REAL,beta REAL,n INTEGER NOT NULL,data_density REAL NOT NULL,
      impulse_window_s REAL,btc_impulse REAL,target_reaction REAL,reaction_completion REAL,
      expected_move_bps REAL,total_cost_bps REAL,lag_confidence REAL NOT NULL,lag_candidate INTEGER NOT NULL,
      reason TEXT NOT NULL,raw_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_major_lag_target_ts ON major_leadlag_states(target,ts);
    """);db.commit();return db


def public_json(path,params):
    if path not in ("/ticker/price",):raise ValueError("unsupported endpoint")
    url=REST+path+"?"+urllib.parse.urlencode(params)
    req=urllib.request.Request(url,headers={"User-Agent":"crypto-paper-lab-microstructure/0.1","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=8) as r:raw=r.read(500_001)
    if len(raw)>500_000:raise ValueError("oversize")
    body=json.loads(raw)
    if not isinstance(body,dict):raise ValueError("malformed")
    return body


def resolve_symbols(fetch=public_json):
    out={}
    for major,cands in CANDIDATES.items():
        errs=[]
        for sym in cands:
            try:
                body=fetch("/ticker/price",{"symbol":sym});_f(body.get("price"));out[major]=sym;break
            except Exception as exc:errs.append(type(exc).__name__)
        if major not in out:raise RuntimeError(f"no market for {major}:{errs}")
    return out


def stream_url(symbol_map):
    streams=[]
    for sym in symbol_map.values():
        s=sym.lower();streams.extend((s+"@aggTrade",s+"@bookTicker"))
    return WSS+"/".join(streams)


def store_trade(db,major,data):
    tid=int(data["a"]);ts=int(data["T"]);p=_f(data["p"]);q=_f(data["q"])
    if ts<=0 or p<=0 or q<0:raise ValueError("invalid trade")
    with db:db.execute("INSERT OR IGNORE INTO major_ticks(symbol,trade_id,ts_ms,price,qty,buyer_maker,source) VALUES(?,?,?,?,?,?,?)",
      (major,tid,ts,p,q,int(bool(data.get("m"))),SOURCE))


def store_quote(db,major,data):
    ts=int(data.get("E") or int(time.time()*1000));bid=_f(data["b"]);ask=_f(data["a"])
    if bid<=0 or ask<=0 or ask<bid:raise ValueError("invalid quote")
    with db:db.execute("""INSERT INTO major_quotes(symbol,ts_ms,bid,ask,source) VALUES(?,?,?,?,?)
      ON CONFLICT(symbol) DO UPDATE SET ts_ms=excluded.ts_ms,bid=excluded.bid,ask=excluded.ask,source=excluded.source""",
      (major,ts,bid,ask,SOURCE))


def _raw_ticks(db,symbol,start_ms,end_ms):
    return [(int(r["ts_ms"]),float(r["price"])) for r in db.execute("SELECT ts_ms,price FROM major_ticks WHERE symbol=? AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms,trade_id",(symbol,start_ms,end_ms)).fetchall()]


def grid_series(db,symbol,now_ms,lookback_s=7200,bucket_ms=BUCKET_MS):
    """Forward-filled close series on a uniform grid. Raw trades retain ms timestamps.

    A 250ms research grid lets the model test sub-second lag without claiming the
    market always reacts that fast. Sparse markets naturally fail the density gate.
    """
    if bucket_ms<=0 or 1000%bucket_ms!=0:raise ValueError("bucket_ms must divide one second")
    rows=_raw_ticks(db,symbol,now_ms-lookback_s*1000,now_ms)
    if not rows:return [],0.0
    buckets={}
    for ts,p in rows:buckets[ts//bucket_ms]=p
    first=min(buckets);last=now_ms//bucket_ms;out=[];cur=None;nonempty=0
    for slot in range(first,last+1):
        if slot in buckets:cur=buckets[slot];nonempty+=1
        if cur is not None:out.append((slot,cur))
    density=nonempty/max(1,last-first+1)
    return out,density

def second_series(db,symbol,now_ms,lookback_s=7200):
    return grid_series(db,symbol,now_ms,lookback_s,1000)


def _returns_by_window(series,window_s):
    prices=dict(series);keys=sorted(prices);out={}
    if not keys:return out
    # forward-filled series normally has every second; tolerate gaps by exact lookup only.
    for t in keys:
        old=prices.get(t-window_s)
        if old and old>0:out[t]=prices[t]/old-1
    return out


def _pearson(xs,ys):
    if len(xs)<3:return None
    mx=sum(xs)/len(xs);my=sum(ys)/len(ys);vx=sum((x-mx)**2 for x in xs);vy=sum((y-my)**2 for y in ys)
    if vx<=0 or vy<=0:return None
    return sum((x-mx)*(y-my) for x,y in zip(xs,ys))/math.sqrt(vx*vy)


def _beta(xs,ys):
    if len(xs)<3:return None
    mx=sum(xs)/len(xs);my=sum(ys)/len(ys);v=sum((x-mx)**2 for x in xs)
    if v<=0:return None
    return sum((x-mx)*(y-my) for x,y in zip(xs,ys))/v


def quote_cost_bps(db,target,now_ms):
    row=db.execute("SELECT * FROM major_quotes WHERE symbol=?",(target,)).fetchone()
    spread=8.0 # conservative fallback if bookTicker is stale/missing
    if row and now_ms-int(row["ts_ms"])<=10_000:
        bid=float(row["bid"]);ask=float(row["ask"]);mid=(bid+ask)/2
        if mid>0:spread=(ask-bid)/mid*10000
    return spread+2*FEE_BPS_PER_SIDE+2*SLIPPAGE_BUFFER_BPS_PER_SIDE


@dataclass(frozen=True)
class LagState:
    target:str;direction:int;best_lag_s:float|None;best_corr:float|None;corr0:float|None;beta:float|None;n:int
    data_density:float;impulse_window_s:float|None;btc_impulse:float|None;target_reaction:float|None
    reaction_completion:float|None;expected_move_bps:float;total_cost_bps:float;lag_confidence:float
    lag_candidate:bool;reason:str


def _analyze_grid(btc,target,target_name,total_cost_bps=25.0,density=1.0,min_n=600,step_ms=1000):
    if step_ms<=0:raise ValueError("invalid step_ms")
    lag_ms=[ms for ms in LAGS_MS if ms%step_ms==0]
    lag_steps=[(ms,ms//step_ms) for ms in lag_ms]
    max_lag=max((u for _,u in lag_steps),default=0)
    max_window=max(1,60_000//step_ms)
    if len(btc)<min_n+max_lag+max_window or len(target)<min_n+max_lag+max_window:
        return LagState(target_name,0,None,None,None,None,min(len(btc),len(target)),density,None,None,None,None,0,total_cost_bps,0,False,"INSUFFICIENT_SUBMINUTE_HISTORY")
    # For sub-second grids use 1s returns; for 1s grids preserve the older 5s smoothing.
    ret_window_ms=1000 if step_ms<1000 else 5000
    ret_steps=max(1,ret_window_ms//step_ms)
    br=_returns_by_window(btc,ret_steps);tr=_returns_by_window(target,ret_steps)
    common=sorted(set(br)&set(tr))[-max(min_n,int(3_600_000/step_ms)):]
    if len(common)<min_n:return LagState(target_name,0,None,None,None,None,len(common),density,None,None,None,None,0,total_cost_bps,0,False,"INSUFFICIENT_COMMON_HISTORY")
    corr={};betas={};ns={};tkeys=set(tr)
    for lag_ms_value,lag_units in lag_steps:
        xs=[];ys=[]
        for t in common:
            if t+lag_units in tkeys:xs.append(br[t]);ys.append(tr[t+lag_units])
        corr[lag_ms_value]=_pearson(xs,ys);betas[lag_ms_value]=_beta(xs,ys);ns[lag_ms_value]=len(xs)
    xs0=[];ys0=[]
    for t in common:
        if t in tr:xs0.append(br[t]);ys0.append(tr[t])
    corr0=_pearson(xs0,ys0)
    valid=[(lag,c) for lag,c in corr.items() if c is not None and ns[lag]>=min_n]
    if not valid:return LagState(target_name,0,None,None,corr0,None,len(common),density,None,None,None,None,0,total_cost_bps,0,False,"NO_VALID_LAG_CORRELATION")
    best_lag_ms,best_corr=max(valid,key=lambda x:x[1]);beta=betas[best_lag_ms];best_lag_s=best_lag_ms/1000
    improvement=best_corr-(corr0 if corr0 is not None else 0)
    # Sub-second bins are much more sensitive to sparse prints, so require higher density.
    min_density=.05 if step_ms>=1000 else .03
    stable=best_corr>=.12 and improvement>=.025 and density>=min_density and beta is not None and beta>0
    conf=max(0.0,min(1.0,.35+max(0,best_corr)*.7+max(0,improvement)*2.0+(min(1,density/.30)-.5)*.15)) if stable else max(0.0,min(.55,max(0,best_corr or 0)))

    prices_b=dict(btc);prices_t=dict(target);now=min(max(prices_b),max(prices_t));candidates=[]
    # Dynamic event windows. Floors are merely noise guards; history-based median still dominates when volatile.
    impulse_specs=((250,.00015),(500,.00022),(1000,.00035),(2000,.00045),(5000,.00055),(10000,.00075),(30000,.0012),(60000,.0018))
    for w_ms,floor in impulse_specs:
        if w_ms%step_ms:continue
        w=max(1,w_ms//step_ms);bret=_returns_by_window(btc,w)
        hist_back=max(1,1_800_000//step_ms);cooloff=max(1,5000//step_ms)
        hist=[abs(v) for t,v in bret.items() if now-hist_back<=t<now-cooloff]
        med=statistics.median(hist) if hist else 0
        threshold=max(floor,4.0*med);impulse=bret.get(now)
        if impulse is not None and abs(impulse)>=threshold:candidates.append((abs(impulse)/max(threshold,1e-9),w_ms,w,impulse))
    if not stable:return LagState(target_name,0,best_lag_s,best_corr,corr0,beta,ns[best_lag_ms],density,None,None,None,None,0,total_cost_bps,conf,False,"NO_STABLE_DYNAMIC_LAG")
    if not candidates:return LagState(target_name,0,best_lag_s,best_corr,corr0,beta,ns[best_lag_ms],density,None,None,None,None,0,total_cost_bps,conf,False,"NO_BTC_SUBMINUTE_IMPULSE")
    _,w_ms,w,impulse=max(candidates);tret=_returns_by_window(target,w).get(now)
    if tret is None:return LagState(target_name,0,best_lag_s,best_corr,corr0,beta,ns[best_lag_ms],density,w_ms/1000,impulse,None,None,0,total_cost_bps,conf,False,"NO_TARGET_CURRENT_REACTION")
    expected=beta*impulse;direction=1 if impulse>0 else -1;aligned=(tret*expected)>0
    completion=(abs(tret)/abs(expected)) if aligned and expected else 0.0
    completion=max(0.0,min(1.5,completion));remaining=max(0.0,abs(expected)-max(0.0,abs(tret) if aligned else 0.0))*10000
    candidate=completion<.70 and remaining>total_cost_bps+5 and conf>=.58
    reason="BTC_IMPULSE_TARGET_UNDERREACTED" if candidate else "TARGET_REACTED_OR_NO_NET_EDGE"
    return LagState(target_name,direction,best_lag_s,best_corr,corr0,beta,ns[best_lag_ms],density,w_ms/1000,impulse,tret,completion,remaining,total_cost_bps,conf,candidate,reason)


def analyze_series(btc,target,target_name,total_cost_bps=25.0,density=1.0,min_n=600):
    """Compatibility helper for second-indexed synthetic tests."""
    return _analyze_grid(btc,target,target_name,total_cost_bps,density,min_n,1000)


def analyze_subsecond_series(btc,target,target_name,total_cost_bps=25.0,density=1.0,min_n=2400):
    """250ms-grid lead/lag research; best_lag_s may be 0.25 or 0.5 seconds."""
    return _analyze_grid(btc,target,target_name,total_cost_bps,density,min_n,BUCKET_MS)


def analyze_db(db,now_ms=None):
    now_ms=int(time.time()*1000) if now_ms is None else int(now_ms);btc,bd=grid_series(db,"BTC",now_ms,bucket_ms=BUCKET_MS);out={}
    for target in TARGETS:
        ts,td=grid_series(db,target,now_ms,bucket_ms=BUCKET_MS);density=min(bd,td);cost=quote_cost_bps(db,target,now_ms)
        out[target]=analyze_subsecond_series(btc,ts,target,cost,density)
    return out


def save_states(db,states,now=None):
    now=time.time() if now is None else float(now)
    with db:
        for s in states.values():
            raw=json.dumps(asdict(s),separators=(",",":"),allow_nan=False)
            db.execute("""INSERT INTO major_leadlag_states(ts,target,direction,best_lag_s,best_corr,corr0,beta,n,data_density,impulse_window_s,btc_impulse,target_reaction,reaction_completion,expected_move_bps,total_cost_bps,lag_confidence,lag_candidate,reason,raw_json)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(now,s.target,s.direction,s.best_lag_s,s.best_corr,s.corr0,s.beta,s.n,s.data_density,s.impulse_window_s,s.btc_impulse,s.target_reaction,s.reaction_completion,s.expected_move_bps,s.total_cost_bps,s.lag_confidence,int(s.lag_candidate),s.reason,raw))


def latest_states(db,now=None,max_age=10.0):
    now=time.time() if now is None else float(now);out={}
    for t in TARGETS:
        row=db.execute("SELECT * FROM major_leadlag_states WHERE target=? ORDER BY ts DESC,id DESC LIMIT 1",(t,)).fetchone()
        if row and now-float(row["ts"])<=max_age:out[t]=row
    return out


def prune(db,now_ms):
    with db:
        db.execute("DELETE FROM major_ticks WHERE ts_ms<?",(now_ms-6*3600*1000,))
        db.execute("DELETE FROM major_leadlag_states WHERE ts<?",(now_ms/1000-3*86400,))


class StreamWorker:
    def __init__(self,path:Path,symbol_map=None):
        self.path=path;self.symbol_map=symbol_map or resolve_symbols();self.reverse={v:k for k,v in self.symbol_map.items()};self.stop=threading.Event()
    def on_message(self,ws,message):
        body=json.loads(message);data=body.get("data") if isinstance(body,dict) else None
        if not isinstance(data,dict):return
        major=self.reverse.get(str(data.get("s") or ""))
        if not major:return
        db=dbopen(self.path)
        try:
            if data.get("e")=="aggTrade":store_trade(db,major,data)
            elif "b" in data and "a" in data:store_quote(db,major,data)
        finally:db.close()
    def run_stream(self):
        if websocket is None:raise RuntimeError("websocket-client unavailable")
        delay=1.0
        while not self.stop.is_set():
            try:
                app=websocket.WebSocketApp(stream_url(self.symbol_map),on_message=self.on_message,
                    on_error=lambda ws,e:emit("MAJOR_STREAM_ERROR",error_type=type(e).__name__,error=str(e)[:120]),
                    on_close=lambda ws,a,b:emit("MAJOR_STREAM_CLOSED",code=a,reason=str(b)[:80]))
                app.run_forever(ping_interval=15,ping_timeout=8)
            except Exception as exc:emit("MAJOR_STREAM_CONNECT_ERROR",error_type=type(exc).__name__,error=str(exc)[:160])
            if not self.stop.is_set():time.sleep(delay);delay=min(30,delay*1.7)
    def analyze_loop(self):
        db=dbopen(self.path)
        try:
            while not self.stop.is_set():
                started=time.monotonic()
                try:
                    states=analyze_db(db);save_states(db,states);prune(db,int(time.time()*1000))
                    emit("MAJOR_MICROSTRUCTURE_OK",candidates={k:v.lag_candidate for k,v in states.items()},
                         lags={k:v.best_lag_s for k,v in states.items()},reasons={k:v.reason for k,v in states.items()},source=SOURCE,no_trade=True)
                except Exception as exc:emit("MAJOR_MICROSTRUCTURE_ANALYSIS_ERROR",error_type=type(exc).__name__,error=str(exc)[:180])
                self.stop.wait(max(.5,2-(time.monotonic()-started)))
        finally:db.close()
    def run(self):
        t=threading.Thread(target=self.run_stream,daemon=True);t.start();self.analyze_loop()


def main():
    data=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"));data.mkdir(parents=True,exist_ok=True)
    StreamWorker(data/"discovery.sqlite3").run()
if __name__=="__main__":main()
