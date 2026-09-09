"""Independent long/short PAPER ledger for BTC-led major-coin lag research.

No real orders. Shorts are synthetic research positions and are explicitly not
assumed executable on a spot-only venue. Live use would require a separately
validated margin/perpetual venue and its actual fee/funding/liquidation model.
"""
from __future__ import annotations
import json,math,os,sqlite3,time
from pathlib import Path
from major_microstructure import FEE_BPS_PER_SIDE,SLIPPAGE_BUFFER_BPS_PER_SIDE

VERSION="major-paper-0.1.0"
PLANNED_USD=max(10.0,float(os.getenv("MAJOR_PAPER_USD","100")))


def emit(event,**fields):print(json.dumps({"event":event,"version":VERSION,**fields},separators=(",",":"),allow_nan=False),flush=True)

def dbopen(path:Path):
    db=sqlite3.connect(path,timeout=30);db.row_factory=sqlite3.Row;db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS major_paper_positions(
      id INTEGER PRIMARY KEY,decision_id INTEGER NOT NULL UNIQUE,symbol TEXT NOT NULL,direction INTEGER NOT NULL,status TEXT NOT NULL,
      opened_at REAL NOT NULL,last_update REAL NOT NULL,entry_mid REAL NOT NULL,entry_effective REAL NOT NULL,qty REAL NOT NULL,
      planned_usd REAL NOT NULL,entry_fee REAL NOT NULL,entry_spread_bps REAL NOT NULL,expected_move_bps REAL NOT NULL,
      stop_bps REAL NOT NULL,take_bps REAL NOT NULL,max_hold_s REAL NOT NULL,closed_at REAL,exit_mid REAL,exit_effective REAL,
      exit_fee REAL,net_pnl REAL,close_reason TEXT,cost_model TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_major_paper_open ON major_paper_positions(status,symbol);
    CREATE TABLE IF NOT EXISTS major_paper_actions(
      id INTEGER PRIMARY KEY,ts REAL NOT NULL,position_id INTEGER NOT NULL,action TEXT NOT NULL,mid REAL,effective REAL,fee REAL,reason TEXT NOT NULL,raw_json TEXT);
    """);db.commit();return db


def _num(x,default=None):
    try:v=float(x);return v if math.isfinite(v) else default
    except Exception:return default


def quote(db,symbol,now,max_age=10):
    row=db.execute("SELECT * FROM major_quotes WHERE symbol=?",(symbol,)).fetchone()
    if row and now*1000-int(row["ts_ms"])<=max_age*1000:
        bid=float(row["bid"]);ask=float(row["ask"]);mid=(bid+ask)/2
        return {"mid":mid,"bid":bid,"ask":ask,"spread_bps":(ask-bid)/mid*10000,"source":row["source"]}
    row=db.execute("SELECT * FROM major_ticks WHERE symbol=? AND ts_ms<=? ORDER BY ts_ms DESC,trade_id DESC LIMIT 1",(symbol,int(now*1000))).fetchone()
    if row and now*1000-int(row["ts_ms"])<=max_age*1000:
        p=float(row["price"]);return {"mid":p,"bid":p,"ask":p,"spread_bps":8.0,"source":row["source"]+":NO_FRESH_BOOK_FALLBACK"}
    return None


def _entry_exec(q,direction,usd):
    mid=q["mid"];slip=SLIPPAGE_BUFFER_BPS_PER_SIDE/10000
    eff=(q["ask"]*(1+slip)) if direction>0 else (q["bid"]*(1-slip))
    fee=usd*FEE_BPS_PER_SIDE/10000;qty=max(0,usd)/eff
    return eff,qty,fee


def _exit_exec(q,direction,qty):
    slip=SLIPPAGE_BUFFER_BPS_PER_SIDE/10000
    eff=(q["bid"]*(1-slip)) if direction>0 else (q["ask"]*(1+slip))
    notional=qty*eff;fee=notional*FEE_BPS_PER_SIDE/10000
    return eff,fee


def decisions(db,now,limit=20):
    return db.execute("""SELECT * FROM bot_decisions WHERE scope='MAJOR' AND action IN ('OPEN_LONG_MAJOR_PAPER','OPEN_SHORT_MAJOR_PAPER')
      AND ts>=? AND ts<=? ORDER BY id DESC LIMIT ?""",(now-10,now,limit)).fetchall()


def open_decision(db,d,now):
    if db.execute("SELECT 1 FROM major_paper_positions WHERE decision_id=?",(d["id"],)).fetchone():return None
    if db.execute("SELECT 1 FROM major_paper_positions WHERE symbol=? AND status='OPEN'",(d["symbol"],)).fetchone():return None
    q=quote(db,d["symbol"],now)
    if not q:return None
    md=json.loads(d["metadata_json"] or "{}");direction=1 if d["action"]=="OPEN_LONG_MAJOR_PAPER" else -1
    lagrow=None
    if md.get("lag_state_id"):
        lagrow=db.execute("SELECT * FROM major_leadlag_states WHERE id=?",(md["lag_state_id"],)).fetchone()
    expected=max(10.0,_num(lagrow["expected_move_bps"],20) if lagrow else 20.0)
    lag=max(1,_num(md.get("best_lag_s"),30));max_hold=max(30,min(300,lag*4))
    take=max(8,min(120,expected*.80));stop=max(8,min(80,expected*.60))
    eff,qty,fee=_entry_exec(q,direction,PLANNED_USD)
    if qty<=0:return None
    cost_model="BOOK_TICKER_SPREAD+FEE+SLIPPAGE_BUFFER;SYNTHETIC_SHORT_NOT_SPOT_EXECUTION" if direction<0 else "BOOK_TICKER_SPREAD+FEE+SLIPPAGE_BUFFER"
    with db:
        cur=db.execute("""INSERT INTO major_paper_positions(decision_id,symbol,direction,status,opened_at,last_update,entry_mid,entry_effective,qty,planned_usd,entry_fee,entry_spread_bps,expected_move_bps,stop_bps,take_bps,max_hold_s,cost_model)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(d["id"],d["symbol"],direction,"OPEN",now,now,q["mid"],eff,qty,PLANNED_USD,fee,q["spread_bps"],expected,stop,take,max_hold,cost_model))
        pid=cur.lastrowid
        db.execute("INSERT INTO major_paper_actions(ts,position_id,action,mid,effective,fee,reason,raw_json) VALUES(?,?,?,?,?,?,?,?)",
          (now,pid,"OPEN_LONG" if direction>0 else "OPEN_SHORT",q["mid"],eff,fee,"BTC_LEAD_LAG_DECISION",json.dumps(md,separators=(",",":"))))
    return pid


