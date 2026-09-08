"""Low-priority historical crypto pool collector/backfill.

Purpose:
- Build a REAL, timestamped history corpus from GeckoTerminal public on-chain data.
- Keep historical work separate from the live discovery loop.
- Never place orders and never use wallet/account APIs.

Important limitation:
GeckoTerminal public endpoints can backfill OHLCV for known pools, but do not provide
a complete point-in-time archive of every historical new pool or historical liquidity/
holder/social state. Therefore data is tagged by cohort/source to avoid survivorship bias.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

GT = "https://api.geckoterminal.com/api/v2"
VERSION = "history-replay-0.1.0"
NETWORKS = ("solana", "eth", "base", "bsc", "arbitrum", "polygon_pos")
NETWORK_MAP = {
    "ethereum": "eth",
    "polygon": "polygon_pos",
    "solana": "solana",
    "base": "base",
    "bsc": "bsc",
    "arbitrum": "arbitrum",
}
MIN_CALL_INTERVAL = float(os.getenv("HISTORY_MIN_CALL_INTERVAL", "7.5"))  # <= ~8 calls/min
USER_AGENT = "crypto-paper-lab-history/0.1"


def gt_json(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.startswith("/networks/"):
        raise ValueError("unsupported endpoint")
    url = GT + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json;version=20230203",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read(5_000_001)
        if len(raw) > 5_000_000:
            raise ValueError("oversize response")
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError("malformed response")
    return body


def db_connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS history_pool_catalog(
        network TEXT NOT NULL,
        pool_address TEXT NOT NULL,
        pool_name TEXT,
        pool_created_at TEXT,
        cohort TEXT NOT NULL,
        first_cataloged_at REAL NOT NULL,
        last_cataloged_at REAL NOT NULL,
        raw_json TEXT,
        PRIMARY KEY(network, pool_address, cohort)
    );
    CREATE TABLE IF NOT EXISTS history_ohlcv(
        network TEXT NOT NULL,
        pool_address TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        ts INTEGER NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY(network, pool_address, timeframe, ts)
    );
    CREATE TABLE IF NOT EXISTS history_jobs(
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        job TEXT NOT NULL,
        status TEXT NOT NULL,
        detail TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_history_catalog_pool
        ON history_pool_catalog(network, pool_address);
    CREATE INDEX IF NOT EXISTS idx_history_ohlcv_pool
        ON history_ohlcv(network, pool_address, timeframe, ts);
    """)
    db.commit()
    return db


def parse_pool_row(item: dict[str, Any]) -> tuple[str, str, str | None, str | None, str] | None:
    if not isinstance(item, dict):
        return None
    pid = str(item.get("id") or "")
    attrs = item.get("attributes") or {}
    if "_" not in pid or not isinstance(attrs, dict):
        return None
    network, address = pid.split("_", 1)
    if not network or not address:
        return None
    return network, address, attrs.get("name"), attrs.get("pool_created_at"), json.dumps(item, separators=(",", ":"))


def catalog_response(db: sqlite3.Connection, body: dict[str, Any], cohort: str, now: float) -> int:
    data = body.get("data")
    if not isinstance(data, list):
        raise ValueError("catalog response missing data")
    n = 0
    with db:
        for item in data:
            row = parse_pool_row(item)
            if not row:
                continue
            network, address, name, created, raw = row
            db.execute("""
                INSERT INTO history_pool_catalog(
                    network,pool_address,pool_name,pool_created_at,cohort,
                    first_cataloged_at,last_cataloged_at,raw_json
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(network,pool_address,cohort) DO UPDATE SET
                    pool_name=excluded.pool_name,
                    pool_created_at=COALESCE(excluded.pool_created_at,history_pool_catalog.pool_created_at),
                    last_cataloged_at=excluded.last_cataloged_at,
                    raw_json=excluded.raw_json
            """, (network,address,name,created,cohort,now,now,raw))
            n += 1
    return n


def parse_ohlcv(body: dict[str, Any]) -> list[tuple[int,float,float,float,float,float]]:
    try:
        rows = body["data"]["attributes"]["ohlcv_list"]
    except (KeyError, TypeError):
        raise ValueError("OHLCV response missing rows")
    out = []
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, list) or len(r) < 6:
            continue
        ts = int(r[0])
        vals = tuple(float(x) for x in r[1:6])
        if ts <= 0 or any(x < 0 for x in vals):
            continue
        out.append((ts, *vals))
    return out


def save_ohlcv(db: sqlite3.Connection, network: str, pool: str, timeframe: str, rows) -> int:
    with db:
        before = db.total_changes
        db.executemany("""
            INSERT OR IGNORE INTO history_ohlcv(
                network,pool_address,timeframe,ts,open,high,low,close,volume
            ) VALUES(?,?,?,?,?,?,?,?,?)
        """, [(network,pool,timeframe,*r) for r in rows])
        return db.total_changes - before


