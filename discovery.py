"""Paper-only early-token discovery service.

Uses public DexScreener data to find very early tokens with *evidence* of utility.
It never calls wallet, account, or order APIs and can only simulate entries/exits.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import signal
import sys
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

VERSION = "utility-discovery-0.1.0"
LOG = logging.getLogger("discovery")
D = Decimal
DEX = "https://api.dexscreener.com"
SUPPORTED_CHAINS = {"solana", "ethereum", "base", "bsc", "arbitrum", "polygon"}
ADDR = re.compile(r"^[A-Za-z0-9:_-]{20,128}$")
CHAIN = re.compile(r"^[a-z0-9_-]{2,32}$")
UTILITY_TERMS = {
    "payments", "payment", "settlement", "remittance", "infrastructure", "protocol",
    "network", "compute", "computing", "storage", "data", "oracle", "privacy",
    "identity", "authentication", "marketplace", "lending", "credit", "exchange",
    "interoperability", "bridge", "tokenization", "rwa", "real world asset", "supply chain",
    "ai", "artificial intelligence", "agent", "security", "developer", "sdk", "api",
    "gaming", "game", "depin", "wireless", "energy", "science", "research", "media",
}
MEME_TERMS = {
    "meme", "memecoin", "dog", "doge", "cat", "frog", "pepe", "shiba", "bonk",
    "community coin", "viral", "fun token", "shitcoin", "moon coin",
}


def num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        if x != x or x in (float("inf"), float("-inf")):
            return default
        return x
    except (TypeError, ValueError):
        return default


def public_json(path: str) -> Any:
    if path == "/token-profiles/latest/v1":
        pass
    elif path.startswith("/token-pairs/v1/"):
        parts = path.split("/")
        if len(parts) != 5 or not CHAIN.fullmatch(parts[3]) or not ADDR.fullmatch(parts[4]):
            raise ValueError("Unsafe token-pairs path")
    else:
        raise ValueError("Endpoint not allowed")
    req = urllib.request.Request(DEX + path, headers={"User-Agent": VERSION, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        raw = response.read(3_000_001)
        if len(raw) > 3_000_000:
            raise ValueError("Oversize response")
    return json.loads(raw)


def best_pair(pairs: Any, address: str) -> dict[str, Any] | None:
    if not isinstance(pairs, list):
        return None
    address_l = address.lower()
    eligible = []
    for p in pairs:
        if not isinstance(p, dict):
            continue
        base = p.get("baseToken") or {}
        if str(base.get("address", "")).lower() != address_l:
            continue
        if not p.get("priceUsd"):
            continue
        eligible.append(p)
    if not eligible:
        return None
    return max(eligible, key=lambda p: num((p.get("liquidity") or {}).get("usd")))


def utility_evidence(profile: dict[str, Any], pair: dict[str, Any]) -> tuple[list[str], bool]:
    desc = str(profile.get("description") or "").strip()
    text = desc.lower()
    websites = (pair.get("info") or {}).get("websites") or []
    socials = (pair.get("info") or {}).get("socials") or []
    links = profile.get("links") or []
    utility_hits = sorted(t for t in UTILITY_TERMS if t in text)
    meme_hit = any(t in text for t in MEME_TERMS)
    evidence = []
    if desc:
        evidence.append("profile_description")
    if websites:
        evidence.append("website")
    if socials or links:
        evidence.append("social_or_profile_link")
    if utility_hits:
        evidence.append("utility_terms:" + ",".join(utility_hits[:5]))
    return evidence, meme_hit


@dataclass(frozen=True)
class Assessment:
    chain: str
    address: str
    symbol: str
    name: str
    pair_address: str
    price: float
    market_cap: float
    liquidity: float
    volume24: float
    buys_h1: int
    sells_h1: int
    age_hours: float
    score: int
    evidence: str
    dex_url: str
    eligible: bool


def assess(profile: dict[str, Any], pair: dict[str, Any], now_ms: int) -> Assessment:
    base = pair.get("baseToken") or {}
    chain = str(pair.get("chainId") or profile.get("chainId") or "")
    address = str(base.get("address") or profile.get("tokenAddress") or "")
    created = int(num(pair.get("pairCreatedAt"), now_ms))
    age_h = max(0.0, (now_ms - created) / 3_600_000)
    market_cap = num(pair.get("marketCap")) or num(pair.get("fdv"))
    liquidity = num((pair.get("liquidity") or {}).get("usd"))
    volume24 = num((pair.get("volume") or {}).get("h24"))
    h1 = (pair.get("txns") or {}).get("h1") or {}
    buys, sells = int(num(h1.get("buys"))), int(num(h1.get("sells")))
    evidence_parts, meme_hit = utility_evidence(profile, pair)
    websites = (pair.get("info") or {}).get("websites") or []
    socials = (pair.get("info") or {}).get("socials") or []
    desc = str(profile.get("description") or "")
    utility_hit = any(t in desc.lower() for t in UTILITY_TERMS)
    score = 0
    score += 2 if websites else 0
    score += 1 if socials or profile.get("links") else 0
    score += 2 if utility_hit else 0
    score += 1 if 50_000 <= market_cap <= 5_000_000 else 0
    score += 1 if liquidity >= 30_000 else 0
    score += 1 if volume24 >= 50_000 else 0
    score += 1 if buys >= 5 and buys >= max(1, sells) * 1.10 else 0
    score += 1 if age_h <= 72 else 0
    if meme_hit:
        score -= 3
    if num((pair.get("boosts") or {}).get("active")) > 0:
        score -= 1
        evidence_parts.append("paid_boost_active")
    eligible = bool(
        score >= 7 and not meme_hit and utility_hit and websites and
        50_000 <= market_cap <= 5_000_000 and liquidity >= 30_000 and
        volume24 >= 20_000 and age_h <= 168
    )
    return Assessment(
        chain=chain, address=address, symbol=str(base.get("symbol") or "?"),
        name=str(base.get("name") or "?"), pair_address=str(pair.get("pairAddress") or ""),
        price=num(pair.get("priceUsd")), market_cap=market_cap, liquidity=liquidity,
        volume24=volume24, buys_h1=buys, sells_h1=sells, age_hours=age_h, score=score,
        evidence="; ".join(evidence_parts), dex_url=str(pair.get("url") or ""), eligible=eligible,
    )


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS candidates(
                chain TEXT, address TEXT, symbol TEXT, name TEXT, pair_address TEXT,
                first_seen REAL, last_seen REAL, entry_price REAL, current_price REAL,
                market_cap REAL, liquidity REAL, volume24 REAL, score INTEGER,
                evidence TEXT, dex_url TEXT, max_return REAL DEFAULT 0,
                PRIMARY KEY(chain,address));
            CREATE TABLE IF NOT EXISTS positions(
                id INTEGER PRIMARY KEY, chain TEXT, address TEXT, symbol TEXT,
                opened_at REAL, closed_at REAL, entry_price REAL, exit_price REAL,
                usd REAL, qty REAL, status TEXT, reason TEXT, pnl REAL DEFAULT 0,
                UNIQUE(chain,address,status));
            CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY, ts REAL, status TEXT, body TEXT);
        """)
        self.db.commit()

    def observe(self, a: Assessment, now: float) -> None:
        with self.lock, self.db:
            old = self.db.execute("SELECT entry_price,max_return FROM candidates WHERE chain=? AND address=?", (a.chain, a.address)).fetchone()
            first_price = a.price if not old else old["entry_price"]
            max_ret = 0.0 if not old or not first_price else max(old["max_return"], a.price / first_price - 1)
            self.db.execute("""INSERT INTO candidates(chain,address,symbol,name,pair_address,first_seen,last_seen,entry_price,current_price,market_cap,liquidity,volume24,score,evidence,dex_url,max_return)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(chain,address) DO UPDATE SET last_seen=excluded.last_seen,current_price=excluded.current_price,
                market_cap=excluded.market_cap,liquidity=excluded.liquidity,volume24=excluded.volume24,score=excluded.score,
                evidence=excluded.evidence,dex_url=excluded.dex_url,max_return=excluded.max_return""",
                (a.chain,a.address,a.symbol,a.name,a.pair_address,now,now,first_price,a.price,a.market_cap,a.liquidity,a.volume24,a.score,a.evidence,a.dex_url,max_ret))

    def maybe_open(self, a: Assessment, now: float, usd: float = 100.0) -> bool:
        if not a.eligible or a.price <= 0:
            return False
        with self.lock, self.db:
            open_count = self.db.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0]
            exists = self.db.execute("SELECT 1 FROM positions WHERE chain=? AND address=?", (a.chain,a.address)).fetchone()
            if open_count >= 20 or exists:
                return False
            self.db.execute("INSERT INTO positions(chain,address,symbol,opened_at,entry_price,usd,qty,status,reason) VALUES(?,?,?,?,?,?,?,?,?)",
                (a.chain,a.address,a.symbol,now,a.price,usd,usd/a.price,"OPEN","UTILITY_SCORE"))
            return True

    def update_position(self, a: Assessment, now: float) -> str | None:
        with self.lock, self.db:
            p = self.db.execute("SELECT * FROM positions WHERE chain=? AND address=? AND status='OPEN'", (a.chain,a.address)).fetchone()
            if not p or a.price <= 0:
                return None
            ret = a.price / p["entry_price"] - 1
            age = now - p["opened_at"]
            reason = None
            if ret <= -0.30:
                reason = "STOP_-30%"
            elif ret >= 1.00:
                reason = "TAKE_+100%"
            elif a.liquidity < 10_000:
                reason = "LIQUIDITY_BREAK"
            elif age >= 7 * 86400:
                reason = "TIME_7D"
            if reason:
                pnl = p["qty"] * a.price - p["usd"]
                self.db.execute("UPDATE positions SET closed_at=?,exit_price=?,status='CLOSED',reason=?,pnl=? WHERE id=?",
                    (now,a.price,reason,pnl,p["id"]))
            return reason

    def open_keys(self) -> list[tuple[str,str]]:
        with self.lock:
            return [(r[0],r[1]) for r in self.db.execute("SELECT chain,address FROM positions WHERE status='OPEN'")]

    def log_scan(self, now: float, status: str, body: dict[str, Any]) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO scans(ts,status,body) VALUES(?,?,?)", (now,status,json.dumps(body,allow_nan=False)))
            self.db.execute("DELETE FROM scans WHERE id <= (SELECT COALESCE(MAX(id),0)-10080 FROM scans)")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            cands = [dict(r) for r in self.db.execute("SELECT * FROM candidates ORDER BY first_seen DESC LIMIT 100")]
            positions = [dict(r) for r in self.db.execute("SELECT * FROM positions ORDER BY opened_at DESC LIMIT 100")]
            scans = self.db.execute("SELECT COUNT(*),SUM(status!='OK') FROM scans").fetchone()
            closed = [p for p in positions if p["status"] == "CLOSED"]
            wins = sum(p["pnl"] > 0 for p in closed)
            total_pnl = sum(p["pnl"] for p in closed)
            last = self.db.execute("SELECT ts,status,body FROM scans ORDER BY id DESC LIMIT 1").fetchone()
            return {"version":VERSION,"candidates":cands,"positions":positions,"scans":scans[0] or 0,
                    "errors":scans[1] or 0,"closed":len(closed),"wins":wins,"realized_pnl":total_pnl,
                    "last_scan":dict(last) if last else None}

    def csv_bytes(self) -> bytes:
        with self.lock:
            cur = self.db.execute("SELECT * FROM candidates ORDER BY first_seen")
            out = io.StringIO(); w = csv.writer(out); w.writerow([d[0] for d in cur.description]); w.writerows(cur)
            return ("\ufeff"+out.getvalue()).encode("utf-8")


