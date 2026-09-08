"""Historical on-chain collector v0.2: observable, rate-limited, and stall-resistant.

Collects REAL public GeckoTerminal pool catalog + daily OHLCV into the existing
/data/discovery.sqlite3. No wallet access and no live trading capability.
"""
from __future__ import annotations
import argparse, json, os, random, sqlite3, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GT="https://api.geckoterminal.com/api/v2"
VERSION="history-replay-0.2.0"
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
    db.commit(); return db

def log_job(db,job,status,detail):
    with db: db.execute("INSERT INTO history_jobs(ts,job,status,detail) VALUES(?,?,?,?)",
                        (time.time(),job,status,json.dumps(detail,allow_nan=False,separators=(",",":"))))

def parse_pool(item):
    if not isinstance(item,dict): return None
    pid=str(item.get("id") or ""); attrs=item.get("attributes") or {}
    if "_" not in pid or not isinstance(attrs,dict): return None
    network,address=pid.split("_",1)
    if not network or not address: return None
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
    out=[]
    if not isinstance(rows,list): return out
    for r in rows:
        if not isinstance(r,list) or len(r)<6: continue
        ts=int(r[0]); vals=tuple(float(x) for x in r[1:6])
        if ts<=0 or any(x<0 for x in vals): continue
        out.append((ts,*vals))
    return out

def save_ohlcv(db,network,pool,rows):
    with db:
        before=db.total_changes
        db.executemany("""INSERT OR IGNORE INTO history_ohlcv
          (network,pool_address,timeframe,ts,open,high,low,close,volume) VALUES(?,?,'day',?,?,?,?,?,?)""",
          [(network,pool,*r) for r in rows])
        return db.total_changes-before

def iso(ts):
    return datetime.fromtimestamp(ts,timezone.utc).date().isoformat() if ts else None

def status(db):
    pools=db.execute("SELECT COUNT(*) FROM history_pool_catalog").fetchone()[0]
    bars,dp,lo,hi=db.execute("""SELECT COUNT(*),COUNT(DISTINCT network||':'||pool_address),
      MIN(ts),MAX(ts) FROM history_ohlcv""").fetchone()
    jobs,ok,err=db.execute("SELECT COUNT(*),SUM(status='OK'),SUM(status='ERROR') FROM history_jobs").fetchone()
    pending=db.execute("SELECT COUNT(*) FROM history_backfill_state WHERE done=0").fetchone()[0]
    return {"event":"HISTORY_STATUS","version":VERSION,"catalog_pools":pools,"ohlcv_rows":bars,
      "ohlcv_pools":dp,"earliest_date":iso(lo),"latest_date":iso(hi),"jobs":jobs,
      "jobs_ok":ok or 0,"jobs_error":err or 0,"pending_pools":pending}

class Worker:
    def __init__(self,db):
        self.db=db; self.last_call=0.0
    def call(self,path,params=None):
        wait=MIN_CALL_INTERVAL-(time.monotonic()-self.last_call)
        if wait>0: time.sleep(wait)
        try: return gt_json(path,params)
        finally: self.last_call=time.monotonic()
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
        return self.db.execute("""SELECT s.network,s.pool_address,MIN(o.ts) earliest,s.attempts,s.empty_pages
          FROM history_backfill_state s LEFT JOIN history_ohlcv o
          ON o.network=s.network AND o.pool_address=s.pool_address AND o.timeframe='day'
          WHERE s.done=0 GROUP BY s.network,s.pool_address
          ORDER BY COALESCE(s.last_attempt,0) ASC,s.attempts ASC LIMIT 1""").fetchone()
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
            inserted=save_ohlcv(self.db,network,pool,rows)
            with self.db:
                if rows:
                    self.db.execute("""UPDATE history_backfill_state SET last_success=?,empty_pages=0
                      WHERE network=? AND pool_address=?""",(time.time(),network,pool))
                else:
                    self.db.execute("""UPDATE history_backfill_state SET empty_pages=empty_pages+1,
                      done=CASE WHEN empty_pages+1>=2 THEN 1 ELSE done END
                      WHERE network=? AND pool_address=?""",(network,pool))
            d={"network":network,"pool":pool,"rows":len(rows),"inserted":inserted,
               "earliest_before":earliest,"earliest_after":min((r[0] for r in rows),default=earliest)}
            log_job(self.db,"backfill_day_ohlcv","OK",d); return d
        except Exception as e:
            with self.db:
                self.db.execute("""UPDATE history_backfill_state SET last_error=?,
                  done=CASE WHEN attempts>=5 THEN 1 ELSE done END WHERE network=? AND pool_address=?""",
                  (f"{type(e).__name__}: {str(e)[:180]}",network,pool))
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
    db=db_connect(data/"discovery.sqlite3"); log_job(db,"history_start","OK",{"version":VERSION,"mode":"REAL_DATA_NO_TRADING"})
    w=Worker(db)
    if args.once: w.cycle(0)
    else: w.loop()

if __name__=="__main__": main()