def log_job(db: sqlite3.Connection, job: str, status: str, detail: dict[str,Any]) -> None:
    with db:
        db.execute("INSERT INTO history_jobs(ts,job,status,detail) VALUES(?,?,?,?)",
                   (time.time(), job, status, json.dumps(detail, allow_nan=False)))


class HistoryWorker:
    def __init__(self, db: sqlite3.Connection, min_call_interval: float = MIN_CALL_INTERVAL):
        self.db = db
        self.min_call_interval = max(6.5, min_call_interval)
        self.last_call = 0.0

    def _call(self, path: str, params: dict[str,Any] | None = None) -> dict[str,Any]:
        wait = self.min_call_interval - (time.monotonic() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        body = gt_json(path, params)
        self.last_call = time.monotonic()
        return body

    def collect_new_pools(self, page: int = 1) -> int:
        body = self._call("/networks/new_pools", {"page": page})
        n = catalog_response(self.db, body, "new_pool_prospective", time.time())
        log_job(self.db, "catalog_new_pools", "OK", {"page":page,"rows":n})
        return n

    def collect_top_pools(self, network: str, page: int = 1) -> int:
        if network not in NETWORKS:
            raise ValueError("unsupported network")
        body = self._call(f"/networks/{network}/pools",
                          {"page": page, "sort": "h24_tx_count_desc"})
        n = catalog_response(self.db, body, "current_top_pool_survivor_biased", time.time())
        log_job(self.db, "catalog_top_pools", "OK", {"network":network,"page":page,"rows":n})
        return n

    def pick_backfill_target(self) -> sqlite3.Row | None:
        return self.db.execute("""
            SELECT c.network,c.pool_address,c.pool_created_at,c.cohort,
                   MIN(o.ts) AS earliest
            FROM history_pool_catalog c
            LEFT JOIN history_ohlcv o
              ON o.network=c.network AND o.pool_address=c.pool_address AND o.timeframe='day'
            GROUP BY c.network,c.pool_address,c.cohort
            ORDER BY
              CASE c.cohort WHEN 'new_pool_prospective' THEN 0 ELSE 1 END,
              CASE WHEN earliest IS NULL THEN 0 ELSE 1 END,
              COALESCE(earliest, 0) DESC,
              c.first_cataloged_at ASC
            LIMIT 1
        """).fetchone()

    def backfill_one_page(self) -> dict[str,Any] | None:
        target = self.pick_backfill_target()
        if not target:
            return None
        network, pool = target["network"], target["pool_address"]
        earliest = target["earliest"]
        params: dict[str,Any] = {
            "aggregate": 1, "limit": 1000, "currency": "usd",
            "token": "base", "include_empty_intervals": "false",
        }
        if earliest:
            params["before_timestamp"] = int(earliest)
        body = self._call(f"/networks/{network}/pools/{pool}/ohlcv/day", params)
        rows = parse_ohlcv(body)
        inserted = save_ohlcv(self.db, network, pool, "day", rows)
        detail = {"network":network,"pool":pool,"rows":len(rows),"inserted":inserted,
                  "earliest_before":earliest}
        log_job(self.db, "backfill_day_ohlcv", "OK", detail)
        return detail

    def run_cycle(self, tick: int) -> None:
        # One public API call per cycle. This keeps historical traffic below the public limit
        # and prevents the history collector from starving the real-time scanner.
        try:
            mode = tick % 10
            if mode == 0:
                self.collect_new_pools(1 + ((tick // 10) % 10))
            elif mode == 1:
                net = NETWORKS[(tick // 10) % len(NETWORKS)]
                page = 1 + ((tick // (10 * len(NETWORKS))) % 10)
                self.collect_top_pools(net, page)
            else:
                self.backfill_one_page()
        except Exception as exc:
            log_job(self.db, "cycle", "ERROR", {"type":type(exc).__name__, "message":str(exc)[:300]})

    def loop(self) -> None:
        tick = 0
        while True:
            self.run_cycle(tick)
            tick += 1
            # Randomized delay is deliberately conservative for the ~10 calls/min public limit.
            time.sleep(max(1.0, self.min_call_interval + random.uniform(0.3, 1.5)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    data_dir = Path(os.getenv("DATA_DIR", "/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"))
    db = db_connect(data_dir / "discovery.sqlite3")
    worker = HistoryWorker(db)
    log_job(db, "history_start", "OK", {"version":VERSION, "mode":"REAL_DATA_NO_TRADING"})
    if args.once:
        worker.run_cycle(0)
        return
    worker.loop()


if __name__ == "__main__":
    main()