def persistent_ready(data_dir: Path) -> bool:
    if not os.getenv("RAILWAY_ENVIRONMENT_ID"):
        return True
    mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
    return bool(mount) and Path(mount).resolve() == data_dir.resolve() and os.path.ismount(data_dir)


class App:
    def __init__(self, store: Store, data_dir: Path, password: str) -> None:
        self.store, self.data_dir, self.password = store, data_dir, password
        self.stop = threading.Event(); self.loop_alive = False; self.storage_ready = persistent_ready(data_dir)

    def _profiles(self) -> list[dict[str,Any]]:
        raw = public_json("/token-profiles/latest/v1")
        return [p for p in raw if isinstance(p,dict)] if isinstance(raw,list) else []

    def scan_once(self) -> dict[str,Any]:
        now = time.time(); now_ms = int(now*1000); opened=[]; closed=[]; observed=0
        if not persistent_ready(self.data_dir):
            raise ValueError("Persistent volume missing")
        profiles = self._profiles()[:80]
        seen=set()
        for p in profiles:
            chain=str(p.get("chainId") or "").lower(); addr=str(p.get("tokenAddress") or "")
            if chain not in SUPPORTED_CHAINS or not ADDR.fullmatch(addr):
                continue
            pair = best_pair(public_json(f"/token-pairs/v1/{chain}/{addr}"), addr)
            if not pair:
                continue
            a=assess(p,pair,now_ms); seen.add((chain,addr)); self.store.observe(a,now); observed+=1
            if self.store.update_position(a,now): closed.append(f"{chain}:{a.symbol}")
            if self.store.maybe_open(a,now): opened.append(f"{chain}:{a.symbol}")
            if observed >= 30:
                break
        # Refresh positions that dropped out of the latest-profile feed.
        for chain,addr in self.store.open_keys():
            if (chain,addr) in seen: continue
            pair=best_pair(public_json(f"/token-pairs/v1/{chain}/{addr}"),addr)
            if not pair: continue
            a=assess({"chainId":chain,"tokenAddress":addr},pair,now_ms)
            self.store.observe(a,now)
            if self.store.update_position(a,now): closed.append(f"{chain}:{a.symbol}")
        result={"observed":observed,"opened":opened,"closed":closed}
        self.store.log_scan(now,"OK",result); return result

    def loop(self) -> None:
        self.loop_alive=True
        try:
            while not self.stop.is_set():
                started=time.monotonic()
                try:
                    self.storage_ready=persistent_ready(self.data_dir)
                    r=self.scan_once(); LOG.info(json.dumps({"event":"DISCOVERY_SCAN_OK",**r}))
                except Exception as exc:
                    detail=str(exc) if isinstance(exc,ValueError) else "public-data/storage operation failed"
                    self.store.log_scan(time.time(),"PAUSED",{"reason":f"{type(exc).__name__}: {detail}"})
                    LOG.warning(json.dumps({"event":"DISCOVERY_SCAN_PAUSED","error":detail}))
                self.stop.wait(max(1,60-(time.monotonic()-started)))
        finally: self.loop_alive=False

    def snapshot(self) -> dict[str,Any]:
        s=self.store.snapshot(); last=s["last_scan"]
        s.update({"mode":"PAPER_ONLY","storage_ready":self.storage_ready,"loop_alive":self.loop_alive,
                  "ready":bool(self.storage_ready and self.loop_alive and last and last["status"]=="OK" and time.time()-last["ts"]<180)})
        return s


