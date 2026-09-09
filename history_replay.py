"""Historical on-chain collector v0.2: observable, rate-limited, and stall-resistant.

Collects REAL public GeckoTerminal pool catalog + daily OHLCV into the existing
/data/discovery.sqlite3. No wallet access and no live trading capability.
"""
from __future__ import annotations
import argparse, json, math, os, random, sqlite3, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from live_ohlcv import SharedGTQuota, retry_after_seconds

GT="https://api.geckoterminal.com/api/v2"
VERSION="history-replay-0.3.1"
NETWORKS=("solana","eth","base","bsc","arbitrum","polygon_pos")
MIN_CALL_INTERVAL=max(7.5,float(os.getenv("HISTORY_MIN_CALL_INTERVAL","7.5")))
UA="crypto-paper-lab-history/0.2"

def gt_json(path:str, params:dict[str,Any]|None=None)->dict[str,Any]:
    if not path.startswith("/networks/"): raise ValueError("unsupported endpoint")
    url=GT+path+("?" + urllib.parse.urlencode(params) if params else "")
    req=urllib.request.Request(url,headers={"User-Agent":UA,"Accept":"application/json;version=20230203"})
    with urllib.request.urlopen(req,timeout=20) as r:
        raw=r.read(5_000_001)
        if len(raw)>5_000_000: raise ValueError("oversize response")
    body=json.loads(raw)
    if not isinstance(body,dict): raise ValueError("malformed response")
    return body

