"""Five-major crypto market regime + BTC lead/lag research worker.

Paper/research only. Uses Binance.US public 1-minute klines for one consistent,
US-accessible venue. It stores market observations but never accesses wallets,
accounts, or order APIs.

Lead/lag is estimated dynamically from completed one-minute bars. A historical
lag is never assumed to persist and never becomes a standalone trade signal.
"""
from __future__ import annotations
import json, math, os, sqlite3, statistics, time, urllib.parse, urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Mapping, Sequence

VERSION="market-regime-0.2.0"
BINANCE_US="https://api.binance.us/api/v3"
SYMBOL_CANDIDATES={"BTC":("BTCUSDT","BTCUSD"),"ETH":("ETHUSDT","ETHUSD"),"XRP":("XRPUSDT","XRPUSD"),"SOL":("SOLUSDT","SOLUSD"),"BNB":("BNBUSDT","BNBUSD")}
SYMBOLS={k:v[0] for k,v in SYMBOL_CANDIDATES.items()}
COINS=tuple(SYMBOL_CANDIDATES)
CHAIN_LEADER={"solana":"SOL","bsc":"BNB","ethereum":"ETH","base":"ETH","arbitrum":"ETH","polygon":None,"robinhood":None,"arc":None,"stable":None}
SOURCE="BINANCE_US_PUBLIC_1M_KLINE"


def emit(event, **fields):
    print(json.dumps({"event":event,"version":VERSION,**fields},separators=(",",":"),allow_nan=False),flush=True)


def finite_pos(x):
    v=float(x)
    if not math.isfinite(v) or v<=0: raise ValueError("invalid positive number")
    return v


def finite_nonneg(x):
    v=float(x)
    if not math.isfinite(v) or v<0: raise ValueError("invalid nonnegative number")
    return v