def close(db,p,q,now,reason):
    eff,fee=_exit_exec(q,int(p["direction"]),float(p["qty"]));entry=float(p["entry_effective"]);qty=float(p["qty"])
    gross=(eff-entry)*qty if int(p["direction"])>0 else (entry-eff)*qty
    pnl=gross-float(p["entry_fee"])-fee
    with db:
        db.execute("UPDATE major_paper_positions SET status='CLOSED',last_update=?,closed_at=?,exit_mid=?,exit_effective=?,exit_fee=?,net_pnl=?,close_reason=? WHERE id=?",
          (now,now,q["mid"],eff,fee,pnl,reason,p["id"]))
        db.execute("INSERT INTO major_paper_actions(ts,position_id,action,mid,effective,fee,reason,raw_json) VALUES(?,?,?,?,?,?,?,?)",
          (now,p["id"],"CLOSE",q["mid"],eff,fee,reason,"{}"))
    return pnl


def manage(db,p,now):
    q=quote(db,p["symbol"],now)
    if not q:return "NO_FRESH_QUOTE"
    direction=int(p["direction"]);move=(q["mid"]/float(p["entry_mid"])-1)*10000*direction
    # An opposite high-confidence BTC lead/lag decision after entry is a reversal signal.
    opp=db.execute("""SELECT action FROM bot_decisions WHERE scope='MAJOR' AND symbol=? AND ts>? ORDER BY id DESC LIMIT 1""",(p["symbol"],p["opened_at"])).fetchone()
    if opp and ((direction>0 and opp[0]=="OPEN_SHORT_MAJOR_PAPER") or (direction<0 and opp[0]=="OPEN_LONG_MAJOR_PAPER")):
        close(db,p,q,now,"BTC_LEAD_REVERSAL");return "CLOSED_REVERSAL"
    if move>=float(p["take_bps"]):close(db,p,q,now,"EXPECTED_LAG_MOVE_CAPTURED");return "CLOSED_TAKE"
    if move<=-float(p["stop_bps"]):close(db,p,q,now,"LAG_THESIS_FAILED");return "CLOSED_STOP"
    if now-float(p["opened_at"])>=float(p["max_hold_s"]):close(db,p,q,now,"LAG_WINDOW_EXPIRED");return "CLOSED_TIME"
    with db:db.execute("UPDATE major_paper_positions SET last_update=? WHERE id=?",(now,p["id"]))
    return "HELD"


