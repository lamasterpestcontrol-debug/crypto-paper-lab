"""Shadow A/B evaluator for crypto-paper-lab.

Runs beside discovery.py inside the same Railway service.
It does NOT place orders and does NOT alter the existing paper strategy.
It observes the same public DexScreener feed and writes shadow decisions
for CONTROL vs CHALLENGER (strategy v0.3 + v0.4) into SQLite.

This isolates the experiment so production paper behavior is preserved.
"""
from __future__ import annotations
import json, math, os, sqlite3, time, urllib.request, urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any
from strategy_v03 import (MarketSnapshot, build_decision, exit_first_ok,
                          data_freshness_ok, pullback_state)
from strategy_v04 import estimate_slippage, classify_regime, RegimeInput, regime_position_multiplier
from strategy_v05 import Evidence, classify as prediscovery_classify
from strategy_v06 import SignalPoint, persistence_check
from strategy_v07 import FastInput, fast_route

DEX="https://api.dexscreener.com"
UA="crypto-paper-lab-shadow-ab/0.2"
DIAG_VERSION="persistence-diag-0.2.0"
EVM_CHAINS={"ethereum","base","bsc","arbitrum","polygon"}
SUPPORTED={"solana","ethereum","base","bsc","arbitrum","polygon"}

def get_json(path:str)->Any:
    req=urllib.request.Request(DEX+path,headers={"User-Agent":UA,"Accept":"application/json"})
    with urllib.request.urlopen(req,timeout=12) as r:
        raw=r.read(3_000_001)
        if len(raw)>3_000_000: raise ValueError("oversize")
    return json.loads(raw)

def num(x, d=0.0):
    try:
        value=float(x)
        return value if math.isfinite(value) else d
    except (TypeError, ValueError, OverflowError):
        return d

def emit(event, **fields):
    print(json.dumps({"event": event, "diag_version": DIAG_VERSION, **fields},
                     separators=(",", ":"), allow_nan=False), flush=True)

def canonical_key(chain, address):
    chain=str(chain).strip().lower()
    address=str(address).strip()
    if chain in EVM_CHAINS:
        address=address.lower()
    return chain,address

def key_clause(chain):
    # Collation is selected from a fixed allowlist, never user-controlled SQL.
    return "chain=? AND address=? COLLATE NOCASE" if chain in EVM_CHAINS else "chain=? AND address=?"


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
      persistence_fail_reason TEXT,
      persistence_key_matches INTEGER,
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
        ("persistence_fail_reason","TEXT"),
        ("persistence_key_matches","INTEGER"),
        ("fast_route","TEXT"),
        ("fast_priority","REAL"),
        ("deep_analysis","INTEGER"),
    ]:
        if name not in cols:
            db.execute(f"ALTER TABLE strategy_ab_observations ADD COLUMN {name} {typ}")
    db.commit()
    return db

def latest_prev(db, chain, address, now=None):
    chain,address=canonical_key(chain,address)
    cutoff=time.time() if now is None else float(now)
    return db.execute(f"""SELECT ts,price,liquidity,volume_h1,volume_h24,buys_h1,sells_h1
      FROM strategy_ab_observations WHERE {key_clause(chain)} AND ts<?
      ORDER BY ts DESC,id DESC LIMIT 1""", (chain,address,cutoff)).fetchone()

def price_anchors(db, chain, address, now, current_price):
    chain,address=canonical_key(chain,address)
    where=key_clause(chain)+" AND ts<? AND price>0"
    first=db.execute(f"SELECT price FROM strategy_ab_observations WHERE {where} ORDER BY ts,id LIMIT 1",
                     (chain,address,now)).fetchone()
    peak=db.execute(f"SELECT MAX(price) FROM strategy_ab_observations WHERE {where}",
                    (chain,address,now)).fetchone()
    return (num(first[0],current_price) if first else current_price,
            max(current_price,num(peak[0],current_price) if peak else current_price))