def public_json(path:str,params:Mapping[str,object]):
    if path!="/klines": raise ValueError("unsupported Binance.US endpoint")
    url=BINANCE_US+path+"?"+urllib.parse.urlencode(params)
    req=urllib.request.Request(url,headers={"User-Agent":"crypto-paper-lab-market-regime/0.2","Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=10) as resp:
        raw=resp.read(2_000_001)
        if len(raw)>2_000_000: raise ValueError("oversize kline response")
    body=json.loads(raw)
    if not isinstance(body,list): raise ValueError("malformed kline response")
    return body


def parse_klines(rows,now_ms:int):
    if not isinstance(rows,list): raise ValueError("klines must be a list")
    out=[]
    for i,row in enumerate(rows):
        if not isinstance(row,list) or len(row)<7: raise ValueError(f"bad kline row {i}")
        try:
            open_ms=int(row[0]); close_ms=int(row[6])
            o,h,l,c=(finite_pos(row[j]) for j in range(1,5)); v=finite_nonneg(row[5])
        except (TypeError,ValueError,OverflowError) as exc:
            raise ValueError(f"invalid kline row {i}: {exc}") from exc
        if open_ms<=0 or close_ms<open_ms or h<max(o,l,c) or l>min(o,h,c):
            raise ValueError(f"invalid kline bounds {i}")
        # Open timestamp is the canonical 1-minute bucket. A bar is complete only
        # after Binance.US' close timestamp; correlation never consumes partial bars.
        out.append((open_ms//1000,o,h,l,c,v,1 if now_ms>close_ms else 0))
    return out


def fetch_symbol_klines(symbol:str,limit:int=8,now_ms:int|None=None):
    if symbol not in {x for xs in SYMBOL_CANDIDATES.values() for x in xs}: raise ValueError("unsupported major symbol")
    now_ms=int(time.time()*1000) if now_ms is None else int(now_ms)
    rows=public_json("/klines",{"symbol":symbol,"interval":"1m","limit":int(limit)})
    return parse_klines(rows,now_ms)

def fetch_major_klines(major:str,limit:int,now_ms:int):
    if major not in SYMBOL_CANDIDATES: raise ValueError("unsupported major")
    errors=[]
    for venue in SYMBOL_CANDIDATES[major]:
        try:return venue,fetch_symbol_klines(venue,limit,now_ms)
        except Exception as exc:errors.append(f"{venue}:{type(exc).__name__}")
    raise RuntimeError("no Binance.US market for "+major+";"+",".join(errors))


def dbopen(path:Path):
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30); db.row_factory=sqlite3.Row; db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS major_ohlcv_1m(
        symbol TEXT NOT NULL,ts INTEGER NOT NULL,open REAL NOT NULL,high REAL NOT NULL,low REAL NOT NULL,
        close REAL NOT NULL,volume REAL NOT NULL,complete INTEGER NOT NULL,source TEXT NOT NULL,
        PRIMARY KEY(symbol,ts));
      CREATE INDEX IF NOT EXISTS idx_major_ohlcv_symbol_ts ON major_ohlcv_1m(symbol,ts);
      CREATE TABLE IF NOT EXISTS market_regime_states(
        id INTEGER PRIMARY KEY,ts REAL NOT NULL,regime TEXT NOT NULL,shock_direction TEXT,
        confidence REAL NOT NULL,breadth_5m INTEGER,breadth_15m INTEGER,breadth_60m INTEGER,
        btc_1m REAL,btc_5m REAL,btc_15m REAL,btc_60m REAL,
        returns_json TEXT NOT NULL,lead_lag_json TEXT NOT NULL,source TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS idx_market_regime_states_ts ON market_regime_states(ts);
    """)
    # Backward-compatible migrations for volumes from the v0.1 prototype.
    cols={r[1] for r in db.execute("PRAGMA table_info(market_regime_states)")}
    for name,kind in (("shock_direction","TEXT"),("btc_1m","REAL"),("lead_lag_json","TEXT")):
        if name not in cols: db.execute(f"ALTER TABLE market_regime_states ADD COLUMN {name} {kind}")
    db.commit(); return db


def save_symbol_bars(db,symbol:str,rows,now:float,source:str=SOURCE):
    if symbol not in COINS: raise ValueError("unsupported major")
    now=float(now)
    vals=[]
    for ts,o,h,l,c,v,complete in rows:
        if ts>now+5: raise ValueError("future major kline")
        vals.append((symbol,int(ts),o,h,l,c,v,int(bool(complete)),source))
    with db:
        db.executemany("""INSERT INTO major_ohlcv_1m(symbol,ts,open,high,low,close,volume,complete,source)
          VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,ts) DO UPDATE SET
          open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,
          complete=excluded.complete,source=excluded.source""",vals)
        db.execute("DELETE FROM major_ohlcv_1m WHERE ts<?",(int(now)-3*86400,))


def completed_series(db,symbol:str,now:float,minutes:int=720)->list[tuple[int,float]]:
    rows=db.execute("""SELECT ts,close FROM major_ohlcv_1m
      WHERE symbol=? AND complete=1 AND ts<=? AND ts>=? ORDER BY ts""",
      (symbol,int(now),int(now)-(minutes+2)*60)).fetchall()
    return [(int(r["ts"]),finite_pos(r["close"])) for r in rows]


def _anchor(series:list[tuple[int,float]],target_ts:int):
    for ts,p in reversed(series):
        if ts<=target_ts:return p
    return None


def returns_from_db(db,now:float)->dict[str,dict[str,float|None]]:
    out={}
    for symbol in COINS:
        s=completed_series(db,symbol,now,minutes=70)
        if not s:
            out[symbol]={h:None for h in ("1m","5m","15m","60m")};continue
        latest_ts,cur=s[-1];d={}
        for label,m in (("1m",1),("5m",5),("15m",15),("60m",60)):
            old=_anchor(s,latest_ts-m*60)
            d[label]=(cur/old-1.0) if old else None
        out[symbol]=d
    return out


def _breadth(returns,h):
    vals=[returns[s][h] for s in COINS if returns[s][h] is not None]
    return sum(v>0 for v in vals) if len(vals)==len(COINS) else None


@dataclass(frozen=True)
class RegimeResult:
    regime:str; shock_direction:str|None; confidence:float
    breadth_5m:int|None; breadth_15m:int|None; breadth_60m:int|None
    btc_1m:float|None; btc_5m:float|None; btc_15m:float|None; btc_60m:float|None
    returns:dict[str,dict[str,float|None]]


def classify(returns)->RegimeResult:
    for s in COINS:
        if s not in returns: raise ValueError("missing major coin")
    b5,b15,b60=(_breadth(returns,h) for h in ("5m","15m","60m"))
    btc=returns["BTC"];btc1=btc.get("1m");btc5=btc.get("5m");btc15=btc.get("15m");btc60=btc.get("60m")
    if any(v is None for v in (b5,b15,b60,btc5,btc15,btc60)):
        return RegimeResult("NEUTRAL",None,.20,b5,b15,b60,btc1,btc5,btc15,btc60,returns)
    # Candidate thresholds: research parameters, not claims of optimality.
    shock_down=((btc1 is not None and btc1<=-.012) or btc5<=-.025) and b5<=1
    shock_up=((btc1 is not None and btc1>=.012) or btc5>=.025) and b5>=4
    if shock_down or shock_up:
        return RegimeResult("SHOCK","DOWN" if shock_down else "UP",.90,b5,b15,b60,btc1,btc5,btc15,btc60,returns)
    risk_on=btc15>=.004 and btc60>=.008 and b15>=4 and b60>=3
    risk_off=btc15<=-.004 and btc60<=-.008 and b15<=1 and b60<=2
    if risk_on:
        conf=min(.90,.60+.05*max(0,b15-3)+min(.15,abs(btc60)*5));reg="RISK_ON"
    elif risk_off:
        conf=min(.90,.60+.05*max(0,2-b15)+min(.15,abs(btc60)*5));reg="RISK_OFF"
    else: conf=.55;reg="NEUTRAL"
    return RegimeResult(reg,None,conf,b5,b15,b60,btc1,btc5,btc15,btc60,returns)


def variant_for_chain(result:RegimeResult,chain:str)->str:
    if result.regime=="SHOCK":return "halt"
    if result.regime=="RISK_OFF":return "conservative"
    if result.regime=="NEUTRAL":return "balanced"
    key=str(chain).lower();leader=CHAIN_LEADER.get(key)
    # Never infer an aggressive posture when the chain leader is unknown/unobserved.
    if key not in CHAIN_LEADER or leader is None:return "balanced"
    lead15=result.returns.get(leader,{}).get("15m")
    if lead15 is None or lead15<=0:return "balanced"
    return "aggressive"


def _returns(series:Sequence[tuple[int,float]])->dict[int,float]:
    out={};prev=None
    for ts,p in series:
        p=finite_pos(p)
        if prev is not None: out[int(ts)]=p/prev-1
        prev=p
    return out


def _pearson(xs,ys):
    n=len(xs)
    if n<3:return None
    mx=sum(xs)/n;my=sum(ys)/n
    vx=sum((x-mx)**2 for x in xs);vy=sum((y-my)**2 for y in ys)
    if vx<=0 or vy<=0:return None
    return sum((x-mx)*(y-my) for x,y in zip(xs,ys))/math.sqrt(vx*vy)


def _beta(xs,ys):
    n=len(xs)
    if n<3:return None
    mx=sum(xs)/n;my=sum(ys)/n;var=sum((x-mx)**2 for x in xs)
    if var<=0:return None
    return sum((x-mx)*(y-my) for x,y in zip(xs,ys))/var


@dataclass(frozen=True)
class LeadLagResult:
    target:str; best_lag_min:int|None; best_corr:float|None; corr_lag0:float|None; beta:float|None
    n:int; stable_lag:bool; btc_impulse_1m:float|None; target_same_minute:float|None
    lag_candidate:bool; reason:str


def lead_lag_from_series(btc_series,target_series,target:str,max_lag:int=5,min_n:int=60)->LeadLagResult:
    br=_returns(btc_series);tr=_returns(target_series);common=sorted(set(br)&set(tr))
    if len(common)<min_n+max_lag:
        return LeadLagResult(target,None,None,None,None,len(common),False,None,None,False,"INSUFFICIENT_HISTORY")
    common=common[-360:];corr_by={};beta_by={};n_by={};tr_keys=set(tr)
    for lag in range(max_lag+1):
        xs=[];ys=[];shift=lag*60
        for t in common:
            if t+shift in tr_keys: xs.append(br[t]);ys.append(tr[t+shift])
        corr_by[lag]=_pearson(xs,ys);beta_by[lag]=_beta(xs,ys);n_by[lag]=len(xs)
    valid=[(lag,c) for lag,c in corr_by.items() if c is not None and n_by[lag]>=min_n]
    if not valid:return LeadLagResult(target,None,None,corr_by.get(0),None,len(common),False,None,None,False,"NO_VALID_CORRELATION")
    best_lag,best_corr=max(valid,key=lambda x:x[1]);corr0=corr_by.get(0)
    stable=(best_lag>=1 and best_corr>=.15 and corr0 is not None and best_corr>=corr0+.03)
    latest=common[-1];impulse=br.get(latest);target_now=tr.get(latest);beta=beta_by.get(best_lag)
    hist=[abs(br[t]) for t in common[-61:-1] if t in br]
    threshold=max(.0025,2.5*statistics.median(hist)) if hist else .0025
    candidate=False;reason="NO_STABLE_LAG"
    if stable:
        reason="NO_BTC_IMPULSE"
        if impulse is not None and abs(impulse)>=threshold and beta is not None and beta>0:
            expected=beta*impulse
            under=(target_now is not None and ((expected>0 and target_now<expected*.5) or (expected<0 and target_now>expected*.5)))
            if under:candidate=True;reason="BTC_IMPULSE_TARGET_UNDERREACTED"
            else:reason="TARGET_ALREADY_REACTED_OR_DIVERGED"
    return LeadLagResult(target,best_lag,best_corr,corr0,beta,n_by.get(best_lag,0),stable,impulse,target_now,candidate,reason)


def lead_lag_snapshot(db,now:float):
    series={s:completed_series(db,s,now,minutes=420) for s in COINS}
    return {s:asdict(lead_lag_from_series(series["BTC"],series[s],s)) for s in ("ETH","XRP","SOL","BNB")}


def save_state(db,ts,result:RegimeResult,leadlag=None):
    raw=json.dumps(result.returns,separators=(",",":"),allow_nan=False)
    lagraw=json.dumps({} if leadlag is None else leadlag,separators=(",",":"),allow_nan=False)
    with db:db.execute("""INSERT INTO market_regime_states(ts,regime,shock_direction,confidence,breadth_5m,breadth_15m,breadth_60m,
      btc_1m,btc_5m,btc_15m,btc_60m,returns_json,lead_lag_json,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (ts,result.regime,result.shock_direction,result.confidence,result.breadth_5m,result.breadth_15m,result.breadth_60m,
       result.btc_1m,result.btc_5m,result.btc_15m,result.btc_60m,raw,lagraw,SOURCE))


def latest_result_from_db(db,max_age:float=180.0,now:float|None=None)->RegimeResult|None:
    row=db.execute("SELECT * FROM market_regime_states ORDER BY ts DESC,id DESC LIMIT 1").fetchone()
    if not row:return None
    now=time.time() if now is None else float(now)
    if now-float(row["ts"])>max_age or float(row["ts"])>now+5:return None
    try:
        returns=json.loads(row["returns_json"])
        return RegimeResult(row["regime"],row["shock_direction"],float(row["confidence"]),row["breadth_5m"],row["breadth_15m"],row["breadth_60m"],row["btc_1m"],row["btc_5m"],row["btc_15m"],row["btc_60m"],returns)
    except (ValueError,TypeError,KeyError,json.JSONDecodeError):return None


def collect_market(db,now:float,bootstrap_limit:int=500,recent_limit:int=8):
    now_ms=int(now*1000);written=0
    for major in COINS:
        n=db.execute("SELECT COUNT(*) FROM major_ohlcv_1m WHERE symbol=? AND complete=1",(major,)).fetchone()[0]
        limit=bootstrap_limit if n<90 else recent_limit
        venue,rows=fetch_major_klines(major,limit=limit,now_ms=now_ms)
        save_symbol_bars(db,major,rows,now,source=f"{SOURCE}:{venue}");written+=len(rows)
        time.sleep(.03)
    return written


def cycle(db,now=None,collector=True):
    now=time.time() if now is None else float(now)
    if collector:collect_market(db,now)
    returns=returns_from_db(db,now);result=classify(returns);leadlag=lead_lag_snapshot(db,now);save_state(db,now,result,leadlag)
    emit("MARKET_REGIME_OK",regime=result.regime,shock_direction=result.shock_direction,confidence=round(result.confidence,3),
         breadth_5m=result.breadth_5m,breadth_15m=result.breadth_15m,breadth_60m=result.breadth_60m,
         btc_1m=result.btc_1m,btc_5m=result.btc_5m,btc_15m=result.btc_15m,btc_60m=result.btc_60m,
         source=SOURCE,lead_lag={s:{k:v for k,v in d.items() if k in ("best_lag_min","best_corr","corr_lag0","stable_lag","lag_candidate","reason")} for s,d in leadlag.items()},no_trade=True)
    return result,leadlag


def main():
    data=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"));data.mkdir(parents=True,exist_ok=True)
    db=dbopen(data/"discovery.sqlite3")
    try:
        while True:
            started=time.monotonic()
            try:cycle(db)
            except Exception as exc:emit("MARKET_REGIME_ERROR",error_type=type(exc).__name__,error=str(exc)[:180])
            time.sleep(max(5,15-(time.monotonic()-started)))
    finally:db.close()

if __name__=="__main__":main()