def scorecard(db):
    out={}
    for name,direction in (("long",1),("short",-1),("all",0)):
        if direction:rows=db.execute("SELECT net_pnl FROM major_paper_positions WHERE direction=? AND status='CLOSED'",(direction,)).fetchall();open_n=db.execute("SELECT COUNT(*) FROM major_paper_positions WHERE direction=? AND status='OPEN'",(direction,)).fetchone()[0]
        else:rows=db.execute("SELECT net_pnl FROM major_paper_positions WHERE status='CLOSED'").fetchall();open_n=db.execute("SELECT COUNT(*) FROM major_paper_positions WHERE status='OPEN'").fetchone()[0]
        pnls=[float(r[0]) for r in rows if r[0] is not None];wins=[x for x in pnls if x>0];losses=[-x for x in pnls if x<0]
        out[name]={"closed":len(pnls),"open":open_n,"net_pnl":round(sum(pnls),4),"win_rate":round(len(wins)/len(pnls),4) if pnls else None,"profit_factor":round(sum(wins)/sum(losses),4) if losses else (999.0 if wins else None)}
    out["short_execution_note"]="SYNTHETIC_PAPER_ONLY_NOT_SPOT_EXECUTION"
    return out


def cycle(db,now=None):
    now=time.time() if now is None else float(now);opened=0
    try:ds=decisions(db,now)
    except sqlite3.OperationalError:
        emit("MAJOR_PAPER_WAITING_FOR_DECISIONS",no_trade=True);return {"opened":0,"managed":0,"scorecard":scorecard(db),"waiting":True}
    for d in ds:opened+=int(open_decision(db,d,now) is not None)
    rows=db.execute("SELECT * FROM major_paper_positions WHERE status='OPEN' ORDER BY id").fetchall();acts={}
    for p in rows:r=manage(db,p,now);acts[r]=acts.get(r,0)+1
    sc=scorecard(db);emit("MAJOR_PAPER_CYCLE_OK",opened=opened,managed=len(rows),actions=acts,scorecard=sc,no_real_trading=True);return {"opened":opened,"managed":len(rows),"actions":acts,"scorecard":sc}


def main():
    data=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"));data.mkdir(parents=True,exist_ok=True);db=dbopen(data/"discovery.sqlite3")
    try:
        while True:
            started=time.monotonic()
            try:cycle(db)
            except Exception as exc:emit("MAJOR_PAPER_ERROR",error_type=type(exc).__name__,error=str(exc)[:180])
            time.sleep(max(.5,2-(time.monotonic()-started)))
    finally:db.close()
if __name__=="__main__":main()
