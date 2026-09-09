"""Independent v0.8 paper-trade ledger.

Runs conservative/balanced/aggressive side-by-side plus an adaptive regime-selected variant.
Uses only research tables and modeled execution costs. Never connects to wallets or order APIs.
"""
from __future__ import annotations
import json, math, os, sqlite3, time
from pathlib import Path
from strategy_v08 import CONSERVATIVE,BALANCED,AGGRESSIVE

VERSION="v08-paper-0.4.0"
PLANNED_USD=max(10.0,float(os.getenv("V08_PAPER_PLANNED_USD","100")))
FEE_RATE=max(0.0,float(os.getenv("V08_PAPER_FEE_RATE","0.003")))
GAS_USD={"solana":.02,"base":.05,"bsc":.05,"arbitrum":.08,"polygon":.03,"ethereum":1.00}
PARAMS={"conservative":CONSERVATIVE,"balanced":BALANCED,"aggressive":AGGRESSIVE}
VARIANTS=("conservative","balanced","aggressive","adaptive")
TRUE_BAR_SOURCE="GECKOTERMINAL_ONCHAIN_OHLCV_MINUTE"
MAX_ENTRY_IMPACT_PCT=max(0.1,float(os.getenv("V08_PAPER_MAX_ENTRY_IMPACT_PCT","3.0")))
MAX_EXIT_IMPACT_PCT=max(MAX_ENTRY_IMPACT_PCT,float(os.getenv("V08_PAPER_MAX_EXIT_IMPACT_PCT","10.0")))

def emit(event,**fields):
    print(json.dumps({"event":event,"version":VERSION,**fields},separators=(",",":"),allow_nan=False),flush=True)

