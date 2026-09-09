"""Runtime decision worker: turns research inputs into auditable PAPER decisions.

This is the layer that makes the Railway bot judge rather than merely read data.
It consumes market regime, cross-asset confirmation, external events, technical
signals, persistence and optional token-intelligence/GMGN corroboration. It has
no wallet/order APIs.
"""
from __future__ import annotations
import json,math,os,re,sqlite3,time
from dataclasses import replace
from pathlib import Path
from decision_engine import (Decision,DecisionContext,UtilityTokenInput,MemeTokenInput,MajorLagInput,
                             decide_utility_long,decide_meme_long,decide_major_lag,VERSION as ENGINE_VERSION)
from market_regime import latest_result_from_db,CHAIN_LEADER
from external_events import latest_state as latest_event_state
from cross_asset import latest_state as latest_cross_state
from calibration_worker import latest_active as latest_calibration

VERSION="decision-worker-0.3.1"
TRUE_BAR_SOURCE="GECKOTERMINAL_ONCHAIN_OHLCV_MINUTE"

# Scope classification only. These hints do not relax any entry, liquidity,
# persistence, technical, market-regime, insider, concentration, or contract gate.
_MEME_WORDS={
  "meme","memecoin","dog","doge","cat","frog","pepe","shiba","shib","bonk",
  "woof","wojak","chad","ape","moon","floki","inu","mog","goat","pnut",
}
_MEME_SUBSTRINGS=("pepe","doge","shib","bonk","woof","floki")


def emit(event,**fields):print(json.dumps({"event":event,"version":VERSION,**fields},separators=(",",":"),allow_nan=False),flush=True)

def _num(x,default=None):
    try:
        v=float(x);return v if math.isfinite(v) else default
    except Exception:return default

def _normalized_words(text):
    return " "+re.sub(r"[^a-z0-9]+"," ",str(text or "").lower()).strip()+" "

def _meme_hint(symbol="",name="",evidence=""):
    """Conservative scope classifier for non-utility tokens.

    Utility scope always wins first. This function only routes obvious meme/speculative
    candidates into the existing MEME paper path; it never makes a trade decision.
    """
    ev=str(evidence or "").lower()
    if "meme" in ev:
        return True
    combined=_normalized_words(f"{symbol} {name}")
    if any(f" {term} " in combined for term in _MEME_WORDS):
        return True
    compact=re.sub(r"[^a-z0-9]+","",f"{symbol} {name}".lower())
    if any(term in compact for term in _MEME_SUBSTRINGS):
        return True
    # "inu" is a common meme suffix, but matching it as an arbitrary substring would
    # create false positives such as "minute"; require a token/name boundary suffix.
    for raw in (str(symbol or "").lower(),str(name or "").lower()):
        cleaned=re.sub(r"[^a-z0-9]+","",raw)
        if cleaned.endswith("inu") and len(cleaned)>3:
            return True
    return False

