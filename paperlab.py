"""Paper-only crypto baseline. Public market reads; no live trading capability.

Python 3.12+. Persistent ledger + conservative simulated costs + read-only UI.
This is an engineering/strategy baseline, not a validated profitable strategy.
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
import signal
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

VERSION = "paper-baseline-1.0.0"
D = Decimal
ONE = D("1")
PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "XRP": "XRPUSD"}
API = "https://api.kraken.com/0/public/"
LOG = logging.getLogger("paperlab")


def dec(value: Any) -> Decimal:
    x = D(str(value))
    if not x.is_finite():
        raise ValueError("Non-finite number")
    return x


@dataclass(frozen=True)
class Config:
    initial_cash: str = "10000"
    allocation: str = "0.10"  # Up to 10% initial capital per symbol, 30% in total.
    fee: str = "0.004"        # Assumption, NOT a verified account-specific fee tier.
    slippage: str = "0.001"   # Extra adverse fill on both sides, in addition to spread.
    stop_loss: str = "0.03"
    take_profit: str = "0.06"
    drawdown_limit: str = "0.10"
    interval_seconds: int = 900
    scan_seconds: int = 60
    max_quote_age: int = 120
    max_spread: str = "0.01"

    def __post_init__(self) -> None:
        if dec(self.initial_cash) <= 0:
            raise ValueError("Initial capital must be positive")
        for key in ("allocation", "fee", "slippage", "stop_loss", "take_profit", "drawdown_limit", "max_spread"):
            if not D("0") < dec(getattr(self, key)) < ONE:
                raise ValueError(f"Invalid {key}")
        if dec(self.allocation) * len(PAIRS) > ONE:
            raise ValueError("Combined allocation exceeds capital")
        if self.interval_seconds != 900 or self.scan_seconds < 60 or self.max_quote_age < 1:
            raise ValueError("Unsupported interval or unsafe polling rate")

    @property
    def fingerprint(self) -> str:
        data = {"version": VERSION, "config": asdict(self), "symbols": list(PAIRS)}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Market:
    symbol: str
    bid: Decimal
    ask: Decimal
    quote_ts: float
    bar_ts: int
    closes: tuple[Decimal, ...]

    def validate(self, now: float, cfg: Config) -> None:
        if self.symbol not in PAIRS:
            raise ValueError("Unsupported symbol")
        values = (self.bid, self.ask, *self.closes)
        if any(not x.is_finite() or x <= 0 for x in values):
            raise ValueError("Invalid price")
        if self.ask < self.bid or (self.ask - self.bid) / self.bid > dec(cfg.max_spread):
            raise ValueError("Crossed or excessively wide spread")
        if not -5 <= now - self.quote_ts <= cfg.max_quote_age:
            raise ValueError("Stale or future quote")
        age = now - (self.bar_ts + cfg.interval_seconds)
        if not 0 <= age <= cfg.interval_seconds + 120:
            raise ValueError("Stale or unclosed candle")
        if len(self.closes) < 21:
            raise ValueError("Insufficient closed candles")

    def trend(self) -> str:
        fast = sum(self.closes[-5:]) / 5
        slow = sum(self.closes[-20:]) / 20
        if fast > slow * D("1.001") and self.closes[-1] > max(self.closes[-21:-1]):
            return "BUY"
        return "EXIT" if fast < slow else "WAIT"


def public_get(endpoint: str, **params: Any) -> dict[str, Any]:
    """Only fixed, unauthenticated GET endpoints; never orders or account APIs."""
    if endpoint not in {"OHLC", "Spread"}:
        raise ValueError("Endpoint not allowed")
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": VERSION, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as response:
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Oversize market response")
    body = json.loads(raw)
    if body.get("error"):
        raise ValueError("Exchange rejected public data request")
    if not isinstance(body.get("result"), dict):
        raise ValueError("Malformed market response")
    return body["result"]


def only_rows(result: dict[str, Any]) -> list[Any]:
    rows = [v for k, v in result.items() if k != "last"]
    if len(rows) != 1 or not isinstance(rows[0], list):
        raise ValueError("Unexpected pair response")
    return rows[0]


def parse_market(symbol: str, ohlc: dict[str, Any], spread: dict[str, Any], now: float, cfg: Config) -> Market:
    raw = only_rows(ohlc)
    # Kraken explicitly states that the last OHLC row is uncommitted.
    closed = [r for r in raw[:-1] if int(r[0]) + cfg.interval_seconds <= now]
    recent = closed[-21:]
    if len(recent) < 21:
        raise ValueError("Insufficient committed history")
    stamps = [int(r[0]) for r in recent]
    if any(b - a != cfg.interval_seconds for a, b in zip(stamps, stamps[1:])):
        raise ValueError("Duplicate, out-of-order or missing candle")
    for row in recent:
        op, high, low, close = (dec(row[j]) for j in (1, 2, 3, 4))
        if not (0 < low <= min(op, close) <= max(op, close) <= high):
            raise ValueError("Invalid OHLC bounds")
    quotes = only_rows(spread)
    if not quotes:
        raise ValueError("No recent bid/ask update")
    quote = max(enumerate(quotes), key=lambda item: (float(item[1][0]), item[0]))[1]
    market = Market(symbol, dec(quote[1]), dec(quote[2]), float(quote[0]), stamps[-1], tuple(dec(r[4]) for r in recent))
    market.validate(now, cfg)
    return market


def fetch_market(symbol: str, cfg: Config) -> Market:
    now = time.time()
    ohlc = public_get("OHLC", pair=PAIRS[symbol], interval=15, since=int(now) - 35 * 900)
    spread = public_get("Spread", pair=PAIRS[symbol], since=int(now) - cfg.max_quote_age)
    return parse_market(symbol, ohlc, spread, time.time(), cfg)


class Ledger:
    def __init__(self, path: Path, cfg: Config) -> None:
        self.cfg = cfg
        self.lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY, ts REAL, symbol TEXT, side TEXT,
                qty TEXT, fill TEXT, fee TEXT, pnl TEXT, reason TEXT, bar_ts INTEGER,
                UNIQUE(symbol, side, bar_ts));
            CREATE TABLE IF NOT EXISTS scans (id INTEGER PRIMARY KEY, ts REAL, status TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS daily (day TEXT PRIMARY KEY, scans INTEGER, errors INTEGER);
        """)
        try:
            with self.lock, self.db:
                existing = self.db.execute("SELECT body FROM state WHERE id=1").fetchone()
                if existing:
                    if json.loads(existing[0])["config_fingerprint"] != cfg.fingerprint:
                        raise ValueError("Config/version changed: preserve old ledger and explicitly migrate or use a NEW run directory")
                else:
                    self._save({"version": VERSION, "config_fingerprint": cfg.fingerprint,
                        "initial_cash": cfg.initial_cash, "cash": cfg.initial_cash,
                        "positions": {}, "processed": {}, "high_water": cfg.initial_cash,
                        "max_drawdown": "0", "halted": False, "last_ok_ts": None,
                        "equity": cfg.initial_cash, "scans": 0, "errors": 0, "quotes": {},
                        "benchmark": {}, "benchmark_equity": None, "started_at": time.time()})
        except Exception:
            self.db.close()
            raise

    def _load(self) -> dict[str, Any]:
        return json.loads(self.db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])

    def _save(self, state: dict[str, Any]) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (json.dumps(state, allow_nan=False),))

    def _scan(self, now: float, status: str, info: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO scans(ts,status,body) VALUES(?,?,?)", (now, status, json.dumps(info)))
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        self.db.execute("INSERT INTO daily VALUES(?,1,?) ON CONFLICT(day) DO UPDATE SET scans=scans+1,errors=errors+excluded.errors", (day, int(status != "OK")))
        self.db.execute("DELETE FROM scans WHERE id <= (SELECT COALESCE(MAX(id),0)-10080 FROM scans)")

    def error(self, now: float, reason: str) -> None:
        with self.lock, self.db:
            state = self._load()
            state["scans"] += 1
            state["errors"] += 1
            self._save(state)
            self._scan(now, "PAUSED", {"reason": reason})

    def liquidation(self, market: Market) -> Decimal:
        return market.bid * (ONE - dec(self.cfg.slippage)) * (ONE - dec(self.cfg.fee))

    def _equity(self, state: dict[str, Any], markets: dict[str, Market]) -> Decimal:
        return dec(state["cash"]) + sum((dec(p["qty"]) * self.liquidation(markets[s]) for s, p in state["positions"].items()), D(0))

    def _trade(self, now: float, market: Market, side: str, qty: Decimal, fill: Decimal, fee: Decimal, pnl: Decimal, reason: str) -> None:
        self.db.execute("INSERT INTO trades(ts,symbol,side,qty,fill,fee,pnl,reason,bar_ts) VALUES(?,?,?,?,?,?,?,?,?)",
            (now, market.symbol, side, str(qty), str(fill), str(fee), str(pnl), reason, market.bar_ts))

    def process(self, markets: dict[str, Market], now: float) -> dict[str, Any]:
        if set(markets) != set(PAIRS) or any(k != v.symbol for k, v in markets.items()):
            raise ValueError("Incomplete/mismatched snapshot: no trades allowed")
        for market in markets.values():
            market.validate(now, self.cfg)
        cfg = self.cfg
        with self.lock, self.db:
            state = self._load()
            equity = self._equity(state, markets)
            high = max(dec(state["high_water"]), equity)
            drawdown = ONE - equity / high
            state["halted"] = state["halted"] or drawdown >= dec(cfg.drawdown_limit)
            state["high_water"] = str(high)
            state["max_drawdown"] = str(max(dec(state["max_drawdown"]), drawdown))
            decisions = {}
            for symbol, market in markets.items():
                last = state["processed"].get(symbol, -1)
                if market.bar_ts < last:
                    raise ValueError("Candle timestamp moved backwards")
                new_bar = market.bar_ts > last
                position = state["positions"].get(symbol)
                trend = market.trend()
                reason = "WAIT"
                if position:
                    change = self.liquidation(market) * dec(position["qty"]) / dec(position["cost"]) - ONE
                    if state["halted"]:
                        reason = "DRAWDOWN_HALT"
                    elif change <= -dec(cfg.stop_loss):
                        reason = "STOP_LOSS"
                    elif change >= dec(cfg.take_profit):
                        reason = "TAKE_PROFIT"
                    elif new_bar and trend == "EXIT":
                        reason = "TREND_EXIT"
                    if reason != "WAIT":
                        qty = dec(position["qty"])
                        fill = market.bid * (ONE - dec(cfg.slippage))
                        fee = qty * fill * dec(cfg.fee)
                        proceeds = qty * fill - fee
                        pnl = proceeds - dec(position["cost"])
                        self._trade(now, market, "SELL", qty, fill, fee, pnl, reason)
                        state["cash"] = str(dec(state["cash"]) + proceeds)
                        del state["positions"][symbol]
                elif new_bar and not state["halted"] and trend == "BUY":
                    budget = min(dec(state["cash"]), dec(cfg.initial_cash) * dec(cfg.allocation))
                    fill = market.ask * (ONE + dec(cfg.slippage))
                    qty = (budget / (fill * (ONE + dec(cfg.fee)))).quantize(D("0.00000001"), rounding=ROUND_DOWN)
                    if qty > 0:
                        fee = qty * fill * dec(cfg.fee)
                        cost = qty * fill + fee
                        self._trade(now, market, "BUY", qty, fill, fee, D(0), "BREAKOUT", )
                        state["cash"] = str(dec(state["cash"]) - cost)
                        state["positions"][symbol] = {"qty": str(qty), "cost": str(cost), "fill": str(fill), "opened_at": now}
                        reason = "BUY"
                state["processed"][symbol] = market.bar_ts
                decisions[symbol] = reason
            # A same-start, equal-weight buy-and-hold comparison with the same modeled costs.
            if not state["benchmark"]:
                for symbol, market in markets.items():
                    budget = dec(cfg.initial_cash) / len(PAIRS)
                    state["benchmark"][symbol] = str(budget / (market.ask * (ONE + dec(cfg.slippage)) * (ONE + dec(cfg.fee))))
            state["benchmark_equity"] = str(sum((dec(q) * self.liquidation(markets[s]) for s, q in state["benchmark"].items()), D(0)))
            equity = self._equity(state, markets)
            state["equity"] = str(equity)
            state["max_drawdown"] = str(max(dec(state["max_drawdown"]), ONE - equity / high))
            if ONE - equity / high >= dec(cfg.drawdown_limit):
                state["halted"] = True  # Existing positions will liquidate on next valid scan.
            state["last_ok_ts"] = now
            state["scans"] += 1
            state["quotes"] = {s: {"bid": str(m.bid), "ask": str(m.ask), "quote_ts": m.quote_ts, "closed_bar_ts": m.bar_ts} for s, m in markets.items()}
            self._save(state)
            self._scan(now, "OK", {"equity": str(equity), "decisions": decisions, "quotes": state["quotes"]})
            return decisions

    def status(self) -> dict[str, Any]:
        with self.lock:
            state = self._load()
            rows = [dict(r) for r in self.db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 100")]
            counts = self.db.execute("SELECT COUNT(*),SUM(CASE WHEN side='SELL' THEN 1 ELSE 0 END) FROM trades").fetchone()
            sells = list(self.db.execute("SELECT pnl FROM trades WHERE side='SELL'"))
            wins = sum(dec(r[0]) > 0 for r in sells)
            last = self.db.execute("SELECT ts,status,body FROM scans ORDER BY id DESC LIMIT 1").fetchone()
            state.update({"config": asdict(self.cfg), "recent_trades": rows, "trade_events": counts[0],
                "closed_trades": counts[1] or 0, "wins": wins,
                "net_return": str(dec(state["equity"]) / dec(state["initial_cash"]) - ONE),
                "last_scan": dict(last) if last else None})
            return state

    def trades_csv(self) -> bytes:
        with self.lock:
            cursor = self.db.execute("SELECT * FROM trades ORDER BY id")
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow([c[0] for c in cursor.description])
            writer.writerows(cursor)
            return ("\ufeff" + out.getvalue()).encode("utf-8")

    def close(self) -> None:
        self.db.close()


def persistent_ready(data_dir: Path) -> bool:
    """On Railway require the real mounted volume, not merely a /data directory."""
    if not os.getenv("RAILWAY_ENVIRONMENT_ID"):
        return True
    mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
    return bool(mount) and Path(mount).resolve() == data_dir.resolve() and os.path.ismount(data_dir)


PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>加密货币模拟实验室</title><style>body{font:18px system-ui;max-width:950px;margin:24px auto;padding:0 18px;line-height:1.6}h1{font-size:27px}pre{font-size:15px;white-space:pre-wrap;overflow-wrap:anywhere}a{font-weight:700}table{border-collapse:collapse;width:100%}td,th{padding:8px;border-bottom:1px solid;text-align:left}</style>
<h1>加密货币模拟实验室</h1><p><b>仅虚拟资金 · 无真实下单能力</b><br>规则策略基线，不是 ChatGPT 自主运行，不代表已经验证盈利。</p>
<p id="health">正在读取运行状态…</p><div id="metrics"></div><p><a href="/trades.csv">导出累计模拟交易 CSV</a></p><h2>最近交易</h2><pre id="trades"></pre><details><summary>参数、行情时间和诊断</summary><pre id="details"></pre></details>
<script src="/app.js"></script></html>"""
SCRIPT = """async function load(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);const s=await r.json();document.getElementById('health').textContent=(s.halted?'风险熔断：停止新开仓':(s.ready?'行情与记录正常':'暂停/尚未就绪'))+' | '+(s.storage_ready?'存储检查通过':'缺少持久化数据盘')+' | 最近有效扫描: '+(s.last_ok_ts?new Date(s.last_ok_ts*1000).toLocaleString():'尚无');const fmt=x=>Number(x).toFixed(2);document.getElementById('metrics').textContent='虚拟净值 $'+fmt(s.equity)+' | 净收益 '+fmt(Number(s.net_return)*100)+'% | 最大回撤 '+fmt(Number(s.max_drawdown)*100)+'% | 已平仓 '+s.closed_trades+' 笔 | 模拟现金 $'+fmt(s.cash)+' | 累计扫描 '+s.scans+' | 异常 '+s.errors;document.getElementById('trades').textContent=s.recent_trades.length?JSON.stringify(s.recent_trades,null,2):'尚无交易。没有信号时不会强行买入。';delete s.recent_trades;document.getElementById('details').textContent=JSON.stringify(s,null,2);}catch(e){document.getElementById('health').textContent='读取失败，不能视为正在正常运行: '+e.message;}}load();setInterval(load,60000);"""


class App:
    def __init__(self, ledger: Ledger, cfg: Config, data_dir: Path, password: str) -> None:
        self.ledger, self.cfg, self.data_dir, self.password = ledger, cfg, data_dir, password
        self.stop = threading.Event()
        self.storage_ready = persistent_ready(data_dir)
        self.loop_alive = False

    def snapshot(self) -> dict[str, Any]:
        result = self.ledger.status()
        last = result["last_ok_ts"]
        last_scan_ok = bool(result["last_scan"] and result["last_scan"]["status"] == "OK")
        result.update({"mode": "PAPER_ONLY", "storage_ready": self.storage_ready,
            "ready": bool(self.storage_ready and self.loop_alive and last_scan_ok and last is not None and 0 <= time.time() - last < 180),
            "loop_alive": self.loop_alive})
        return result

    def loop(self) -> None:
        self.loop_alive = True
        try:
            while not self.stop.is_set():
                started = time.monotonic()
                try:
                    self.storage_ready = persistent_ready(self.data_dir)
                    if not self.storage_ready:
                        raise ValueError("Persistent volume missing: all paper orders paused")
                    markets = {symbol: fetch_market(symbol, self.cfg) for symbol in PAIRS}
                    decisions = self.ledger.process(markets, time.time())
                    LOG.info(json.dumps({"event": "SCAN_OK", "mode": "PAPER_ONLY", "decisions": decisions}))
                except Exception as exc:
                    # Do not log URLs/credentials or convert failed data into made-up prices.
                    detail = str(exc) if isinstance(exc, ValueError) else "market/storage operation failed"
                    error = f"{type(exc).__name__}: {detail}; no orders"
                    self.ledger.error(time.time(), error)
                    LOG.warning(json.dumps({"event": "SCAN_PAUSED", "error": error}))
                self.stop.wait(max(1, self.cfg.scan_seconds - (time.monotonic() - started)))
        finally:
            self.loop_alive = False


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, fmt: str, *args: Any) -> None:
            pass  # Do not log request paths, headers or authorization values.

        def send_body(self, code: int, body: bytes, content_type: str = "application/json; charset=utf-8", challenge: bool = False) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'")
            if challenge:
                self.send_header("WWW-Authenticate", 'Basic realm="Paper Lab", charset="UTF-8"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/healthz":
                self.send_body(200, json.dumps({"application": VERSION, "mode": "PAPER_ONLY"}).encode())
                return
            if path == "/readyz":
                ready = app.snapshot()["ready"]
                self.send_body(200 if ready else 503, json.dumps({"ready": ready}).encode())
                return
            expected = "Basic " + base64.b64encode(("viewer:" + app.password).encode()).decode()
            actual = self.headers.get("Authorization", "")
            if not hmac.compare_digest(actual.encode(), expected.encode()):
                self.send_body(401, b'{"error":"login required"}', challenge=True)
                return
            if path == "/":
                self.send_body(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/app.js":
                self.send_body(200, SCRIPT.encode(), "application/javascript; charset=utf-8")
            elif path == "/api/status":
                self.send_body(200, json.dumps(app.snapshot()).encode())
            elif path == "/trades.csv":
                self.send_body(200, app.ledger.trades_csv(), "text/csv; charset=utf-8")
            else:
                self.send_body(404, b'{"error":"not found"}')
    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.getenv("MODE", "PAPER_ONLY") != "PAPER_ONLY":
        raise SystemExit("Only PAPER_ONLY is implemented; real trading is not supported")
    password = os.getenv("DASHBOARD_PASSWORD", "")
    if len(password) < 16 or ":" in password:
        raise SystemExit("Set DASHBOARD_PASSWORD to a new random password of at least 16 characters; do not put it in GitHub")
    cfg = Config()
    data_dir = Path(os.getenv("DATA_DIR", "/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    # Lock file lives on persistent storage. Run exactly ONE replica.
    import fcntl
    lockfile = (data_dir / ".paperlab.lock").open("a")
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Another instance already holds this ledger; use one replica")
    ledger = Ledger(data_dir / "paperlab.sqlite3", cfg)
    app = App(ledger, cfg, data_dir, password)
    host = "0.0.0.0" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "127.0.0.1"
    server = ThreadingHTTPServer((host, int(os.getenv("PORT", "8080"))), make_handler(app))
    server.daemon_threads = True
    worker = threading.Thread(target=app.loop, name="paper-scanner", daemon=True)
    worker.start()
    def shutdown(*_: Any) -> None:
        app.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info(json.dumps({"event": "APPLICATION_STARTED", "version": VERSION, "mode": "PAPER_ONLY", "storage_ready": app.storage_ready}))
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        app.stop.set()
        worker.join(timeout=60)
        server.server_close()
        if not worker.is_alive():
            ledger.close()
        lockfile.close()


if __name__ == "__main__":
    main()