PAGE="""<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>早期实用型代币模拟扫描</title><style>body{font:17px system-ui;max-width:1100px;margin:20px auto;padding:0 16px;line-height:1.55}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:6px;border-bottom:1px solid #aaa;text-align:left}code{word-break:break-all}.ok{font-weight:700}</style><h1>早期实用型代币模拟扫描</h1><p><b>只用虚拟资金 · 无真实下单能力。</b> “实用型”仅表示公开资料出现用途证据，不等于项目已被验证或值得投资。</p><p id=s>读取中…</p><p><a href=/candidates.csv>导出累计候选 CSV</a></p><h2>虚拟仓位</h2><div id=p></div><h2>最近候选</h2><div id=c></div><script src=/app.js></script></html>"""
SCRIPT="""const f=n=>Number(n||0).toLocaleString(undefined,{maximumFractionDigits:2});function table(rows,cols){if(!rows.length)return '暂无';let h='<table><tr>'+cols.map(x=>'<th>'+x[0]+'</th>').join('')+'</tr>';for(const r of rows)h+='<tr>'+cols.map(x=>'<td>'+String(x[1](r)??'')+'</td>').join('')+'</tr>';return h+'</table>'}async function load(){try{let r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error(r.status);let x=await r.json();document.getElementById('s').textContent=(x.ready?'扫描正常':'未就绪')+' | 扫描 '+x.scans+' | 异常 '+x.errors+' | 已平仓 '+x.closed+' | 胜 '+x.wins+' | 已实现模拟盈亏 $'+f(x.realized_pnl);document.getElementById('p').innerHTML=table(x.positions,[['币',r=>r.symbol],['链',r=>r.chain],['状态',r=>r.status],['投入$',r=>f(r.usd)],['入场',r=>r.entry_price],['退出',r=>r.exit_price||''],['盈亏$',r=>f(r.pnl)],['原因',r=>r.reason]]);document.getElementById('c').innerHTML=table(x.candidates.slice(0,50),[['币',r=>r.symbol],['链',r=>r.chain],['评分',r=>r.score],['市值',r=>'$'+f(r.market_cap)],['流动性',r=>'$'+f(r.liquidity)],['24h量',r=>'$'+f(r.volume24)],['首次价',r=>r.entry_price],['现价',r=>r.current_price],['最高涨幅',r=>f(r.max_return*100)+'%'],['用途证据',r=>r.evidence]]);}catch(e){document.getElementById('s').textContent='读取失败: '+e.message}}load();setInterval(load,60000);"""