def pair_for(chain,address):
    chain,address=canonical_key(chain,address)
    raw=get_json(f"/token-pairs/v1/{chain}/{address}")
    if not isinstance(raw,list): return None
    matches=[]
    for p in raw:
        if not isinstance(p,dict): continue
        base=p.get("baseToken") or {}
        if canonical_key(chain,base.get("address",""))[1]!=address: continue
        if not p.get("priceUsd"): continue
        matches.append(p)
    if not matches:return None
    return max(matches,key=lambda p:num((p.get("liquidity") or {}).get("usd")))

def mk_snapshot(pair,ts):
    # Invalid numeric inputs are not silently converted into a favorable signal.
    fields={"priceUsd":pair.get("priceUsd"),
            "liquidity":(pair.get("liquidity") or {}).get("usd"),
            "volume_h1":(pair.get("volume") or {}).get("h1"),
            "volume_h24":(pair.get("volume") or {}).get("h24"),
            "buys_h1":((pair.get("txns") or {}).get("h1") or {}).get("buys"),
            "sells_h1":((pair.get("txns") or {}).get("h1") or {}).get("sells"),
            "price_change_h1":(pair.get("priceChange") or {}).get("h1")}
    for name,value in fields.items():
        if value is None and name!="priceUsd": continue
        numeric=num(value,float("nan"))
        if (not math.isfinite(numeric) or
            (name!="price_change_h1" and numeric<0) or
            (name=="priceUsd" and numeric<=0)):
            raise ValueError("invalid market field: "+name)
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

def _point_failures(score, risk, exit_ok, liquidity):
    failed=[]
    if not math.isfinite(score):
        failed.append("SCORE_INVALID")
    elif score < 1.0:
        failed.append("SCORE_LT_1")
    if not risk: failed.append("RISK_FAIL")
    if not exit_ok: failed.append("EXIT_FAIL")
    if not liquidity: failed.append("LIQUIDITY_FAIL")
    return failed

def _history_point(row):
    # Prefer the actual three gates saved with the observation, not REJECT as a proxy.
    raw=row["raw_json"]
    source="LEGACY_STATE_PROXY"
    diag=None
    if raw:
        try:
            parsed=json.loads(raw)
            diag=parsed.get("persistence_diag") if isinstance(parsed,dict) else None
        except (ValueError,TypeError):
            diag={}
    keys=("risk_pass","exit_pass","liquidity_pass")
    if isinstance(diag,dict) and all(type(diag.get(k)) is bool for k in keys):
        gates=tuple(diag[k] for k in keys)
        source="PERSISTED_GATES"
    elif diag is not None:
        gates=(False,False,False)
        source="INVALID_HISTORY_DIAGNOSTICS"
    else:
        # Compatibility with pre-diagnostic rows; never infer pass for an unknown state.
        allowed=row["challenger_state"] in ("WATCH","ENTER")
        gates=(allowed,allowed,allowed)
    score=num(row["challenger_score"],float("nan"))
    return SignalPoint(row["ts"],score,*gates),source