def db_connect(path:Path)->sqlite3.Connection:
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30)
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL"); db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS history_pool_catalog(
      network TEXT NOT NULL,pool_address TEXT NOT NULL,pool_name TEXT,pool_created_at TEXT,
      cohort TEXT NOT NULL,first_cataloged_at REAL NOT NULL,last_cataloged_at REAL NOT NULL,
      raw_json TEXT,PRIMARY KEY(network,pool_address,cohort));
    CREATE TABLE IF NOT EXISTS history_ohlcv(
      network TEXT NOT NULL,pool_address TEXT NOT NULL,timeframe TEXT NOT NULL,ts INTEGER NOT NULL,
      open REAL,high REAL,low REAL,close REAL,volume REAL,
      PRIMARY KEY(network,pool_address,timeframe,ts));
    CREATE TABLE IF NOT EXISTS history_jobs(
      id INTEGER PRIMARY KEY,ts REAL NOT NULL,job TEXT NOT NULL,status TEXT NOT NULL,detail TEXT);
    CREATE TABLE IF NOT EXISTS history_backfill_state(
      network TEXT NOT NULL,pool_address TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
      empty_pages INTEGER NOT NULL DEFAULT 0,last_attempt REAL,last_success REAL,done INTEGER NOT NULL DEFAULT 0,
      last_error TEXT,PRIMARY KEY(network,pool_address));
    CREATE INDEX IF NOT EXISTS idx_history_catalog_pool ON history_pool_catalog(network,pool_address);
    CREATE INDEX IF NOT EXISTS idx_history_ohlcv_pool ON history_ohlcv(network,pool_address,timeframe,ts);
    """)
    cols={row[1] for row in db.execute("PRAGMA table_info(history_backfill_state)")}
    for name,kind in (("consecutive_errors","INTEGER NOT NULL DEFAULT 0"),
                      ("retry_after","REAL NOT NULL DEFAULT 0")):
        if name not in cols:
            db.execute(f"ALTER TABLE history_backfill_state ADD COLUMN {name} {kind}")
    db.commit(); return db

def log_job(db,job,status,detail):
    with db: db.execute("INSERT INTO history_jobs(ts,job,status,detail) VALUES(?,?,?,?)",
                        (time.time(),job,status,json.dumps(detail,allow_nan=False,separators=(",",":"))))
    if status=="ERROR":
        print(json.dumps({"event":"HISTORY_JOB_ERROR","version":VERSION,"job":job,"detail":detail},
                         allow_nan=False,separators=(",",":")),flush=True)

def parse_pool(item):
    if not isinstance(item,dict): return None
    pid=str(item.get("id") or ""); attrs=item.get("attributes") or {}
    if "_" not in pid or not isinstance(attrs,dict): return None
    # Network IDs may contain underscores (for example polygon_pos).
    network,address=pid.rsplit("_",1)
    if not network or not address: return None
    declared_address=attrs.get("address")
    if declared_address and str(declared_address)!=address: return None
    return network,address,attrs.get("name"),attrs.get("pool_created_at"),json.dumps(item,separators=(",",":"))

def catalog_response(db,body,cohort,now):
    data=body.get("data")
    if not isinstance(data,list): raise ValueError("catalog response missing data")
    n=0
    with db:
        for item in data:
            row=parse_pool(item)
            if not row: continue
            network,address,name,created,raw=row
            db.execute("""INSERT INTO history_pool_catalog
              (network,pool_address,pool_name,pool_created_at,cohort,first_cataloged_at,last_cataloged_at,raw_json)
              VALUES(?,?,?,?,?,?,?,?)
              ON CONFLICT(network,pool_address,cohort) DO UPDATE SET
              pool_name=excluded.pool_name,pool_created_at=COALESCE(excluded.pool_created_at,history_pool_catalog.pool_created_at),
              last_cataloged_at=excluded.last_cataloged_at,raw_json=excluded.raw_json""",
              (network,address,name,created,cohort,now,now,raw))
            db.execute("INSERT OR IGNORE INTO history_backfill_state(network,pool_address) VALUES(?,?)",(network,address))
            n+=1
    return n

def parse_ohlcv(body):
    try: rows=body["data"]["attributes"]["ohlcv_list"]
    except (KeyError,TypeError): raise ValueError("OHLCV response missing rows")
    if not isinstance(rows,list):
        raise ValueError("OHLCV rows must be a list, not an empty-page substitute")
    out=[]
    for index,row in enumerate(rows):
        try:
            if not isinstance(row,list) or len(row)<6: raise ValueError("bad row shape")
            timestamp=float(row[0]); values=tuple(float(x) for x in row[1:6])
            if not math.isfinite(timestamp) or timestamp<=0 or not timestamp.is_integer():
                raise ValueError("invalid timestamp")
            if any(not math.isfinite(x) or x<0 for x in values): raise ValueError("invalid numeric value")
            opening,high,low,close,volume=values
            if high<max(opening,low,close) or low>min(opening,high,close):
                raise ValueError("inconsistent OHLC bounds")
        except (TypeError,ValueError,OverflowError) as exc:
            # Reject the page, rather than advancing the cursor over invalid/missing data.
            raise ValueError(f"invalid OHLCV row {index}: {exc}") from exc
        out.append((int(timestamp),*values))
    return out


def save_ohlcv(db,network,pool,timeframe,rows):
    if timeframe != "day":
        raise ValueError("unsupported timeframe")
    with db:
        before=db.total_changes
        db.executemany("""INSERT OR IGNORE INTO history_ohlcv
          (network,pool_address,timeframe,ts,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?,?,?)""",
          [(network,pool,timeframe,*r) for r in rows])
        return db.total_changes-before

def iso(ts):
    return datetime.fromtimestamp(ts,timezone.utc).date().isoformat() if ts else None

def status(db):
    pools=db.execute("SELECT COUNT(*) FROM history_pool_catalog").fetchone()[0]
    bars,dp,lo,hi=db.execute("""SELECT COUNT(*),COUNT(DISTINCT network||':'||pool_address),
      MIN(ts),MAX(ts) FROM history_ohlcv""").fetchone()
    jobs,ok,err=db.execute("SELECT COUNT(*),SUM(status='OK'),SUM(status='ERROR') FROM history_jobs").fetchone()
    pending=db.execute("SELECT COUNT(*) FROM history_backfill_state WHERE done=0").fetchone()[0]
    suspect=db.execute("SELECT COUNT(*) FROM history_backfill_state WHERE done=1 AND last_error IS NOT NULL AND empty_pages<2").fetchone()[0]
    retrying=db.execute("SELECT COUNT(*) FROM history_backfill_state WHERE done=0 AND retry_after>?",(time.time(),)).fetchone()[0]
    return {"suspect_completed_pools":suspect,"retry_wait_pools":retrying,"event":"HISTORY_STATUS","version":VERSION,"catalog_pools":pools,"ohlcv_rows":bars,
      "ohlcv_pools":dp,"earliest_date":iso(lo),"latest_date":iso(hi),"jobs":jobs,
      "jobs_ok":ok or 0,"jobs_error":err or 0,"pending_pools":pending}

class Worker:
    def __init__(self,db,quota=None):
        self.db=db; self.last_call=0.0; self.quota=quota
    def call(self,path,params=None):
        wait=MIN_CALL_INTERVAL-(time.monotonic()-self.last_call)
        if wait>0: time.sleep(wait)
        if self.quota:self.quota.acquire("history")
        try:return gt_json(path,params)
        except urllib.error.HTTPError as exc:
            if exc.code==429 and self.quota:self.quota.penalize(retry_after_seconds(exc))
            raise
        finally:self.last_call=time.monotonic()
    def new_pools(self,page):
        n=catalog_response(self.db,self.call("/networks/new_pools",{"page":page}),
                           "new_pool_prospective",time.time())
        log_job(self.db,"catalog_new_pools","OK",{"page":page,"rows":n}); return n
    def top_pools(self,network,page):
        n=catalog_response(self.db,self.call(f"/networks/{network}/pools",
            {"page":page,"sort":"h24_tx_count_desc"}),"current_top_pool_survivor_biased",time.time())
        log_job(self.db,"catalog_top_pools","OK",{"network":network,"page":page,"rows":n}); return n
    def target(self):
        # Round-robin by least recently attempted; empty/failed pools cannot monopolize the queue.
        return self.db.execute("""SELECT s.network,s.pool_address,MIN(o.ts) earliest,s.attempts,s.empty_pages,s.consecutive_errors
          FROM history_backfill_state s LEFT JOIN history_ohlcv o
          ON o.network=s.network AND o.pool_address=s.pool_address AND o.timeframe='day'
          WHERE s.done=0 AND s.retry_after<=? GROUP BY s.network,s.pool_address
          ORDER BY COALESCE(s.last_attempt,0) ASC,s.attempts ASC LIMIT 1""",(time.time(),)).fetchone()
    def backfill(self):
        t=self.target()
        if not t: return None
        network,pool,earliest=t["network"],t["pool_address"],t["earliest"]
        now=time.time()
        with self.db:
            self.db.execute("""UPDATE history_backfill_state SET attempts=attempts+1,last_attempt=?
              WHERE network=? AND pool_address=?""",(now,network,pool))
        p={"aggregate":1,"limit":1000,"currency":"usd","token":"base","include_empty_intervals":"false"}
        if earliest: p["before_timestamp"]=int(earliest)
        try:
            rows=parse_ohlcv(self.call(f"/networks/{network}/pools/{pool}/ohlcv/day",p))
            if any(row[0]>int(now) for row in rows):
                raise ValueError("FUTURE_OHLCV_TIMESTAMP")
            if earliest and rows:
                older=[row for row in rows if row[0]<earliest]
                if not older:
                    raise ValueError("NO_BACKFILL_PROGRESS: non-empty page contains no older candles")
                rows=older
            inserted=save_ohlcv(self.db,network,pool,"day",rows)
            with self.db:
                if rows:
                    self.db.execute("""UPDATE history_backfill_state SET last_success=?,empty_pages=0
                      WHERE network=? AND pool_address=?""",(time.time(),network,pool))
                else:
                    self.db.execute("""UPDATE history_backfill_state SET empty_pages=empty_pages+1,
                      done=CASE WHEN empty_pages+1>=2 THEN 1 ELSE done END
                      WHERE network=? AND pool_address=?""",(network,pool))
            with self.db:
                self.db.execute("UPDATE history_backfill_state SET consecutive_errors=0,retry_after=0,last_error=NULL WHERE network=? AND pool_address=?",(network,pool))
            d={"network":network,"pool":pool,"rows":len(rows),"inserted":inserted,
               "earliest_before":earliest,"earliest_after":min((r[0] for r in rows),default=earliest)}
            log_job(self.db,"backfill_day_ohlcv","OK",d); return d
        except Exception as e:
            failures=int(t["consecutive_errors"] or 0)+1
            delay=min(3600.0,max(30.0,MIN_CALL_INTERVAL*2**min(failures,8)))
            with self.db:
                self.db.execute("""UPDATE history_backfill_state SET last_error=?,
                  consecutive_errors=?,retry_after=? WHERE network=? AND pool_address=?""",
                  (f"{type(e).__name__}: {str(e)[:180]}",failures,time.time()+delay,network,pool))
            # No exception may mark a pool complete. Only validated empty pages do that.
            print(json.dumps({"event":"HISTORY_BACKFILL_RETRY","version":VERSION,
                "network":network,"pool":pool,"error_type":type(e).__name__,
                "error":str(e)[:180],"consecutive_errors":failures,"retry_delay_seconds":delay,
                "done":False},separators=(",",":"),allow_nan=False),flush=True)
            raise
    def cycle(self,tick):
        try:
            m=tick%12
            if m==0: self.new_pools(1+(tick//12)%10)
            elif m==1:
                net=NETWORKS[(tick//12)%len(NETWORKS)]
                self.top_pools(net,1+(tick//(12*len(NETWORKS)))%10)
            else: self.backfill()
        except Exception as e:
            log_job(self.db,"cycle","ERROR",{"type":type(e).__name__,"message":str(e)[:250]})
        if tick%6==0:
            print(json.dumps(status(self.db),separators=(",",":")),flush=True)
    def loop(self):
        tick=0
        while True:
            self.cycle(tick); tick+=1
            time.sleep(MIN_CALL_INTERVAL+random.uniform(.4,1.4))

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--once",action="store_true"); ap.add_argument("--loop",action="store_true")
    args=ap.parse_args()
    data=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"))
    db=db_connect(data/"discovery.sqlite3"); log_job(db,"history_start","OK",{"version":VERSION,"mode":"REAL_DATA_NO_TRADING","shared_quota":True})
    quota=SharedGTQuota(data);w=Worker(db,quota=quota)
    try:
        if args.once: w.cycle(0)
        else: w.loop()
    finally:
        db.close()

if __name__=="__main__": main()