def make_handler(app:App):
    class H(BaseHTTPRequestHandler):
        def log_message(self,*_): pass
        def sendb(self,code:int,b:bytes,ct="application/json; charset=utf-8",challenge=False):
            self.send_response(code); self.send_header("Content-Type",ct); self.send_header("Content-Length",str(len(b))); self.send_header("Cache-Control","no-store"); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("X-Frame-Options","DENY");
            if challenge:self.send_header("WWW-Authenticate",'Basic realm="Discovery", charset="UTF-8"')
            self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            path=urllib.parse.urlsplit(self.path).path
            if path=="/healthz": return self.sendb(200,json.dumps({"application":VERSION,"mode":"PAPER_ONLY"}).encode())
            if path=="/readyz":
                ready=app.snapshot()["ready"]; return self.sendb(200 if ready else 503,json.dumps({"ready":ready}).encode())
            expected="Basic "+base64.b64encode(("viewer:"+app.password).encode()).decode(); actual=self.headers.get("Authorization","")
            if not hmac.compare_digest(actual.encode(),expected.encode()): return self.sendb(401,b'{"error":"login required"}',challenge=True)
            if path=="/": return self.sendb(200,PAGE.encode(),"text/html; charset=utf-8")
            if path=="/app.js": return self.sendb(200,SCRIPT.encode(),"application/javascript; charset=utf-8")
            if path=="/api/status": return self.sendb(200,json.dumps(app.snapshot(),allow_nan=False).encode())
            if path=="/candidates.csv": return self.sendb(200,app.store.csv_bytes(),"text/csv; charset=utf-8")
            return self.sendb(404,b'{"error":"not found"}')
    return H


