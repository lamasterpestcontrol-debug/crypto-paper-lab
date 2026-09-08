"""Shadow A/B evaluator for crypto-paper-lab.

Runs beside discovery.py inside the same Railway service.
It does NOT place orders and does NOT alter the existing paper strategy.
It observes the same public DexScreener feed and writes shadow decisions
for CONTROL vs CHALLENGER (strategy v0.3 + v0.4) into SQLite.

This isolates the experiment so production paper behavior is preserved.
"""
from __future__ import annotations
import json, os, sqlite3, time, urllib.request, urllib.parse
from pathlib import Path
from typing import Any
from strategy_v03 import MarketSnapshot, build_decision
from strategy_v04 import estimate_slippage, classify_regime, RegimeInput, regime_position_multiplier

DEX="https://api.dexscreener.com"
UA="crypto-paper-lab-shadow-ab/0.1"
SUPPORTED={"solana","ethereum","base","bsc","arbitrum","polygon"}

def get_json(path:str)->Any:
    req=urllib.request.Request(DEX+path,headers={"User-Agent":UA,"Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=12) as r:
        raw=r.read(3_000_001)
        if len(raw)>3_000_000: raise ValueError("oversize")
    return json.loads(raw)

def num(x,d=0.0):
    try:return float(x)
    except Exception:return d

def dbopen(path:Path):
    db=sqlite3.connect(path,timeout=30)
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS strategy_ab_observations(
      id INTEGER PRIMARY KEY,
      ts REAL NOT NULL,
      chain TEXT NOT NULL,
      address TEXT NOT NULL,
      symbol TEXT,
      price REAL,
      liquidity REAL,
      volume_h1 REAL,
      volume_h24 REAL,
      buys_h1 INTEGER,
      sells_h1 INTEGER,
      control_state TEXT,
      challenger_state TEXT,
      challenger_reason TEXT,
      challenger_score REAL,
      max_safe_usd REAL,
      regime TEXT,
      raw_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_ab_token_ts
      ON strategy_ab_observations(chain,address,ts);
    CREATE TABLE IF NOT EXISTS strategy_ab_meta(
      k TEXT PRIMARY KEY,v TEXT NOT NULL
    );
    """)
    db.commit()
    return db

def latest_prev(db,chain,address):
    return db.execute("""SELECT ts,price,liquidity,volume_h1,volume_h24,buys_h1,sells_h1
      FROM strategy_ab_observations WHERE chain=? AND address=? ORDER BY id DESC LIMIT 1""",
      (chain,address)).fetchone()

def pair_for(chain,address):
    raw=get_json(f"/token-pairs/v1/{chain}/{address}")
    if not isinstance(raw,list): return None
    matches=[]
    for p in raw:
        if not isinstance(p,dict): continue
        base=p.get("baseToken") or {}
        if str(base.get("address","")).lower()!=address.lower(): continue
        if not p.get("priceUsd"): continue
        matches.append(p)
    if not matches:return None
    return max(matches,key=lambda p:num((p.get("liquidity") or {}).get("usd")))

def mk_snapshot(pair,ts):
    h1=(pair.get("txns") or {}).get("h1") or {}
    vol=pair.get("volume") or {}
    pc=pair.get("priceChange") or {}
    return MarketSnapshot(
      ts=ts,price=num(pair.get("priceUsd")),
      liquidity=num((pair.get("liquidity") or {}).get("usd")),
      volume_h1=num(vol.get("h1")),volume_h24=num(vol.get("h24")),
      buys_h1=int(num(h1.get("buys"))),sells_h1=int(num(h1.get("sells"))),
      price_change_h1_pct=num(pc.get("h1"))
    )

def mk_prev(row):
    if not row:return None
    return MarketSnapshot(
      ts=row["ts"],price=row["price"] or 0,liquidity=row["liquidity"] or 0,
      volume_h1=row["volume_h1"] or 0,volume_h24=row["volume_h24"] or 0,
      buys_h1=row["buys_h1"] or 0,sells_h1=row["sells_h1"] or 0
    )

def control_state(pair):
    liq=num((pair.get("liquidity") or {}).get("usd"))
    vol=num((pair.get("volume") or {}).get("h24"))
    mc=num(pair.get("marketCap")) or num(pair.get("fdv"))
    h1=(pair.get("txns") or {}).get("h1") or {}
    buys,sells=int(num(h1.get("buys"))),int(num(h1.get("sells")))
    # Shadow approximation only; existing discovery.py remains authoritative control.
    return "ELIGIBLE" if (50000<=mc<=5_000_000 and liq>=30_000 and vol>=20_000 and buys>=5 and buys>=max(1,sells)*1.1) else "WATCH"

def synthetic_regime():
    # Until BTC regime feed is wired, remain conservative and explicit.
    return "NEUTRAL"

def evaluate_one(db,chain,address,symbol,pair,now):
    cur=mk_snapshot(pair,now)
    prev=mk_prev(latest_prev(db,chain,address))
    first= db.execute("""SELECT price,MAX(price) FROM strategy_ab_observations
      WHERE chain=? AND address=?""",(chain,address)).fetchone()
    first_price=(first[0] if first and first[0] else cur.price)
    high=(first[1] if first and first[1] else cur.price)
    high=max(high or 0,cur.price)
    invalidation=max(cur.price*0.70, first_price*0.70) if cur.price>0 else 0
    intended=100.0
    d=build_decision(cur,prev,intended,first_price,high,invalidation,now)
    regime=synthetic_regime()
    intended*=regime_position_multiplier(regime)
    slip=estimate_slippage(cur.liquidity,intended)
    state=d.state
    reason=d.reason
    if not slip.pass_check:
        state="REJECT"; reason="SLIPPAGE_TOO_HIGH"
    raw={"audit":d.audit,"slippage":slip.__dict__,"mode":"SHADOW_ONLY"}
    with db:
        db.execute("""INSERT INTO strategy_ab_observations(
          ts,chain,address,symbol,price,liquidity,volume_h1,volume_h24,buys_h1,sells_h1,
          control_state,challenger_state,challenger_reason,challenger_score,max_safe_usd,regime,raw_json)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (now,chain,address,symbol,cur.price,cur.liquidity,cur.volume_h1,cur.volume_h24,
           cur.buys_h1,cur.sells_h1,control_state(pair),state,reason,d.score,
           min(d.max_safe_usd,slip.max_safe_usd),regime,json.dumps(raw,separators=(",",":"))))
    return {"chain":chain,"symbol":symbol,"control":control_state(pair),"challenger":state,"reason":reason}

def cycle(db):
    now=time.time()
    profiles=get_json("/token-profiles/latest/v1")
    if not isinstance(profiles,list): return {"observed":0}
    n=0; decisions=[]
    for p in profiles[:50]:
        if not isinstance(p,dict):continue
        chain=str(p.get("chainId") or "").lower()
        addr=str(p.get("tokenAddress") or "")
        if chain not in SUPPORTED or len(addr)<20: continue
        try:
            pair=pair_for(chain,addr)
            if not pair:continue
            symbol=str((pair.get("baseToken") or {}).get("symbol") or "?")
            decisions.append(evaluate_one(db,chain,addr,symbol,pair,now))
            n+=1
            if n>=20:break
            time.sleep(.25)
        except Exception:
            continue
    with db:
        db.execute("INSERT OR REPLACE INTO strategy_ab_meta(k,v) VALUES('last_cycle',?)",
                   (json.dumps({"ts":now,"observed":n},separators=(",",":")),))
    print(json.dumps({"event":"STRATEGY_AB_CYCLE_OK","observed":n,"sample":decisions[:3]},
                     separators=(",",":")),flush=True)
    return {"observed":n}

def main():
    data=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"))
    data.mkdir(parents=True,exist_ok=True)
    db=dbopen(data/"discovery.sqlite3")
    while True:
        started=time.monotonic()
        try: cycle(db)
        except Exception as e:
            print(json.dumps({"event":"STRATEGY_AB_CYCLE_ERROR","error":f"{type(e).__name__}: {str(e)[:160]}"}),flush=True)
        time.sleep(max(5,60-(time.monotonic()-started)))

if __name__=="__main__":
    main()
