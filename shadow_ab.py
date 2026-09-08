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
from strategy_v05 import Evidence, classify as prediscovery_classify
from strategy_v06 import SignalPoint, persistence_check
from strategy_v07 import FastInput, fast_route

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
      prediscovery_stage TEXT,
      prediscovery_score REAL,
      persistence_streak INTEGER,
      persistence_required INTEGER,
      persistence_confirmed INTEGER,
      fast_route TEXT,
      fast_priority REAL,
      deep_analysis INTEGER,
      raw_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_ab_token_ts
      ON strategy_ab_observations(chain,address,ts);
    CREATE TABLE IF NOT EXISTS strategy_ab_meta(
      k TEXT PRIMARY KEY,v TEXT NOT NULL
    );
    """)
    # Backward-compatible schema migrations for existing Railway volume.
    cols={r[1] for r in db.execute("PRAGMA table_info(strategy_ab_observations)")}
    for name,typ in [
        ("prediscovery_stage","TEXT"),
        ("prediscovery_score","REAL"),
        ("persistence_streak","INTEGER"),
        ("persistence_required","INTEGER"),
        ("persistence_confirmed","INTEGER"),
        ("fast_route","TEXT"),
        ("fast_priority","REAL"),
        ("deep_analysis","INTEGER"),
    ]:
        if name not in cols:
            db.execute(f"ALTER TABLE strategy_ab_observations ADD COLUMN {name} {typ}")
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


def prediscovery_from_pair(pair, now):
    info=pair.get("info") or {}
    websites=info.get("websites") or []
    socials=info.get("socials") or []
    h1=(pair.get("txns") or {}).get("h1") or {}
    liq=num((pair.get("liquidity") or {}).get("usd"))
    volh1=num((pair.get("volume") or {}).get("h1"))
    created=num(pair.get("pairCreatedAt"))
    age_h=((now*1000-created)/3_600_000) if created>0 else None
    # These are conservative proxies from currently available public feed.
    # They are explicitly not treated as real GitHub/product/on-chain identity verification.
    e=Evidence(
        ts=now,
        github_activity=0.0,
        product_live=.25 if websites else 0.0,
        ecosystem_support=0.0,
        contract_deployed=1.0,
        first_pool=1.0 if liq>0 else 0.0,
        onchain_adoption=min(1.0,volh1/50_000.0) if volh1>0 else 0.0,
        social_early_growth=.25 if socials else 0.0,
        catalyst=0.0,
        official_identity=.5 if websites else .2,
        independent_sources=1 + (1 if websites else 0),
        paid_promo_risk=0.0,
        age_hours=age_h,
    )
    return e,prediscovery_classify(e)

def persistence_for_token(db,chain,address,now,current_score,risk_pass,exit_pass,liq_pass):
    rows=db.execute("""SELECT ts,challenger_score,challenger_state
        FROM strategy_ab_observations
        WHERE chain=? AND address=? ORDER BY id DESC LIMIT 5""",(chain,address)).fetchall()
    pts=[]
    for r in reversed(rows):
        pts.append(SignalPoint(
            ts=r["ts"],
            score=float(r["challenger_score"] or 0),
            risk_pass=(r["challenger_state"]!="REJECT"),
            exit_pass=(r["challenger_state"]!="REJECT"),
            liquidity_pass=(r["challenger_state"]!="REJECT"),
        ))
    pts.append(SignalPoint(now,float(current_score or 0),risk_pass,exit_pass,liq_pass))
    return persistence_check(pts,required=3,min_score=1.0)


def fast_route_from_pair(pair, now):
    h1=(pair.get("txns") or {}).get("h1") or {}
    liq=num((pair.get("liquidity") or {}).get("usd"))
    volh1=num((pair.get("volume") or {}).get("h1"))
    created=num(pair.get("pairCreatedAt"))
    age_m=((now*1000-created)/60_000) if created>0 else 999999
    x=FastInput(
        liquidity_usd=liq,
        volume_h1_usd=volh1,
        buys_h1=int(num(h1.get("buys"))),
        sells_h1=int(num(h1.get("sells"))),
        age_minutes=age_m,
        basic_risk_block=False,
    )
    return fast_route(x)

def evaluate_one(db,chain,address,symbol,pair,now):
    cur=mk_snapshot(pair,now)
    route=fast_route_from_pair(pair,now)
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
    pre_e,pre_r=prediscovery_from_pair(pair,now)
    pers=persistence_for_token(
        db,chain,address,now,d.score,
        risk_pass=(state!="REJECT"),
        exit_pass=slip.pass_check,
        liq_pass=(cur.liquidity>0),
    )
    raw={
        "audit":d.audit,
        "slippage":slip.__dict__,
        "prediscovery":{"evidence":pre_e.__dict__,"result":pre_r.__dict__,
                        "note":"CURRENTLY_PROXY_INPUTS_ONLY_NOT_FULL_REAL_DATA"},
        "persistence":pers.__dict__,
        "fast_route":route.__dict__,
        "mode":"SHADOW_ONLY",
    }
    with db:
        db.execute("""INSERT INTO strategy_ab_observations(
          ts,chain,address,symbol,price,liquidity,volume_h1,volume_h24,buys_h1,sells_h1,
          control_state,challenger_state,challenger_reason,challenger_score,max_safe_usd,regime,
          prediscovery_stage,prediscovery_score,persistence_streak,persistence_required,persistence_confirmed,
          fast_route,fast_priority,deep_analysis,raw_json)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (now,chain,address,symbol,cur.price,cur.liquidity,cur.volume_h1,cur.volume_h24,
           cur.buys_h1,cur.sells_h1,control_state(pair),state,reason,d.score,
           min(d.max_safe_usd,slip.max_safe_usd),regime,
           pre_r.stage,pre_r.score,pers.streak,pers.required,1 if pers.confirmed else 0,
           route.route,route.priority,1 if route.deep_analysis else 0,
           json.dumps(raw,separators=(",",":"))))
    return {"chain":chain,"symbol":symbol,"control":control_state(pair),"challenger":state,
            "reason":reason,"pre":pre_r.stage,"persistence":f"{pers.streak}/{pers.required}",
            "fast_route":route.route,"priority":round(route.priority,1)}

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