def dbopen(path:Path):
    db=sqlite3.connect(path,timeout=30);db.row_factory=sqlite3.Row;db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS token_intel_signals(
      id INTEGER PRIMARY KEY,ts REAL NOT NULL,chain TEXT NOT NULL,address TEXT NOT NULL,source TEXT NOT NULL,
      smart_money_score REAL,insider_risk REAL,top10_pct REAL,contract_risk_pass INTEGER,independent_catalyst INTEGER,
      raw_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_token_intel_key_ts ON token_intel_signals(chain,address,ts);
    CREATE TABLE IF NOT EXISTS decision_context_states(
      id INTEGER PRIMARY KEY,ts REAL NOT NULL,regime TEXT NOT NULL,shock_direction TEXT,market_confidence REAL NOT NULL,
      external_score REAL NOT NULL,external_shock REAL NOT NULL,cross_asset_score REAL NOT NULL,cross_asset_quality REAL NOT NULL,
      cross_asset_shock INTEGER NOT NULL,calibrated_variant TEXT,raw_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_decision_context_ts ON decision_context_states(ts);
    CREATE TABLE IF NOT EXISTS bot_decisions(
      id INTEGER PRIMARY KEY,ts REAL NOT NULL,scope TEXT NOT NULL,source_ref TEXT NOT NULL UNIQUE,
      chain TEXT,address TEXT,symbol TEXT,action TEXT NOT NULL,variant TEXT NOT NULL,score REAL NOT NULL,confidence REAL NOT NULL,
      reasons_json TEXT NOT NULL,metadata_json TEXT NOT NULL,engine_version TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_bot_decision_scope_ts ON bot_decisions(scope,ts);
    CREATE INDEX IF NOT EXISTS idx_bot_decision_token_ts ON bot_decisions(chain,address,ts);
    """)
    cols={r[1] for r in db.execute("PRAGMA table_info(decision_context_states)")}
    if "calibrated_variant" not in cols:db.execute("ALTER TABLE decision_context_states ADD COLUMN calibrated_variant TEXT")
    db.commit();return db


def context(db,now=None):
    now=time.time() if now is None else float(now);rg=None;ev=None;ca=None
    try:rg=latest_result_from_db(db,now=now,max_age=180)
    except sqlite3.OperationalError:pass
    try:ev=latest_event_state(db,now=now,max_age=180)
    except sqlite3.OperationalError:pass
    try:ca=latest_cross_state(db,now=now,max_age=180)
    except sqlite3.OperationalError:pass
    regime=rg.regime if rg else "NEUTRAL"
    # Calibration is scope-specific (MEME vs UTILITY_NEW_TOKEN), so the shared
    # market context must not accidentally inherit whichever scope wrote last.
    calibrated=None
    c=DecisionContext(
      regime=regime,shock_direction=rg.shock_direction if rg else None,
      market_confidence=rg.confidence if rg else .2,
      external_signed_score=ev["signed_score"] if ev else 0.0,external_shock_confidence=ev["shock_confidence"] if ev else 0.0,
      cross_asset_score=ca["risk_score"] if ca else 0.0,cross_asset_quality=ca["data_quality"] if ca else 0.0,cross_asset_shock=bool(ca and ca["shock"]),
      calibrated_variant=calibrated,
    )
    with db:db.execute("""INSERT INTO decision_context_states(ts,regime,shock_direction,market_confidence,external_score,external_shock,cross_asset_score,cross_asset_quality,cross_asset_shock,calibrated_variant,raw_json)
      VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(now,c.regime,c.shock_direction,c.market_confidence,c.external_signed_score,c.external_shock_confidence,c.cross_asset_score,c.cross_asset_quality,int(c.cross_asset_shock),c.calibrated_variant,json.dumps(c.__dict__,separators=(",",":"))))
    return c,rg


def scope_context(db,ctx,scope,now):
    calibrated=latest_calibration(db,ctx.regime,now=now,max_age=900,asset_scope=scope)
    return replace(ctx,calibrated_variant=calibrated)


def chain_aligned(rg,chain):
    if not rg:return False
    key=str(chain).lower();leader=CHAIN_LEADER.get(key)
    # Unknown chain leader cannot be used to justify aggressive exposure.
    if key not in CHAIN_LEADER or leader is None:return False
    r=rg.returns.get(leader,{}).get("15m")
    if r is None:return False
    if rg.regime=="RISK_OFF":return r<=0
    return r>=0


def technical_strength(row):
    try:raw=json.loads(row["raw_json"] or "{}")
    except Exception:raw={}
    score=40.0
    if raw.get("bullish_structure"):score+=20
    if raw.get("breakout20"):score+=10
    rsi=_num(row["rsi14"],50);score+=max(0,15-abs(rsi-60)*.6)
    vr=_num(row["volume_ratio"],0);score+=max(0,min(15,(vr-1)*12))
    if raw.get("overextended"):score-=20
    return max(0,min(100,score))


def utility_confidence(db,chain,address):
    row=db.execute("SELECT score,evidence FROM candidates WHERE chain=? AND address=?",(chain,address)).fetchone()
    if not row:return 25.0
    score=max(0,min(10,_num(row["score"],0)));out=score*9
    ev=str(row["evidence"] or "")
    if "utility_terms:" in ev:out+=10
    if "website" in ev:out+=5
    return max(0,min(100,out))


def market_inputs(db,chain,address,now):
    row=db.execute("""SELECT * FROM strategy_ab_observations WHERE chain=? AND address=? AND ts<=? ORDER BY ts DESC,id DESC LIMIT 1""",(chain,address,now)).fetchone()
    if row:
        liq=_num(row["liquidity"],0);persist=bool(row["persistence_confirmed"]);source="STRATEGY_AB"
    else:
        # GMGN-only discovery candidates may arrive before DexScreener profiles. They can
        # enter MEME technical research after repeated sightings. Utility-new-token scope
        # still requires independent utility evidence in its own decision path.
        try:g=db.execute("SELECT * FROM gmgn_discovery_candidates WHERE chain=? AND address=? AND last_seen>=?",(chain,address,now-180)).fetchone()
        except sqlite3.OperationalError:g=None
        if not g:return None
        liq=_num(g["liquidity"],0);persist=int(g["sighting_streak"] or 0)>=3;source="GMGN_TRENCHES"
    quality=0.0
    if liq>0:
        quality=max(0,min(100,50+50*math.log(max(liq,30_000)/30_000)/math.log(500_000/30_000))) if liq>=30_000 else max(0,50*liq/30_000)
    return {"liquidity":liq,"liquidity_quality":quality,"exit_liquidity_pass":liq>=30_000,"persistence_confirmed":persist,"ab":row,"source":source}


def _pct(x):
    v=_num(x)
    if v is None:return None
    return max(0.0,min(100.0,v*100.0 if 0.0<=v<=1.0 else v))


def gmgn_candidate(db,chain,address,now,max_age=600):
    try:return db.execute("SELECT * FROM gmgn_discovery_candidates WHERE chain=? AND address=? AND last_seen>=? ORDER BY last_seen DESC LIMIT 1",(chain,address,now-max_age)).fetchone()
    except sqlite3.OperationalError:return None


def token_intel(db,chain,address,now,max_age=300):
    row=db.execute("SELECT * FROM token_intel_signals WHERE chain=? AND address=? AND ts>=? ORDER BY CASE WHEN source='GMGN' THEN 0 ELSE 1 END,ts DESC LIMIT 1",(chain,address,now-max_age)).fetchone()
    if not row:return {"source":None,"smart_money_score":None,"insider_risk":None,"top10_pct":None,"contract_risk_pass":None,"independent_catalyst":False}
    return {"source":row["source"],"smart_money_score":_num(row["smart_money_score"]),"insider_risk":_num(row["insider_risk"]),"top10_pct":_pct(row["top10_pct"]),
      "contract_risk_pass":None if row["contract_risk_pass"] is None else bool(row["contract_risk_pass"]),"independent_catalyst":bool(row["independent_catalyst"])}


def token_scope(db,chain,address,now):
    """Classify runtime path without forcing MEME coins through a utility gate."""
    u=utility_confidence(db,chain,address)
    evidence=symbol=name=""
    try:
        r=db.execute("SELECT * FROM candidates WHERE chain=? AND address=?",(chain,address)).fetchone()
        if r:
            keys=set(r.keys())
            evidence=str(r["evidence"] or "") if "evidence" in keys else ""
            symbol=str(r["symbol"] or "") if "symbol" in keys else ""
            name=str(r["name"] or "") if "name" in keys else ""
    except sqlite3.OperationalError:pass
    g=gmgn_candidate(db,chain,address,now)
    if u>=55:
        return "UTILITY_NEW_TOKEN",u,g
    if g is not None or _meme_hint(symbol,name,evidence):
        return "MEME",u,g
    return "UNCLASSIFIED",u,g


def _save(db,now,scope,ref,decision,chain=None,address=None,symbol=None,extra=None):
    md=dict(decision.metadata);md.update(extra or {})
    with db:db.execute("""INSERT OR IGNORE INTO bot_decisions(ts,scope,source_ref,chain,address,symbol,action,variant,score,confidence,reasons_json,metadata_json,engine_version)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(now,scope,ref,chain,address,symbol,decision.action,decision.variant,decision.score,decision.confidence,
       json.dumps(decision.reasons,separators=(",",":")),json.dumps(md,separators=(",",":"),allow_nan=False),ENGINE_VERSION))


def process_tokens(db,ctx,rg,now,limit=40):
    rows=db.execute("""SELECT * FROM strategy_v08_observations WHERE ts>=? AND ts<=? ORDER BY id DESC LIMIT ?""",(now-180,now,limit)).fetchall()
    counts={"MEME":0,"UTILITY_NEW_TOKEN":0,"UNCLASSIFIED":0,"opens":0}
    for r in rows:
        ref=f"V08:{r['id']}"
        if db.execute("SELECT 1 FROM bot_decisions WHERE source_ref=?",(ref,)).fetchone():continue
        mi=market_inputs(db,r["chain"],r["address"],now)
        if not mi:continue
        intel=token_intel(db,r["chain"],r["address"],now)
        scope,uconf,g=token_scope(db,r["chain"],r["address"],now)
        common=dict(chain=r["chain"],technical_entry_ready=bool(r["entry_ready"]) and r["bar_source"]==TRUE_BAR_SOURCE,
          technical_strength=technical_strength(r),liquidity_quality=mi["liquidity_quality"],
          exit_liquidity_pass=mi["exit_liquidity_pass"],persistence_confirmed=mi["persistence_confirmed"],
          gmgn_smart_money_score=intel["smart_money_score"],gmgn_insider_risk=intel["insider_risk"],
          chain_leader_aligned=chain_aligned(rg,r["chain"]),contract_risk_pass=intel["contract_risk_pass"])
        scoped_ctx=scope_context(db,ctx,scope,now) if scope in {"MEME","UTILITY_NEW_TOKEN"} else ctx
        if scope=="UTILITY_NEW_TOKEN":
            inp=UtilityTokenInput(utility_confidence=uconf,**common)
            d=decide_utility_long(inp,scoped_ctx)
        elif scope=="MEME":
            inp=MemeTokenInput(gmgn_top10_pct=intel["top10_pct"],gmgn_sighting_streak=int(g["sighting_streak"] or 0) if g else 0,**common)
            d=decide_meme_long(inp,scoped_ctx)
        else:
            d=Decision("WATCH","balanced",.55,0.0,("ASSET_SCOPE_UNCLASSIFIED",),{"chain":r["chain"],"asset_scope":"UNCLASSIFIED"})
        _save(db,now,scope,ref,d,r["chain"],r["address"],r["symbol"],{
          "signal_id":r["id"],"bar_source":r["bar_source"],"intel_source":intel["source"] or "UNAVAILABLE",
          "asset_scope":scope,"utility_confidence":uconf,"market_input_source":mi["source"]})
        counts[scope]+=1;counts["opens"]+=int(d.action=="OPEN_LONG_PAPER")
    return counts


def process_mini(db,ctx,rg,now,limit=30):
    """Backward-compatible wrapper; runtime uses explicit MEME/UTILITY scopes."""
    c=process_tokens(db,ctx,rg,now,limit)
    return c["MEME"]+c["UTILITY_NEW_TOKEN"]+c["UNCLASSIFIED"],c["opens"]


def target_event_conflict(db,symbol,now):
    try:
        rows=db.execute("SELECT title FROM external_events WHERE active=1 AND COALESCE(published_at,observed_at)>=? ORDER BY id DESC LIMIT 30",(now-1800,)).fetchall()
    except sqlite3.OperationalError:return False
    pat={"ETH":("ethereum"," eth "),"XRP":("xrp","ripple"),"SOL":("solana"," sol "),"BNB":("bnb","binance coin")}[symbol]
    for r in rows:
        t=" "+str(r["title"] or "").lower()+" "
        if any(x in t for x in pat):return True
    return False


def process_major(db,ctx,now):
    try:rows=db.execute("""SELECT l.* FROM major_leadlag_states l JOIN (SELECT target,MAX(id) id FROM major_leadlag_states WHERE ts>=? GROUP BY target) x ON l.id=x.id""",(now-10,)).fetchall()
    except sqlite3.OperationalError:return 0,0
    n=0;opens=0
    for r in rows:
        ref=f"LAG:{r['id']}"
        if db.execute("SELECT 1 FROM bot_decisions WHERE source_ref=?",(ref,)).fetchone():continue
        completion=_num(r["reaction_completion"],1.0)
        direction=int(r["direction"] or 0)
        if direction not in (-1,1):continue
        inp=MajorLagInput(symbol=r["target"],direction=direction,lag_confidence=max(0,min(1,_num(r["lag_confidence"],0))),reaction_completion=max(0,min(1,completion)),
          expected_move_bps=max(0,_num(r["expected_move_bps"],0)),total_cost_bps=max(0,_num(r["total_cost_bps"],999)),independent_catalyst_conflict=target_event_conflict(db,r["target"],now))
        d=decide_major_lag(inp,ctx);_save(db,now,"MAJOR",ref,d,symbol=r["target"],extra={"lag_state_id":r["id"],"best_lag_s":r["best_lag_s"],"impulse_window_s":r["impulse_window_s"],"direction":direction})
        n+=1;opens+=int(d.action in ("OPEN_LONG_MAJOR_PAPER","OPEN_SHORT_MAJOR_PAPER"))
    return n,opens


def latest_context(db,now=None,max_age=10):
    row=db.execute("SELECT * FROM decision_context_states ORDER BY ts DESC,id DESC LIMIT 1").fetchone()
    if not row:return None
    now=time.time() if now is None else float(now)
    if now-float(row["ts"])>max_age:return None
    return row


def cycle(db,now=None):
    now=time.time() if now is None else float(now);ctx,rg=context(db,now);tc=process_tokens(db,ctx,rg,now);jn,jo=process_major(db,ctx,now)
    emit("DECISION_ENGINE_CYCLE_OK",regime=ctx.regime,external_score=round(ctx.external_signed_score,2),external_shock=round(ctx.external_shock_confidence,3),
      cross_asset_score=round(ctx.cross_asset_score,2),meme_evaluated=tc["MEME"],utility_evaluated=tc["UTILITY_NEW_TOKEN"],
      unclassified_evaluated=tc["UNCLASSIFIED"],token_open_candidates=tc["opens"],major_evaluated=jn,major_open_candidates=jo,no_real_trading=True)
    return {"meme":tc["MEME"],"utility":tc["UTILITY_NEW_TOKEN"],"unclassified":tc["UNCLASSIFIED"],"token_open":tc["opens"],"major":jn,"major_open":jo,"context":ctx}


def main():
    data=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"));data.mkdir(parents=True,exist_ok=True)
    db=dbopen(data/"discovery.sqlite3")
    try:
        while True:
            started=time.monotonic()
            try:cycle(db)
            except Exception as exc:emit("DECISION_ENGINE_CYCLE_ERROR",error_type=type(exc).__name__,error=str(exc)[:180])
            time.sleep(max(1,2-(time.monotonic()-started)))
    finally:db.close()
if __name__=="__main__":main()