def dbopen(path:Path):
    db=sqlite3.connect(path,timeout=30);db.row_factory=sqlite3.Row;db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS v08_paper_positions(
      id INTEGER PRIMARY KEY,variant TEXT NOT NULL,asset_scope TEXT NOT NULL DEFAULT 'TECHNICAL_ALL',chain TEXT NOT NULL,address TEXT NOT NULL,symbol TEXT,
      status TEXT NOT NULL,opened_at REAL NOT NULL,last_update REAL NOT NULL,signal_id INTEGER NOT NULL,
      selected_at_entry TEXT,entry_regime TEXT,planned_usd REAL NOT NULL,deployed_usd REAL NOT NULL,qty REAL NOT NULL,
      initial_qty REAL NOT NULL,avg_entry REAL NOT NULL,peak_price REAL NOT NULL,cash_outflow REAL NOT NULL,
      cash_inflow REAL NOT NULL DEFAULT 0,tier_mask INTEGER NOT NULL DEFAULT 0,add_count INTEGER NOT NULL DEFAULT 0,
      closed_at REAL,close_price REAL,net_pnl REAL,close_reason TEXT,execution_blocked INTEGER NOT NULL DEFAULT 0,
      data_source TEXT NOT NULL,cost_model TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_v08_paper_open ON v08_paper_positions(status,chain,address,variant);
    CREATE TABLE IF NOT EXISTS v08_paper_signal_uses(
      signal_id INTEGER NOT NULL,variant TEXT NOT NULL,position_id INTEGER NOT NULL,PRIMARY KEY(signal_id,variant));
    CREATE TABLE IF NOT EXISTS v08_paper_actions(
      id INTEGER PRIMARY KEY,ts REAL NOT NULL,position_id INTEGER NOT NULL,variant TEXT NOT NULL,action TEXT NOT NULL,
      market_price REAL NOT NULL,effective_price REAL,qty REAL,usd REAL,fee_usd REAL,gas_usd REAL,impact_pct REAL,
      reason TEXT NOT NULL,raw_json TEXT);
    CREATE TABLE IF NOT EXISTS v08_paper_entry_rejections(
      id INTEGER PRIMARY KEY,ts REAL NOT NULL,signal_id INTEGER NOT NULL,variant TEXT NOT NULL,chain TEXT NOT NULL,address TEXT NOT NULL,
      reason TEXT NOT NULL,liquidity REAL,impact_pct REAL);
    """)
    cols={r[1] for r in db.execute("PRAGMA table_info(v08_paper_positions)")}
    if "entry_regime" not in cols:db.execute("ALTER TABLE v08_paper_positions ADD COLUMN entry_regime TEXT")
    if "asset_scope" not in cols:db.execute("ALTER TABLE v08_paper_positions ADD COLUMN asset_scope TEXT NOT NULL DEFAULT 'TECHNICAL_ALL'")
    db.commit();return db

def _num(x,default=None):
    try:
        v=float(x)
        return v if math.isfinite(v) else default
    except (TypeError,ValueError):return default

def latest_true_market(db,chain,address,now):
    now=float(now)
    row=db.execute("""SELECT ts,close,source FROM live_ohlcv WHERE chain=? AND address=? AND timeframe='minute' AND ts<=?
      ORDER BY ts DESC LIMIT 1""",(chain,address,int(now))).fetchone()
    if not row or now-float(row["ts"])>180 or float(row["ts"])>now+5:return None
    liq=db.execute("""SELECT ts,liquidity FROM strategy_ab_observations WHERE chain=? AND address=? AND ts<=?
      ORDER BY ts DESC,id DESC LIMIT 1""",(chain,address,now)).fetchone()
    if not liq or now-float(liq["ts"])>180:return None
    liquidity=_num(liq["liquidity"],None)
    if liquidity is None or liquidity<=0:return None
    return {"ts":float(row["ts"]),"price":float(row["close"]),"liquidity":liquidity,"source":str(row["source"] or TRUE_BAR_SOURCE)}

def latest_signal(db,chain,address,now):
    return db.execute("""SELECT * FROM strategy_v08_observations WHERE chain=? AND address=? AND ts<=?
      ORDER BY ts DESC,id DESC LIMIT 1""",(chain,address,now)).fetchone()

def entry_signals(db,now,limit=12):
    rows=db.execute("""SELECT * FROM strategy_v08_observations WHERE ts>=? AND ts<=? AND entry_ready=1
      AND bar_source=? ORDER BY id DESC LIMIT ?""",(now-180,now,TRUE_BAR_SOURCE,limit*4)).fetchall()
    out=[];seen=set()
    for r in rows:
        key=(r["chain"],r["address"])
        if key in seen:continue
        seen.add(key);out.append(r)
        if len(out)>=limit:break
    return out

def _exec(chain,mid,liquidity,usd=None,qty=None,side="buy"):
    mid=float(mid);liq=float(liquidity or 0);gas=GAS_USD.get(chain,.10)
    if not math.isfinite(mid) or mid<=0 or not math.isfinite(liq) or liq<=0:
        return {"effective":None,"qty":0.0,"usd":0.0,"fee":0.0,"gas":gas,"impact_pct":None,"cash_flow":0.0,"blocked":True}
    if side=="buy":
        usd=float(usd);impact=(usd/liq)*1.5;blocked=(not math.isfinite(usd) or usd<=0 or impact*100>MAX_ENTRY_IMPACT_PCT)
        if blocked:return {"effective":mid*(1+min(.95,max(0.0,impact))),"qty":0.0,"usd":usd,"fee":0.0,"gas":gas,"impact_pct":impact*100,"cash_flow":0.0,"blocked":True}
        eff=mid*(1+impact);fee=usd*FEE_RATE;acquired=max(0.0,usd-fee)/eff
        return {"effective":eff,"qty":acquired,"usd":usd,"fee":fee,"gas":gas,"impact_pct":impact*100,"cash_flow":-(usd+gas),"blocked":False}
    qty=float(qty);notional=max(0.0,qty*mid);impact=(notional/liq)*1.5
    blocked=(not math.isfinite(qty) or qty<=0 or impact*100>MAX_EXIT_IMPACT_PCT)
    eff=mid*(1-min(.95,max(0.0,impact)))
    if blocked:return {"effective":eff,"qty":qty,"usd":0.0,"fee":0.0,"gas":gas,"impact_pct":impact*100,"cash_flow":0.0,"blocked":True}
    gross=qty*eff;fee=gross*FEE_RATE;inflow=max(0.0,gross-fee-gas)
    return {"effective":eff,"qty":qty,"usd":inflow,"fee":fee,"gas":gas,"impact_pct":impact*100,"cash_flow":inflow,"blocked":False}

def latest_bot_decision(db,chain,address,now,max_age=180):
    try:
        return db.execute("""SELECT * FROM bot_decisions WHERE scope IN ('MEME','UTILITY_NEW_TOKEN') AND chain=? AND address=? AND ts<=? AND ts>=? ORDER BY ts DESC,id DESC LIMIT 1""",(chain,address,now,now-max_age)).fetchone()
    except sqlite3.OperationalError:return None

def global_halt(db,now,max_age=10):
    try:row=db.execute("SELECT * FROM decision_context_states WHERE ts<=? ORDER BY ts DESC,id DESC LIMIT 1",(now,)).fetchone()
    except sqlite3.OperationalError:return False
    if not row or now-float(row["ts"])>max_age:return False
    return row["regime"]=="SHOCK" or float(row["external_shock"] or 0)>=.72 or bool(row["cross_asset_shock"])

def entry_regime(db,now,max_age=180):
    try:row=db.execute("SELECT regime,ts FROM decision_context_states WHERE ts<=? ORDER BY ts DESC,id DESC LIMIT 1",(now,)).fetchone()
    except sqlite3.OperationalError:return None
    return str(row["regime"]) if row and now-float(row["ts"])<=max_age else None

def _params(variant,signal,selected_variant=None):
    if variant!="adaptive":return PARAMS[variant]
    name=str(selected_variant or signal["recommended_variant"] or "balanced")
    return PARAMS.get(name,BALANCED)

def _record_action(db,now,pid,variant,action,mid,ex,reason,raw=None):
    with db:db.execute("""INSERT INTO v08_paper_actions(ts,position_id,variant,action,market_price,effective_price,qty,usd,fee_usd,gas_usd,impact_pct,reason,raw_json)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(now,pid,variant,action,mid,ex.get("effective"),ex.get("qty"),ex.get("usd"),ex.get("fee"),ex.get("gas"),ex.get("impact_pct"),reason,json.dumps(raw or {},separators=(",",":"))))