def persistence_for_token(db,chain,address,now,current_score,risk_pass,exit_pass,liq_pass,return_diag=False):
    chain,address=canonical_key(chain,address)
    now=float(now)
    if not math.isfinite(now):
        raise ValueError("non-finite observation timestamp")
    # Exclude future/current observations. A duplicated timestamp is not a new round.
    raw_rows=db.execute(f"""SELECT ts,challenger_score,challenger_state,raw_json
        FROM strategy_ab_observations WHERE {key_clause(chain)} AND ts<?
        ORDER BY ts DESC,id DESC LIMIT 25""",(chain,address,now)).fetchall()
    seen=set();rows=[]
    for row in raw_rows:
        if row["ts"] not in seen:
            rows.append(row);seen.add(row["ts"])
            if len(rows)==5: break
    pts=[];sources=[]
    for row in reversed(rows):
        point,source=_history_point(row)
        pts.append(point);sources.append(source)
    score=num(current_score,float("nan"))
    gates=(risk_pass is True,exit_pass is True,liq_pass is True)
    # The required count and score threshold are unchanged.
    checked_score=score if math.isfinite(score) else float("-inf")
    checked_pts=[SignalPoint(p.ts,p.score if math.isfinite(p.score) else float("-inf"),
                             p.risk_pass,p.exit_pass,p.liquidity_pass) for p in pts]
    result=persistence_check(checked_pts+[SignalPoint(now,checked_score,*gates)],required=3,min_score=1.0)
    failed=_point_failures(score,*gates)
    history_break="NONE"
    if not failed and not result.confirmed:
        for point in reversed(pts):
            failures=_point_failures(point.score,point.risk_pass,point.exit_pass,point.liquidity_pass)
            if failures:
                history_break="|".join(failures);break
        if history_break=="NONE": history_break="INSUFFICIENT_PRIOR_ROUNDS"
    diag={"version":DIAG_VERSION,"key_matches":len(rows),"history_limit":5,
          "current_score":round(score,6) if math.isfinite(score) else None,
          "risk_pass":gates[0],"exit_pass":gates[1],"liquidity_pass":gates[2],
          "failed":"|".join(failed) if failed else "NONE",
          "history_break_reason":history_break,"history_gate_sources":dict(Counter(sources)),
          "key":{"chain":chain,"address":address},
          "key_status":"PRIOR_HISTORY_FOUND" if rows else "NO_PRIOR_HISTORY"}
    return (result,diag) if return_diag else result



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
    chain,address=canonical_key(chain,address)
    cur=mk_snapshot(pair,now)
    route=fast_route_from_pair(pair,now)
    prev=mk_prev(latest_prev(db,chain,address,now))
    first_price,high=price_anchors(db,chain,address,now,cur.price)
    invalidation=max(cur.price*0.70, first_price*0.70) if cur.price>0 else 0
    intended=100.0
    exit_intended=intended
    d=build_decision(cur,prev,intended,first_price,high,invalidation,now)
    regime=synthetic_regime()
    intended*=regime_position_multiplier(regime)
    slip=estimate_slippage(cur.liquidity,intended)
    state=d.state
    reason=d.reason
    if not slip.pass_check:
        state="REJECT"; reason="SLIPPAGE_TOO_HIGH"
    pre_e,pre_r=prediscovery_from_pair(pair,now)
    # Independent strategy gates. These are NOT a contract/wallet safety audit.
    size_exit_ok,_=exit_first_ok(cur.liquidity,exit_intended)
    pstate=pullback_state(first_price,cur.price,high,invalidation)
    risk_ok=(data_freshness_ok(now,cur.social_ts,cur.holder_ts)
             and pstate!="EXPIRE"
             and not (d.state=="REJECT" and d.reason!="EXIT_TOO_THIN"))
    exit_ok=bool(size_exit_ok and slip.pass_check)
    pers,pers_diag=persistence_for_token(
        db,chain,address,now,d.score,
        risk_pass=bool(risk_ok),exit_pass=exit_ok,liq_pass=(cur.liquidity>0),
        return_diag=True,
    )
    pers_diag.update({"gate_scope":"STRATEGY_PROXY_NOT_CONTRACT_AUDIT",
                     "score_source":"strategy_v03_decision_score",
                     "score_reason":d.reason,
                     "vbp_strength":num((d.audit or {}).get("vbp_strength")),
                     "exit_size_pass":bool(size_exit_ok),"slippage_pass":bool(slip.pass_check),
                     "first_seen_price":first_price,"local_high_price":high})
    raw={
        "audit":d.audit,
        "slippage":slip.__dict__,
        "prediscovery":{"evidence":pre_e.__dict__,"result":pre_r.__dict__,
                        "note":"CURRENTLY_PROXY_INPUTS_ONLY_NOT_FULL_REAL_DATA"},
        "persistence":pers.__dict__,
        "persistence_diag":pers_diag,
        "fast_route":route.__dict__,
        "mode":"SHADOW_ONLY",
    }
    with db:
        db.execute("""INSERT INTO strategy_ab_observations(
          ts,chain,address,symbol,price,liquidity,volume_h1,volume_h24,buys_h1,sells_h1,
          control_state,challenger_state,challenger_reason,challenger_score,max_safe_usd,regime,
          prediscovery_stage,prediscovery_score,persistence_streak,persistence_required,persistence_confirmed,
          persistence_fail_reason,persistence_key_matches,fast_route,fast_priority,deep_analysis,raw_json)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (now,chain,address,symbol,cur.price,cur.liquidity,cur.volume_h1,cur.volume_h24,
           cur.buys_h1,cur.sells_h1,control_state(pair),state,reason,d.score,
           min(d.max_safe_usd,slip.max_safe_usd),regime,
           pre_r.stage,pre_r.score,pers.streak,pers.required,1 if pers.confirmed else 0,
           pers_diag["failed"],pers_diag["key_matches"],route.route,route.priority,1 if route.deep_analysis else 0,
           json.dumps(raw,separators=(",",":"))))
    return {"chain":chain,"address":address,"symbol":symbol,"diag_version":DIAG_VERSION,"control":control_state(pair),"challenger":state,
            "reason":reason,"pre":pre_r.stage,"persistence":f"{pers.streak}/{pers.required}",
            "persistence_failed":pers_diag["failed"],"persistence_key_matches":pers_diag["key_matches"],
            "persistence_score":pers_diag["current_score"],
            "persistence_confirmed":pers.confirmed,
            "persistence_history_break":pers_diag["history_break_reason"],
            "persistence_gate_scope":pers_diag["gate_scope"],
            "score_source":pers_diag["score_source"],
            "vbp_strength":pers_diag["vbp_strength"],
            "fast_route":route.route,"priority":round(route.priority,1)}

def cycle(db):
    now=time.time()
    profiles=get_json("/token-profiles/latest/v1")
    if not isinstance(profiles,list): raise ValueError("profile feed is not a list")
    n=0; decisions=[]; errors=0; seen=set()
    for p in profiles[:50]:
        if not isinstance(p,dict):continue
        chain=str(p.get("chainId") or "").lower()
        addr=str(p.get("tokenAddress") or "")
        chain,addr=canonical_key(chain,addr)
        if chain not in SUPPORTED or len(addr)<20: continue
        if (chain,addr) in seen: continue
        seen.add((chain,addr))
        try:
            pair=pair_for(chain,addr)
            if not pair:continue
            symbol=str((pair.get("baseToken") or {}).get("symbol") or "?")
            decision=evaluate_one(db,chain,addr,symbol,pair,now)
            decisions.append(decision)
            emit("STRATEGY_AB_OBSERVATION",**decision)
            n+=1
            if n>=20:break
            time.sleep(.25)
        except Exception as exc:
            errors+=1
            emit("STRATEGY_AB_TOKEN_ERROR",chain=chain,address=addr,error_type=type(exc).__name__,error=str(exc)[:160])
            continue
    with db:
        db.execute("INSERT OR REPLACE INTO strategy_ab_meta(k,v) VALUES('last_cycle',?)",
                   (json.dumps({"ts":now,"observed":n},separators=(",",":")),))
    failures=Counter(reason for d in decisions for reason in d["persistence_failed"].split("|") if reason!="NONE")
    summary={"observed":n,"errors":errors,"failure_counts":dict(failures),"sample":decisions[:3]}
    emit("STRATEGY_AB_CYCLE_DEGRADED" if errors else "STRATEGY_AB_CYCLE_OK",**summary)
    return summary

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
