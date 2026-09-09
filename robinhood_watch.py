"""Robinhood Chain / Pons launch watcher for the paper-only discovery stack.

Purpose: close the blind spot where Robinhood/Pons launches were absent from the
utility-first DexScreener profile feed. This worker is WATCH-ONLY: it never
opens paper positions and has no wallet, key, or order capability.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "robinhood-watch-0.1.0"
CHAIN = "robinhood"
RPC_DEFAULT = "https://rpc.mainnet.chain.robinhood.com"
DEX = "https://api.dexscreener.com"
PONS_V2_FACTORY_DEFAULT = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
TOKEN_LAUNCHED_SIGNATURE = "TokenLaunched(address,address,address,address,uint256,uint256)"
ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
HEX = re.compile(r"^0x[0-9a-fA-F]*$")
MISSED_CASES_PATH = Path(__file__).with_name("missed_opportunities.json")


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, "version": VERSION, **fields}, separators=(",", ":"), allow_nan=False), flush=True)


def num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if out == out and out not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError):
        return default


def rpc_call(method: str, params: list[Any], rpc_url: str | None = None) -> Any:
    if method not in {"eth_blockNumber", "web3_sha3", "eth_getLogs", "eth_call", "eth_getBlockByNumber"}:
        raise ValueError("RPC method not allowed")
    url = rpc_url or os.getenv("ROBINHOOD_RPC_URL", RPC_DEFAULT)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, separators=(",", ":")).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": VERSION})
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("oversize RPC response")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("RPC error")
    if "result" not in payload:
        raise ValueError("RPC result missing")
    return payload["result"]


def event_topic(rpc_url: str | None = None) -> str:
    encoded = "0x" + TOKEN_LAUNCHED_SIGNATURE.encode().hex()
    topic = str(rpc_call("web3_sha3", [encoded], rpc_url))
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", topic):
        raise ValueError("invalid event topic")
    return topic.lower()


def address_from_topic(topic: str) -> str:
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", str(topic)):
        raise ValueError("invalid indexed address topic")
    return "0x" + str(topic)[-40:].lower()


def _decode_abi_text(raw: str) -> str:
    if not isinstance(raw, str) or not HEX.fullmatch(raw) or len(raw) < 2:
        return "?"
    data = bytes.fromhex(raw[2:])
    if not data:
        return "?"
    try:
        # Standard ABI dynamic string: offset | ... | length | bytes.
        if len(data) >= 64:
            offset = int.from_bytes(data[:32], "big")
            if 0 <= offset <= len(data) - 32:
                length = int.from_bytes(data[offset:offset + 32], "big")
                start = offset + 32
                if 0 <= length <= 256 and start + length <= len(data):
                    text = data[start:start + length].decode("utf-8", "replace").strip("\x00 ")
                    return text[:96] or "?"
        # Some legacy tokens return bytes32 for symbol/name.
        text = data[:32].rstrip(b"\x00").decode("utf-8", "replace").strip()
        return text[:96] or "?"
    except (UnicodeDecodeError, ValueError, OverflowError):
        return "?"


def erc20_text(token: str, selector: str, rpc_url: str | None = None) -> str:
    if not ADDR.fullmatch(token) or selector not in {"0x06fdde03", "0x95d89b41"}:
        raise ValueError("unsafe eth_call")
    try:
        raw = rpc_call("eth_call", [{"to": token, "data": selector}, "latest"], rpc_url)
        return _decode_abi_text(str(raw))
    except Exception:
        return "?"


def block_timestamp(block_number: int, rpc_url: str | None = None) -> float | None:
    try:
        block = rpc_call("eth_getBlockByNumber", [hex(block_number), False], rpc_url)
        if not isinstance(block, dict):
            return None
        value = int(str(block.get("timestamp") or "0x0"), 16)
        return float(value) if value > 0 else None
    except Exception:
        return None


def pons_factories() -> tuple[str, ...]:
    raw = os.getenv("PONS_V2_FACTORIES", PONS_V2_FACTORY_DEFAULT)
    out = []
    for item in raw.split(","):
        addr = item.strip()
        if ADDR.fullmatch(addr):
            out.append(addr.lower())
    if not out:
        raise ValueError("no valid Pons V2 factory configured")
    return tuple(dict.fromkeys(out))


def dex_pairs(token: str) -> list[dict[str, Any]]:
    if not ADDR.fullmatch(token):
        raise ValueError("invalid token address")
    path = f"/token-pairs/v1/{CHAIN}/{token}"
    req = urllib.request.Request(DEX + path, headers={"Accept": "application/json", "User-Agent": VERSION})
    with urllib.request.urlopen(req, timeout=10) as response:
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("oversize DexScreener response")
    body = json.loads(raw)
    return [x for x in body if isinstance(x, dict)] if isinstance(body, list) else []


def best_pair(pairs: list[dict[str, Any]], token: str) -> dict[str, Any] | None:
    token_l = token.lower()
    eligible = []
    for pair in pairs:
        base = pair.get("baseToken") or {}
        if str(pair.get("chainId") or "").lower() != CHAIN:
            continue
        if str(base.get("address") or "").lower() != token_l:
            continue
        if not pair.get("priceUsd"):
            continue
        eligible.append(pair)
    if not eligible:
        return None
    return max(eligible, key=lambda p: num((p.get("liquidity") or {}).get("usd")))


def momentum(pair: dict[str, Any], launch_ts: float | None, now: float) -> tuple[str, int, str]:
    liquidity = num((pair.get("liquidity") or {}).get("usd"))
    volume = num((pair.get("volume") or {}).get("h24"))
    h1 = (pair.get("txns") or {}).get("h1") or {}
    buys, sells = int(num(h1.get("buys"))), int(num(h1.get("sells")))
    age_h = ((now - launch_ts) / 3600.0) if launch_ts and now >= launch_ts else None
    turnover = volume / liquidity if liquidity > 0 else 0.0
    score = 0
    reasons = []
    if age_h is not None and age_h <= 24:
        score += 2; reasons.append("age<=24h")
    elif age_h is not None and age_h <= 72:
        score += 1; reasons.append("age<=72h")
    if liquidity >= 30_000:
        score += 2; reasons.append("liq>=30k")
    if volume >= 100_000:
        score += 2; reasons.append("vol24>=100k")
    if turnover >= 3:
        score += 2; reasons.append("vol/liq>=3x")
    if buys + sells >= 10:
        score += 1; reasons.append("h1_tx>=10")
    if buys >= 5 and buys >= max(1, sells) * 1.05:
        score += 1; reasons.append("buy_pressure")
    level = "HOT" if score >= 8 else "WARM" if score >= 5 else "WATCH"
    return level, score, ",".join(reasons) or "launch_only"


def db_connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA busy_timeout=10000")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS robinhood_launch_watch(
      address TEXT PRIMARY KEY, symbol TEXT, name TEXT, platform TEXT NOT NULL,
      source TEXT NOT NULL, first_seen REAL NOT NULL, launch_ts REAL,
      launch_block INTEGER, tx_hash TEXT, last_enriched REAL,
      price REAL, market_cap REAL, liquidity REAL, volume24 REAL,
      buys_h1 INTEGER, sells_h1 INTEGER, alert_level TEXT NOT NULL DEFAULT 'BIRTH',
      momentum_score INTEGER NOT NULL DEFAULT 0, watch_reason TEXT, dex_url TEXT);
    CREATE TABLE IF NOT EXISTS robinhood_watch_state(
      key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS missed_opportunity_cases(
      case_id TEXT PRIMARY KEY, chain TEXT NOT NULL, address TEXT NOT NULL,
      symbol TEXT, launch_date TEXT, classification TEXT, root_causes TEXT,
      desired_detection TEXT, mode TEXT, inserted_at REAL NOT NULL);
    """)
    db.commit()
    return db


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def seed_missed_cases(db: sqlite3.Connection, path: Path = MISSED_CASES_PATH) -> int:
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("missed-opportunity fixture must be a list")
    inserted = 0
    now = time.time()
    with db:
        for case in data:
            if not isinstance(case, dict):
                continue
            address = str(case.get("address") or "").lower()
            if not ADDR.fullmatch(address):
                continue
            cur = db.execute("""INSERT OR IGNORE INTO missed_opportunity_cases
              (case_id,chain,address,symbol,launch_date,classification,root_causes,desired_detection,mode,inserted_at)
              VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                str(case.get("case_id") or f"{CHAIN}:{address}"), str(case.get("chain") or CHAIN), address,
                str(case.get("symbol") or "?"), case.get("launch_date"), case.get("classification"),
                json.dumps(case.get("root_causes") or [], separators=(",", ":")),
                json.dumps(case.get("desired_detection") or [], separators=(",", ":")),
                str(case.get("mode") or "RETROSPECTIVE_WATCH_ONLY"), now))
            inserted += int(cur.rowcount > 0)
            # Explicitly keep the missed case on the live watch list from now on.
            db.execute("""INSERT INTO robinhood_launch_watch
              (address,symbol,name,platform,source,first_seen,alert_level,watch_reason)
              VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(address) DO NOTHING""",
              (address, str(case.get("symbol") or "?"), str(case.get("symbol") or "?"),
               str(case.get("platform") or "Pons V2"), "MISSED_CASE_FIXTURE", now, "WATCH", "retrospective_case"))
    return inserted


def get_state(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM robinhood_watch_state WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else None


def set_state(db: sqlite3.Connection, key: str, value: str) -> None:
    with db:
        db.execute("INSERT INTO robinhood_watch_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def upsert_launch(db: sqlite3.Connection, token: str, block: int, tx_hash: str, launch_ts: float | None,
                  symbol: str = "?", name: str = "?", source: str = "PONS_V2_TOKEN_LAUNCHED") -> bool:
    now = time.time()
    old = db.execute("SELECT 1 FROM robinhood_launch_watch WHERE address=?", (token.lower(),)).fetchone()
    with db:
        db.execute("""INSERT INTO robinhood_launch_watch
          (address,symbol,name,platform,source,first_seen,launch_ts,launch_block,tx_hash,alert_level,watch_reason)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(address) DO UPDATE SET
            symbol=CASE WHEN robinhood_launch_watch.symbol IN ('?','') THEN excluded.symbol ELSE robinhood_launch_watch.symbol END,
            name=CASE WHEN robinhood_launch_watch.name IN ('?','') THEN excluded.name ELSE robinhood_launch_watch.name END,
            platform=excluded.platform,source=excluded.source,
            launch_ts=COALESCE(robinhood_launch_watch.launch_ts,excluded.launch_ts),
            launch_block=COALESCE(robinhood_launch_watch.launch_block,excluded.launch_block),
            tx_hash=COALESCE(robinhood_launch_watch.tx_hash,excluded.tx_hash)""",
          (token.lower(), symbol, name, "Pons V2", source, now, launch_ts, block, tx_hash, "BIRTH", "factory_event"))
    return old is None


def upsert_candidate_watch(db: sqlite3.Connection, row: sqlite3.Row, now: float) -> None:
    if not _table_exists(db, "candidates"):
        return
    price = num(row["price"]); liquidity = num(row["liquidity"]); market_cap = num(row["market_cap"]); volume = num(row["volume24"])
    reason = f"WATCH_ONLY:robinhood_pons; alert={row['alert_level']}; momentum={row['momentum_score']}; {row['watch_reason'] or ''}"
    old = db.execute("SELECT entry_price,max_return FROM candidates WHERE chain=? AND address=?", (CHAIN, row["address"])).fetchone()
    entry = price if not old or not num(old["entry_price"]) else num(old["entry_price"])
    old_max = num(old["max_return"]) if old else 0.0
    max_return = max(old_max, price / entry - 1) if price > 0 and entry > 0 else old_max
    with db:
        db.execute("""INSERT INTO candidates
          (chain,address,symbol,name,pair_address,first_seen,last_seen,entry_price,current_price,
           market_cap,liquidity,volume24,score,evidence,dex_url,max_return)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(chain,address) DO UPDATE SET
            last_seen=excluded.last_seen,current_price=excluded.current_price,market_cap=excluded.market_cap,
            liquidity=excluded.liquidity,volume24=excluded.volume24,evidence=excluded.evidence,
            dex_url=excluded.dex_url,max_return=excluded.max_return""",
          (CHAIN, row["address"], row["symbol"] or "?", row["name"] or "?", "", row["first_seen"], now,
           entry, price, market_cap, liquidity, volume, 0, reason, row["dex_url"] or "", max_return))


def enrich_one(db: sqlite3.Connection, address: str, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    pairs = dex_pairs(address)
    pair = best_pair(pairs, address)
    before = db.execute("SELECT alert_level,launch_ts FROM robinhood_launch_watch WHERE address=?", (address,)).fetchone()
    if not before:
        raise ValueError("unknown watch token")
    if not pair:
        with db:
            db.execute("UPDATE robinhood_launch_watch SET last_enriched=? WHERE address=?", (now, address))
        return {"address": address, "pair": False, "changed": False}
    base = pair.get("baseToken") or {}
    h1 = (pair.get("txns") or {}).get("h1") or {}
    level, score, reason = momentum(pair, before["launch_ts"], now)
    values = dict(
        symbol=str(base.get("symbol") or "?"), name=str(base.get("name") or "?"), price=num(pair.get("priceUsd")),
        market_cap=num(pair.get("marketCap")) or num(pair.get("fdv")), liquidity=num((pair.get("liquidity") or {}).get("usd")),
        volume24=num((pair.get("volume") or {}).get("h24")), buys_h1=int(num(h1.get("buys"))), sells_h1=int(num(h1.get("sells"))),
        alert_level=level, momentum_score=score, watch_reason=reason, dex_url=str(pair.get("url") or ""))
    with db:
        db.execute("""UPDATE robinhood_launch_watch SET symbol=?,name=?,last_enriched=?,price=?,market_cap=?,liquidity=?,volume24=?,
          buys_h1=?,sells_h1=?,alert_level=?,momentum_score=?,watch_reason=?,dex_url=? WHERE address=?""",
          (values["symbol"], values["name"], now, values["price"], values["market_cap"], values["liquidity"], values["volume24"],
           values["buys_h1"], values["sells_h1"], values["alert_level"], values["momentum_score"], values["watch_reason"], values["dex_url"], address))
    current = db.execute("SELECT * FROM robinhood_launch_watch WHERE address=?", (address,)).fetchone()
    if current:
        upsert_candidate_watch(db, current, now)
    return {"address": address, "pair": True, "changed": str(before["alert_level"]) != level, **values}


def scan_factory_logs(db: sqlite3.Connection, rpc_url: str | None = None) -> list[dict[str, Any]]:
    head = int(str(rpc_call("eth_blockNumber", [], rpc_url)), 16)
    backfill = max(100, min(50_000, int(os.getenv("PONS_BACKFILL_BLOCKS", "5000"))))
    saved = get_state(db, "pons_v2_cursor")
    cursor = int(saved) if saved is not None else max(0, head - backfill)
    if cursor > head:
        cursor = max(0, head - 10)
    topic = event_topic(rpc_url)
    chunk = max(100, min(2_000, int(os.getenv("PONS_LOG_CHUNK_BLOCKS", "1000"))))
    found = []
    while cursor < head:
        to_block = min(head, cursor + chunk)
        for factory in pons_factories():
            logs = rpc_call("eth_getLogs", [{"fromBlock": hex(cursor + 1), "toBlock": hex(to_block), "address": factory, "topics": [topic]}], rpc_url)
            if not isinstance(logs, list):
                raise ValueError("eth_getLogs returned non-list")
            for log in logs:
                if not isinstance(log, dict):
                    continue
                topics = log.get("topics") or []
                if len(topics) < 4 or str(topics[0]).lower() != topic:
                    continue
                token = address_from_topic(str(topics[1]))
                block = int(str(log.get("blockNumber") or "0x0"), 16)
                tx_hash = str(log.get("transactionHash") or "")
                launch_ts = block_timestamp(block, rpc_url)
                symbol = erc20_text(token, "0x95d89b41", rpc_url)
                name = erc20_text(token, "0x06fdde03", rpc_url)
                if upsert_launch(db, token, block, tx_hash, launch_ts, symbol, name):
                    found.append({"token": token, "symbol": symbol, "name": name, "block": block, "tx_hash": tx_hash})
        set_state(db, "pons_v2_cursor", str(to_block))
        cursor = to_block
    return found


def enrich_due(db: sqlite3.Connection, limit: int = 12) -> list[dict[str, Any]]:
    rows = db.execute("""SELECT address FROM robinhood_launch_watch
      WHERE COALESCE(last_enriched,0) <= ? ORDER BY COALESCE(last_enriched,0),first_seen LIMIT ?""",
      (time.time() - 30, max(1, min(limit, 30)))).fetchall()
    out = []
    for row in rows:
        try:
            out.append(enrich_one(db, str(row["address"])))
        except Exception as exc:
            emit("ROBINHOOD_ENRICH_RETRY", address=str(row["address"]), error_type=type(exc).__name__)
    return out


def run_cycle(db: sqlite3.Connection, rpc_url: str | None = None) -> dict[str, Any]:
    launches = scan_factory_logs(db, rpc_url)
    enriched = enrich_due(db, int(os.getenv("ROBINHOOD_ENRICH_PER_CYCLE", "12")))
    for launch in launches:
        emit("ROBINHOOD_PONS_LAUNCH", **launch, mode="WATCH_ONLY")
    for item in enriched:
        if item.get("changed") and item.get("alert_level") in {"WARM", "HOT"}:
            emit("ROBINHOOD_WATCH_ALERT", address=item["address"], symbol=item.get("symbol"),
                 alert_level=item.get("alert_level"), momentum_score=item.get("momentum_score"),
                 reason=item.get("watch_reason"), mode="WATCH_ONLY")
    return {"launches": len(launches), "enriched": len(enriched), "hot": sum(1 for x in enriched if x.get("alert_level") == "HOT")}


def main() -> None:
    if os.getenv("MODE", "PAPER_ONLY") != "PAPER_ONLY":
        raise SystemExit("robinhood_watch supports PAPER_ONLY only")
    data = Path(os.getenv("DATA_DIR", "/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"))
    db = db_connect(data / "discovery.sqlite3")
    seeded = seed_missed_cases(db)
    emit("ROBINHOOD_WATCH_STARTED", mode="WATCH_ONLY", seeded_cases=seeded, factories=list(pons_factories()))
    interval = max(10, min(300, int(os.getenv("ROBINHOOD_WATCH_INTERVAL", "15"))))
    try:
        while True:
            started = time.monotonic()
            try:
                result = run_cycle(db)
                emit("ROBINHOOD_WATCH_CYCLE_OK", **result)
            except Exception as exc:
                emit("ROBINHOOD_WATCH_RETRY", error_type=type(exc).__name__, error=str(exc)[:160])
            time.sleep(max(1, interval - (time.monotonic() - started)))
    finally:
        db.close()


if __name__ == "__main__":
    main()