def open_from_signal(db,signal,variant,now,selected_variant=None,asset_scope="TECHNICAL_ALL"):
    used=db.execute("SELECT 1 FROM v08_paper_signal_uses WHERE signal_id=? AND variant=?",(signal["id"],variant)).fetchone()
    if used:return None
    market=latest_true_market(db,signal["chain"],signal["address"],now)
    if not market:return None
    if str(signal["recommended_variant"] or "balanced")=="halt":return None
    params=_params(variant,signal,selected_variant);usd=PLANNED_USD*params.first_entry_fraction
    ex=_exec(signal["chain"],market["price"],market["liquidity"],usd=usd,side="buy")
    if ex["blocked"] or ex["qty"]<=0:
        with db:
            db.execute("INSERT OR IGNORE INTO v08_paper_signal_uses(signal_id,variant,position_id) VALUES(?,?,?)",(signal["id"],variant,0))
            db.execute("INSERT INTO v08_paper_entry_rejections(ts,signal_id,variant,chain,address,reason,liquidity,impact_pct) VALUES(?,?,?,?,?,?,?,?)",
                       (now,signal["id"],variant,signal["chain"],signal["address"],"ENTRY_IMPACT_TOO_HIGH_OR_INVALID_EXECUTION",market["liquidity"],ex.get("impact_pct")))
        return None
    with db:
        cur=db.execute("""INSERT INTO v08_paper_positions(variant,asset_scope,chain,address,symbol,status,opened_at,last_update,signal_id,selected_at_entry,entry_regime,
          planned_usd,deployed_usd,qty,initial_qty,avg_entry,peak_price,cash_outflow,cash_inflow,data_source,cost_model)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (variant,asset_scope,signal["chain"],signal["address"],signal["symbol"],"OPEN",now,now,signal["id"],selected_variant or signal["recommended_variant"],entry_regime(db,now),
           PLANNED_USD,usd,ex["qty"],ex["qty"],ex["effective"],market["price"],usd+ex["gas"],0.0,market["source"],"FEE_0.30PCT+LIQUIDITY_IMPACT_1.5X+CHAIN_GAS_ASSUMPTION"))
        pid=cur.lastrowid;db.execute("INSERT INTO v08_paper_signal_uses(signal_id,variant,position_id) VALUES(?,?,?)",(signal["id"],variant,pid))
    _record_action(db,now,pid,variant,"BUY",market["price"],ex,"INITIAL_ENTRY",{"selected_at_entry":selected_variant or signal["recommended_variant"]})
    return pid

def _sell(db,p,signal,market,qty,reason,now,close_all=False):
    ex=_exec(p["chain"],market["price"],market["liquidity"],qty=qty,side="sell")
    if ex["blocked"]:
        with db:db.execute("UPDATE v08_paper_positions SET execution_blocked=execution_blocked+1,last_update=? WHERE id=?",(now,p["id"]))
        _record_action(db,now,p["id"],p["variant"],"EXIT_BLOCKED",market["price"],ex,reason)
        return False
    newqty=max(0.0,float(p["qty"])-qty);inflow=float(p["cash_inflow"])+ex["cash_flow"]
    if close_all or newqty<=1e-12:
        pnl=inflow-float(p["cash_outflow"])
        with db:db.execute("""UPDATE v08_paper_positions SET status='CLOSED',qty=0,cash_inflow=?,closed_at=?,close_price=?,net_pnl=?,close_reason=?,last_update=? WHERE id=?""",
          (inflow,now,market["price"],pnl,reason,now,p["id"]))
    else:
        with db:db.execute("UPDATE v08_paper_positions SET qty=?,cash_inflow=?,last_update=? WHERE id=?",(newqty,inflow,now,p["id"]))
    _record_action(db,now,p["id"],p["variant"],"SELL_ALL" if close_all else "SELL_PART",market["price"],ex,reason)
    return True

def manage_one(db,p,now):
    market=latest_true_market(db,p["chain"],p["address"],now)
    signal=latest_signal(db,p["chain"],p["address"],now)
    if not market or not signal:return "NO_FRESH_DATA"
    d=latest_bot_decision(db,p["chain"],p["address"],now) if p["variant"]=="adaptive" else None
    selected=(d["variant"] if d and d["variant"] in PARAMS else p["selected_at_entry"]) if p["variant"]=="adaptive" else None
    params=_params(p["variant"],signal,selected);price=market["price"];peak=max(float(p["peak_price"]),price)
    if p["variant"]=="adaptive" and global_halt(db,now):
        return "CLOSED" if _sell(db,p,signal,market,float(p["qty"]),"GLOBAL_DECISION_SHOCK_EXIT",now,True) else "EXIT_BLOCKED"
    with db:db.execute("UPDATE v08_paper_positions SET peak_price=?,last_update=? WHERE id=?",(peak,now,p["id"]))
    p=db.execute("SELECT * FROM v08_paper_positions WHERE id=?",(p["id"],)).fetchone()
    atr_pct=max(0.0,_num(signal["atr_pct"],0.0));stop_pct=min(params.hard_stop_cap,max(params.hard_stop_floor,params.hard_stop_atr_mult*atr_pct))
    if price<=float(p["avg_entry"])*(1-stop_pct):
        return "CLOSED" if _sell(db,p,signal,market,float(p["qty"]),"VOLATILITY_HARD_STOP",now,True) else "EXIT_BLOCKED"
    if peak/float(p["avg_entry"])-1>=params.trail_activate_gain:
        trail=max(float(p["avg_entry"]),peak-params.trail_atr_mult*(atr_pct*price))
        if price<=trail:
            return "CLOSED" if _sell(db,p,signal,market,float(p["qty"]),"TRAILING_PROFIT_PROTECTION",now,True) else "EXIT_BLOCKED"
    gain=price/float(p["avg_entry"])-1
    prior_takes={r[0] for r in db.execute("SELECT reason FROM v08_paper_actions WHERE position_id=? AND action='SELL_PART'",(p["id"],)).fetchall()}
    for i,t in enumerate(params.tiers):
        tag=f"TAKE_{int(t.gain*100)}"
        if tag not in prior_takes and gain>=t.gain:
            q=min(float(p["qty"]),float(p["initial_qty"])*t.sell_fraction_initial)
            if q>0 and _sell(db,p,signal,market,q,tag,now,False):
                with db:db.execute("UPDATE v08_paper_positions SET tier_mask=tier_mask|? WHERE id=?",(1<<i,p["id"]))
                prior_takes.add(tag);p=db.execute("SELECT * FROM v08_paper_positions WHERE id=?",(p["id"],)).fetchone()
    # Pyramiding only into profitable, reconfirmed pullbacks; never average down.
    max_budget=float(p["planned_usd"])*params.max_deployed_fraction
    adaptive_add_ok=(p["variant"]!="adaptive" or (d is not None and d["action"]=="OPEN_LONG_PAPER"))
    if adaptive_add_ok and int(signal["entry_ready"] or 0)==1 and float(p["deployed_usd"])<max_budget-1e-9:
        gain=price/float(p["avg_entry"])-1;dd=1-price/peak if peak>0 else 0
        rsi=_num(signal["rsi14"],0);ef=_num(signal["ema_fast"],0);es=_num(signal["ema_slow"],0)
        if gain>=params.add_min_gain and params.pullback_min<=dd<=params.pullback_max and params.add_rsi_low<=rsi<=params.add_rsi_high and price>ef>es:
            remaining=max(0.0,max_budget-float(p["deployed_usd"]));usd=min(remaining,float(p["planned_usd"])*params.add_fraction)
            ex=_exec(p["chain"],price,market["liquidity"],usd=usd,side="buy")
            if ex["qty"]>0:
                oldq=float(p["qty"]);newq=oldq+ex["qty"];avg=(oldq*float(p["avg_entry"])+ex["qty"]*ex["effective"])/newq
                with db:db.execute("""UPDATE v08_paper_positions SET deployed_usd=deployed_usd+?,qty=?,avg_entry=?,cash_outflow=cash_outflow+?,add_count=add_count+1,last_update=? WHERE id=?""",
                  (usd,newq,avg,usd+ex["gas"],now,p["id"]))
                _record_action(db,now,p["id"],p["variant"],"ADD",price,ex,"PROFITABLE_PULLBACK_RECONFIRMED")
                return "ADDED"
    return "HELD"

def signal_asset_scope(db,signal_id):
    try:
        row=db.execute("SELECT scope FROM bot_decisions WHERE source_ref=? ORDER BY id DESC LIMIT 1",(f"V08:{int(signal_id)}",)).fetchone()
    except sqlite3.OperationalError:
        row=None
    return str(row["scope"]) if row and row["scope"] in {"MEME","UTILITY_NEW_TOKEN"} else "UNCLASSIFIED_TECHNICAL"


def adaptive_entry_decisions(db,now,limit=12):
    try:rows=db.execute("""SELECT * FROM bot_decisions WHERE scope IN ('MEME','UTILITY_NEW_TOKEN') AND action='OPEN_LONG_PAPER' AND ts>=? AND ts<=? ORDER BY id DESC LIMIT ?""",(now-180,now,limit*3)).fetchall()
    except sqlite3.OperationalError:return []
    out=[];seen=set()
    for d in rows:
        try:md=json.loads(d["metadata_json"] or "{}");sid=int(md.get("signal_id"))
        except Exception:continue
        if sid in seen:continue
        signal=db.execute("SELECT * FROM strategy_v08_observations WHERE id=? AND entry_ready=1 AND bar_source=?",(sid,TRUE_BAR_SOURCE)).fetchone()
        if signal:
            seen.add(sid);out.append((d,signal))
        if len(out)>=limit:break
    return out

def _dependencies_ready(db):
    names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    return {"live_ohlcv","strategy_ab_observations","strategy_v08_observations"}.issubset(names)

def cycle(db,now=None):
    now=time.time() if now is None else float(now)
    if not _dependencies_ready(db):
        emit("V08_PAPER_WAITING_FOR_DATA_TABLES",no_trade=True)
        return {"opened":0,"managed":0,"actions":{},"scorecard":scorecard(db),"waiting":True}
    opened=0
    # Fixed variants remain clean A/B comparators using the same technical signal.
    for signal in entry_signals(db,now):
        scope=signal_asset_scope(db,signal["id"])
        for variant in ("conservative","balanced","aggressive"):
            opened+=int(open_from_signal(db,signal,variant,now,asset_scope=scope) is not None)
    # Adaptive is the actual unified-decision challenger: it opens only when the
    # runtime Decision Engine approves the same true-OHLC signal.
    for decision,signal in adaptive_entry_decisions(db,now):
        opened+=int(open_from_signal(db,signal,"adaptive",now,selected_variant=decision["variant"],asset_scope=decision["scope"]) is not None)
    rows=db.execute("SELECT * FROM v08_paper_positions WHERE status='OPEN' ORDER BY id").fetchall();counts={}
    for p in rows:
        result=manage_one(db,p,now);counts[result]=counts.get(result,0)+1
    summary=scorecard(db)
    emit("V08_PAPER_CYCLE_OK",opened=opened,managed=len(rows),actions=counts,scorecard=summary,no_trade=True)
    return {"opened":opened,"managed":len(rows),"actions":counts,"scorecard":summary}

def scorecard(db):
    out={}
    for v in VARIANTS:
        rows=db.execute("SELECT net_pnl FROM v08_paper_positions WHERE variant=? AND status='CLOSED'",(v,)).fetchall();open_n=db.execute("SELECT COUNT(*) FROM v08_paper_positions WHERE variant=? AND status='OPEN'",(v,)).fetchone()[0]
        blocked=db.execute("SELECT COALESCE(SUM(execution_blocked),0) FROM v08_paper_positions WHERE variant=?",(v,)).fetchone()[0]
        blocked_entries=db.execute("SELECT COUNT(*) FROM v08_paper_entry_rejections WHERE variant=?",(v,)).fetchone()[0]
        pnls=[float(r["net_pnl"]) for r in rows if r["net_pnl"] is not None];wins=[x for x in pnls if x>0];losses=[-x for x in pnls if x<0]
        out[v]={"closed":len(pnls),"open":open_n,"net_pnl":round(sum(pnls),4),"win_rate":round(len(wins)/len(pnls),4) if pnls else None,
          "profit_factor":round(sum(wins)/sum(losses),4) if losses else (999.0 if wins else None),"blocked_exits":int(blocked or 0),"blocked_entries":int(blocked_entries or 0)}
    return out

def main():
    data=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"));data.mkdir(parents=True,exist_ok=True)
    db=dbopen(data/"discovery.sqlite3")
    try:
        while True:
            started=time.monotonic()
            try:cycle(db)
            except Exception as exc:emit("V08_PAPER_CYCLE_ERROR",error_type=type(exc).__name__,error=str(exc)[:180])
            time.sleep(max(5,60-(time.monotonic()-started)))
    finally:db.close()
if __name__=="__main__":main()