def main():
    logging.basicConfig(level=logging.INFO,stream=sys.stdout,format="%(asctime)s %(levelname)s %(message)s")
    if os.getenv("MODE","PAPER_ONLY")!="PAPER_ONLY": raise SystemExit("Only PAPER_ONLY supported")
    password=os.getenv("DASHBOARD_PASSWORD","")
    if len(password)<16 or ":" in password: raise SystemExit("Set DASHBOARD_PASSWORD to at least 16 chars without colon")
    data_dir=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data")); data_dir.mkdir(parents=True,exist_ok=True)
    import fcntl
    lock=(data_dir/".discovery.lock").open("a"); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    store=Store(data_dir/"discovery.sqlite3"); app=App(store,data_dir,password)
    host="0.0.0.0" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "127.0.0.1"; server=ThreadingHTTPServer((host,int(os.getenv("PORT","8080"))),make_handler(app)); server.daemon_threads=True
    worker=threading.Thread(target=app.loop,name="utility-discovery",daemon=True); worker.start()
    def shutdown(*_): app.stop.set(); threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,shutdown); signal.signal(signal.SIGINT,shutdown)
    LOG.info(json.dumps({"event":"DISCOVERY_APPLICATION_STARTED","version":VERSION,"storage_ready":app.storage_ready}))
    try: server.serve_forever(poll_interval=.5)
    finally: app.stop.set(); worker.join(timeout=30); server.server_close(); store.db.close(); lock.close()

if __name__=="__main__": main()
