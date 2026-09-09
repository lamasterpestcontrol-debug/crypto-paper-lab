"""GMGN read-only early-token intelligence worker.

Uses the official gmgn-cli read-only market interface when GMGN_API_KEY is present.
No swap/order commands are invoked and GMGN_PRIVATE_KEY is neither required nor read.
Trenches observations are normalized into the shared SQLite database so the unified
Decision Engine can use Smart Money / insider / Top10 / rug / wash-trading evidence.

The worker fails safe when the API key or CLI is unavailable: it keeps running,
emits an explicit disabled/degraded event, and never fabricates zero-risk values.
"""
from __future__ import annotations
import json, math, os, shutil, sqlite3, subprocess, time
from pathlib import Path
from typing import Any

VERSION="gmgn-intel-0.1.0"
CLI=os.getenv("GMGN_CLI","gmgn-cli")
CYCLE_SECONDS=max(15.0,float(os.getenv("GMGN_INTEL_CYCLE_SECONDS","30")))
LIMIT=max(5,min(60,int(os.getenv("GMGN_INTEL_LIMIT","30"))))
CHAINS=("sol","bsc","base","eth","robinhood")
CHAIN_MAP={"sol":"solana","bsc":"bsc","base":"base","eth":"ethereum","robinhood":"robinhood","arc":"arc","stable":"stable"}


def emit(event,**fields):
    print(json.dumps({"event":event,"version":VERSION,**fields},separators=(",",":"),allow_nan=False),flush=True)


def _num(x,default=None):
    try:
        v=float(x)
        return v if math.isfinite(v) else default
    except (TypeError,ValueError,OverflowError):
        return default


def _bool(x):
    if isinstance(x,bool):return x
    if x in (1,"1","true","True","yes","YES"):return True
    if x in (0,"0","false","False","no","NO"):return False
    return None


def dbopen(path:Path):
    db=sqlite3.connect(path,timeout=30);db.row_factory=sqlite3.Row;db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS gmgn_discovery_candidates(
      chain TEXT NOT NULL,address TEXT NOT NULL,symbol TEXT,name TEXT,stage TEXT,launchpad TEXT,
      first_seen REAL NOT NULL,last_seen REAL NOT NULL,sighting_streak INTEGER NOT NULL DEFAULT 1,
      created_at REAL,market_cap REAL,liquidity REAL,smart_money_count INTEGER,kol_count INTEGER,
      top10_rate REAL,insider_rate REAL,bundler_rate REAL,rug_ratio REAL,wash_trading INTEGER,
      contract_risk_pass INTEGER,priority REAL NOT NULL,raw_json TEXT NOT NULL,
      PRIMARY KEY(chain,address));
    CREATE INDEX IF NOT EXISTS idx_gmgn_candidate_seen ON gmgn_discovery_candidates(last_seen,priority);
    CREATE TABLE IF NOT EXISTS token_intel_signals(
      id INTEGER PRIMARY KEY,ts REAL NOT NULL,chain TEXT NOT NULL,address TEXT NOT NULL,source TEXT NOT NULL,
      smart_money_score REAL,insider_risk REAL,top10_pct REAL,contract_risk_pass INTEGER,independent_catalyst INTEGER,
      raw_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_token_intel_key_ts ON token_intel_signals(chain,address,ts);
    """);db.commit();return db


def _json_from_stdout(text:str):
    s=str(text or "").strip()
    if not s:raise ValueError("gmgn-cli returned empty output")
    try:return json.loads(s)
    except json.JSONDecodeError:
        # --raw should be one-line JSON, but tolerate harmless CLI notices before it.
        for line in reversed(s.splitlines()):
            line=line.strip()
            if not line:continue
            try:return json.loads(line)
            except json.JSONDecodeError:continue
    raise ValueError("gmgn-cli returned non-JSON output")


def run_cli(args:list[str],timeout=22):
    if not os.getenv("GMGN_API_KEY"):
        raise RuntimeError("GMGN_API_KEY_MISSING")
    if not shutil.which(CLI):
        raise RuntimeError("GMGN_CLI_MISSING")
    env=os.environ.copy();env.pop("GMGN_PRIVATE_KEY",None)  # read-only by construction
    cp=subprocess.run([CLI,*args,"--raw"],capture_output=True,text=True,timeout=timeout,env=env,check=False)
    if cp.returncode!=0:
        msg=(cp.stderr or cp.stdout or "gmgn-cli failed").strip().replace("\n"," ")[:240]
        raise RuntimeError(f"GMGN_CLI_ERROR:{cp.returncode}:{msg}")
    out=_json_from_stdout(cp.stdout)
    if not isinstance(out,(dict,list)):raise ValueError("GMGN response wrong type")
    return out


def iter_tokens(payload:Any):
    """Yield token-like dicts from the CLI's grouped Trenches response without guessing a fixed wrapper."""
    seen=set()
    def walk(x,stage=None):
        if isinstance(x,dict):
            local_stage=stage
            for k in ("new_creation","near_completion","completed","new","pump"):
                if k in x and isinstance(x[k],(dict,list)):
                    yield from walk(x[k],k)
            addr=x.get("address") or x.get("token_address")
            if addr and isinstance(addr,str):
                key=(str(addr),str(x.get("chain") or ""))
                if key not in seen:
                    seen.add(key);yield local_stage,x
            for k,v in x.items():
                if k not in {"new_creation","near_completion","completed","new","pump"} and isinstance(v,(dict,list)):
                    yield from walk(v,local_stage)
        elif isinstance(x,list):
            for y in x:yield from walk(y,stage)
    yield from walk(payload)


def risk_metrics(item:dict[str,Any]):
    smart=max(0,int(_num(item.get("smart_degen_count"),0) or 0));kol=max(0,int(_num(item.get("renowned_count"),0) or 0))
    top10=_num(item.get("top_10_holder_rate"));insider=_num(item.get("suspected_insider_hold_rate"))
    if insider is None:insider=_num(item.get("rat_trader_amount_rate"))
    bundler=_num(item.get("bundler_trader_amount_rate"));
    if bundler is None:bundler=_num(item.get("bundler_rate"))
    rug=_num(item.get("rug_ratio"));wash=_bool(item.get("is_wash_trading"));honeypot=_bool(item.get("is_honeypot"))
    dev=_num(item.get("creator_balance_rate"));snipers=max(0,int(_num(item.get("sniper_count"),0) or 0))
    risks=[v for v in (top10,insider,bundler,rug,dev) if v is not None]
    insider_risk=max([0.0]+[min(100.0,max(0.0,v*100.0)) for v in (insider,bundler,dev) if v is not None])
    if wash is True:insider_risk=max(insider_risk,90.0)
    if snipers>=20:insider_risk=max(insider_risk,min(85.0,55.0+snipers))
    explicit_fail=(wash is True or honeypot is True or (rug is not None and rug>.30) or
                   (top10 is not None and top10>.50) or (insider is not None and insider>.30) or
                   (bundler is not None and bundler>.30))
    has_safety=any(v is not None for v in (rug,top10,insider,bundler)) or wash is not None or honeypot is not None
    contract_pass=False if explicit_fail else (True if has_safety else None)
    smart_score=min(100.0,smart*20.0+kol*6.0)
    if rug is not None:smart_score+=max(-20.0,min(10.0,(.20-rug)*50.0))
    if wash is True:smart_score-=30
    smart_score=max(0.0,min(100.0,smart_score))
    priority=smart_score + min(25.0,math.log10(max(_num(item.get("volume_1h"),0) or 0,1))*5.0)
    return {"smart":smart,"kol":kol,"top10":top10,"insider":insider,"bundler":bundler,"rug":rug,"wash":wash,
            "smart_score":smart_score,"insider_risk":insider_risk,"contract_pass":contract_pass,"priority":priority}


def save_token(db,gmgn_chain,stage,item,now):
    chain=CHAIN_MAP.get(gmgn_chain,gmgn_chain);address=str(item.get("address") or item.get("token_address") or "").strip()
    if len(address)<8:return False
    m=risk_metrics(item);symbol=str(item.get("symbol") or "?")[:64];name=str(item.get("name") or "")[:160]
    prev=db.execute("SELECT last_seen,sighting_streak FROM gmgn_discovery_candidates WHERE chain=? AND address=?",(chain,address)).fetchone()
    if prev:
        gap=now-float(prev["last_seen"]);streak=int(prev["sighting_streak"])+1 if 8<=gap<=180 else int(prev["sighting_streak"])
        first=None
    else:streak=1;first=now
    raw=json.dumps({"gmgn_chain":gmgn_chain,"stage":stage,"item":item},separators=(",",":"),allow_nan=False)
    contract=None if m["contract_pass"] is None else int(bool(m["contract_pass"]))
    with db:
        db.execute("""INSERT INTO gmgn_discovery_candidates(chain,address,symbol,name,stage,launchpad,first_seen,last_seen,sighting_streak,
          created_at,market_cap,liquidity,smart_money_count,kol_count,top10_rate,insider_rate,bundler_rate,rug_ratio,wash_trading,
          contract_risk_pass,priority,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(chain,address) DO UPDATE SET symbol=excluded.symbol,name=excluded.name,stage=excluded.stage,launchpad=excluded.launchpad,
          last_seen=excluded.last_seen,sighting_streak=excluded.sighting_streak,created_at=COALESCE(excluded.created_at,gmgn_discovery_candidates.created_at),
          market_cap=excluded.market_cap,liquidity=excluded.liquidity,smart_money_count=excluded.smart_money_count,kol_count=excluded.kol_count,
          top10_rate=excluded.top10_rate,insider_rate=excluded.insider_rate,bundler_rate=excluded.bundler_rate,rug_ratio=excluded.rug_ratio,
          wash_trading=excluded.wash_trading,contract_risk_pass=excluded.contract_risk_pass,priority=excluded.priority,raw_json=excluded.raw_json""",
          (chain,address,symbol,name,str(stage or "unknown"),str(item.get("launchpad_platform") or "")[:80],first or db.execute("SELECT first_seen FROM gmgn_discovery_candidates WHERE chain=? AND address=?",(chain,address)).fetchone()[0],now,streak,
           _num(item.get("created_timestamp")),_num(item.get("usd_market_cap")),_num(item.get("liquidity")),m["smart"],m["kol"],m["top10"],m["insider"],m["bundler"],m["rug"],None if m["wash"] is None else int(m["wash"]),contract,m["priority"],raw))
        # At most one normalized intelligence row per token per 20 seconds.
        recent=db.execute("SELECT 1 FROM token_intel_signals WHERE source='GMGN' AND chain=? AND address=? AND ts>=? LIMIT 1",(chain,address,now-20)).fetchone()
        if not recent:
            db.execute("""INSERT INTO token_intel_signals(ts,chain,address,source,smart_money_score,insider_risk,top10_pct,contract_risk_pass,independent_catalyst,raw_json)
              VALUES(?,?,?,?,?,?,?,?,?,?)""",(now,chain,address,"GMGN",m["smart_score"],m["insider_risk"],None if m["top10"] is None else m["top10"]*100.0,contract,0,raw))
    return True


def fetch_chain(chain):
    return run_cli(["market","trenches","--chain",chain,"--type","new_creation","--type","near_completion",
                    "--filter-preset","safe","--sort-by","smart_degen_count","--limit",str(LIMIT)])


def cycle(db,now=None,chains=CHAINS):
    now=time.time() if now is None else float(now)
    if not os.getenv("GMGN_API_KEY"):
        emit("GMGN_INTEL_DISABLED",reason="GMGN_API_KEY_MISSING",read_only=True,no_trade=True)
        return {"enabled":False,"observed":0,"errors":0}
    observed=0;errors=0;by_chain={}
    for ch in chains:
        try:
            body=fetch_chain(ch);n=0
            for stage,item in iter_tokens(body):
                if save_token(db,ch,stage,item,now):n+=1
            observed+=n;by_chain[ch]=n
        except Exception as exc:
            errors+=1;by_chain[ch]="ERROR";emit("GMGN_CHAIN_ERROR",chain=ch,error_type=type(exc).__name__,error=str(exc)[:200],read_only=True)
    # Keep bounded history/current candidates. Do not erase token_intel history needed for audit.
    with db:db.execute("DELETE FROM gmgn_discovery_candidates WHERE last_seen<?",(now-24*3600,))
    emit("GMGN_INTEL_CYCLE_OK" if not errors else "GMGN_INTEL_CYCLE_DEGRADED",observed=observed,errors=errors,by_chain=by_chain,read_only=True,no_trade=True)
    return {"enabled":True,"observed":observed,"errors":errors,"by_chain":by_chain}


def main():
    data=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"));data.mkdir(parents=True,exist_ok=True)
    db=dbopen(data/"discovery.sqlite3")
    try:
        while True:
            started=time.monotonic()
            try:cycle(db)
            except Exception as exc:emit("GMGN_INTEL_CYCLE_ERROR",error_type=type(exc).__name__,error=str(exc)[:200],read_only=True)
            time.sleep(max(5,CYCLE_SECONDS-(time.monotonic()-started)))
    finally:db.close()

if __name__=="__main__":main()
