"""Read-only cross-market prediction-market research for crypto-paper-lab.

V14 research/shadow only.  No wallet, signer, API key, or order endpoint is used.
Inputs:
- local Binance.US BTC aggregate-trade / quote tables produced by major_microstructure;
- Hyperliquid public /info market data for perp mid, funding, premium, OI, L2;
- Polymarket public Gamma discovery and CLOB order books.

The first release deliberately separates two ideas:
1) deterministic complement dislocation (best executable YES ask + NO ask < $1 after buffer);
2) model-vs-market BTC probability dislocation, emitted only when the market question can be
   mapped to a verified numeric strike and expiry.  Ambiguous questions are stored but cannot
   create a fair-value signal.
"""
from __future__ import annotations

import json, math, os, re, sqlite3, statistics, time, urllib.parse, urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

VERSION = "prediction-market-shadow-0.1.1"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
HYPER = "https://api.hyperliquid.xyz"
SOURCE_POLY = "POLYMARKET_PUBLIC_READ_ONLY"
SOURCE_HYPER = "HYPERLIQUID_PUBLIC_INFO_READ_ONLY"
SOURCE_BTC = "LOCAL_BINANCE_US_MAJOR_TICKS"
SOURCE_BTC_REST = "BINANCE_US_PUBLIC_REST_FALLBACK"
BINANCE_US = "https://api.binance.us/api/v3"
SETTLEMENT_CHAINLINK_TWAP = "CHAINLINK_BTC_USD_TWAP_60S"
MIN_COMPLEMENT_EDGE_CENTS = max(0.25, float(os.getenv("PREDICTION_MIN_COMPLEMENT_EDGE_CENTS", "1.0")))
MIN_MODEL_EDGE_CENTS = max(0.5, float(os.getenv("PREDICTION_MIN_MODEL_EDGE_CENTS", "2.0")))
MAX_MARKETS = max(1, min(20, int(os.getenv("PREDICTION_MAX_MARKETS", "8"))))
POLL_SECONDS = max(1.0, float(os.getenv("PREDICTION_POLL_SECONDS", "5")))
MARKET_REFRESH_SECONDS = max(20.0, float(os.getenv("PREDICTION_MARKET_REFRESH_SECONDS", "30")))
BTC_REST_REFRESH_SECONDS = max(5.0, float(os.getenv("PREDICTION_BTC_REST_REFRESH_SECONDS", "10")))
BTC_VOL_REFRESH_SECONDS = max(30.0, float(os.getenv("PREDICTION_BTC_VOL_REFRESH_SECONDS", "60")))
_BTC_REST_CACHE = {"price_at": 0.0, "price": None, "pair": None, "vol_at": 0.0, "vol": None}


def emit(event, **fields):
    print(json.dumps({"event": event, "version": VERSION, **fields}, separators=(",", ":"), allow_nan=False), flush=True)


def _f(x, default=None):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def _json_field(v, default):
    if isinstance(v, (list, dict)):
        return v
    if v is None:
        return default
    try:
        return json.loads(v)
    except Exception:
        return default


def _request_json(url, method="GET", body=None, timeout=8):
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {"User-Agent": "crypto-paper-lab-cross-market/0.1", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("response too large")
    return json.loads(raw)


def gamma_get(path, params=None):
    q = "" if not params else "?" + urllib.parse.urlencode(params, doseq=True)
    return _request_json(GAMMA + path + q)


def clob_get(path, params=None):
    q = "" if not params else "?" + urllib.parse.urlencode(params, doseq=True)
    return _request_json(CLOB + path + q)


def hyper_info(body):
    return _request_json(HYPER + "/info", method="POST", body=body)


def binance_us_get(path, params=None):
    q = "" if not params else "?" + urllib.parse.urlencode(params, doseq=True)
    return _request_json(BINANCE_US + path + q)


def _ensure_column(db, table, name, ddl):
    cols={str(r[1]) for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def dbopen(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS prediction_markets(
      market_id TEXT PRIMARY KEY,condition_id TEXT,question TEXT NOT NULL,slug TEXT,
      end_ts REAL,yes_token TEXT NOT NULL,no_token TEXT NOT NULL,liquidity REAL,volume REAL,
      strike REAL,question_kind TEXT NOT NULL,last_seen REAL NOT NULL,source TEXT NOT NULL,
      yes_label TEXT,no_label TEXT,resolution_source TEXT,settlement_kind TEXT,discovery_source TEXT);
    CREATE TABLE IF NOT EXISTS prediction_snapshots(
      id INTEGER PRIMARY KEY,ts REAL NOT NULL,market_id TEXT NOT NULL,question TEXT NOT NULL,
      yes_bid REAL,yes_ask REAL,no_bid REAL,no_ask REAL,yes_depth_usd REAL,no_depth_usd REAL,
      btc_spot REAL,hl_mid REAL,hl_funding REAL,hl_premium REAL,hl_open_interest REAL,
      hl_book_imbalance REAL,seconds_to_expiry REAL,model_yes REAL,model_edge_cents REAL,
      complement_cost REAL,complement_edge_cents REAL,raw_json TEXT NOT NULL,
      btc_source TEXT,btc_age_s REAL);
    CREATE INDEX IF NOT EXISTS idx_prediction_snapshots_market_ts ON prediction_snapshots(market_id,ts);
    CREATE TABLE IF NOT EXISTS prediction_signals(
      id INTEGER PRIMARY KEY,ts REAL NOT NULL,market_id TEXT NOT NULL,signal_type TEXT NOT NULL,
      direction TEXT NOT NULL,edge_cents REAL NOT NULL,confidence REAL NOT NULL,executable INTEGER NOT NULL,
      reason TEXT NOT NULL,snapshot_id INTEGER NOT NULL,source TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_prediction_signals_ts ON prediction_signals(ts);
    """)
    # Existing V14 databases need additive migration; never drop or rewrite prior shadow data.
    for table,name,ddl in (
        ("prediction_markets","yes_label","TEXT"),("prediction_markets","no_label","TEXT"),
        ("prediction_markets","resolution_source","TEXT"),("prediction_markets","settlement_kind","TEXT"),
        ("prediction_markets","discovery_source","TEXT"),("prediction_snapshots","btc_source","TEXT"),
        ("prediction_snapshots","btc_age_s","REAL")):
        _ensure_column(db,table,name,ddl)
    db.commit()
    return db


_BTC = re.compile(r"\b(bitcoin|btc)\b", re.I)
_FAST = re.compile(r"\b(5\s*(?:m|min(?:ute)?s?)|15\s*(?:m|min(?:ute)?s?)|five\s*minutes?|fifteen\s*minutes?)\b", re.I)
_UPDOWN = re.compile(r"\b(up|down|higher|lower|above|below)\b", re.I)
_MONEY = re.compile(r"(?:\$\s*)?([0-9]{2,3}(?:,[0-9]{3})+(?:\.\d+)?|[0-9]{4,6}(?:\.\d+)?)")


def _market_text(m):
    return " ".join(str(m.get(k) or "") for k in (
        "question","slug","description","resolutionSource","_event_title","_event_slug","_series_slug"
    ))


def market_relevant(m):
    text = _market_text(m)
    if not _BTC.search(text):
        return False
    # Current Polymarket crypto series commonly encodes the horizon in the event slug (e.g. btc-updown-15m)
    # while the child market question only contains a clock-time range.
    return bool(_FAST.search(text) or (_UPDOWN.search(text) and (_MONEY.search(text) or "updown" in text.lower())))


def parse_strike(question):
    """Return a numeric BTC strike only when the question clearly contains above/below + price.

    Generic 'BTC up or down' contracts intentionally return None because their settlement reference
    is a separate opening/benchmark value; guessing it would create false fair-value signals.
    """
    q = str(question or "")
    if not re.search(r"\b(above|below|higher than|lower than|over|under)\b", q, re.I):
        return None
    vals = []
    for x in _MONEY.findall(q):
        try:
            v = float(x.replace(",", ""))
            if 5_000 <= v <= 1_000_000:
                vals.append(v)
        except Exception:
            pass
    return vals[-1] if vals else None


def parse_end_ts(v):
    if not v:
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000 if float(v) > 10_000_000_000 else float(v)
    s = str(v).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc).timestamp()
    except Exception:
        return None


def _binary_indexes(outcomes):
    labels=[str(x).strip() for x in outcomes]
    lower=[x.lower() for x in labels]
    if "yes" in lower and "no" in lower:
        return lower.index("yes"),lower.index("no"),labels[lower.index("yes")],labels[lower.index("no")]
    if "up" in lower and "down" in lower:
        return lower.index("up"),lower.index("down"),labels[lower.index("up")],labels[lower.index("down")]
    return None


def _settlement_kind(m, yes_label, no_label):
    text=" ".join(str(m.get(k) or "") for k in ("description","resolutionSource","_event_description","_event_resolution_source")).lower()
    if {yes_label.lower(),no_label.lower()}=={"up","down"}:
        if "chain.link" in text and "twap" in text:
            return SETTLEMENT_CHAINLINK_TWAP
        return "UNVERIFIED_UPDOWN_REFERENCE"
    return "FIXED_STRIKE_OR_STANDARD_BINARY"


def normalize_market(m, now=None, discovery_source="GAMMA_MARKETS"):
    now = time.time() if now is None else float(now)
    outcomes = _json_field(m.get("outcomes"), [])
    tokens = _json_field(m.get("clobTokenIds") or m.get("clob_token_ids"), [])
    if len(outcomes) != 2 or len(tokens) != 2:
        return None
    idx=_binary_indexes(outcomes)
    if idx is None:
        return None
    yi,ni,yes_label,no_label=idx
    q = str(m.get("question") or "").strip()
    if not q or not market_relevant(m):
        return None
    end_ts = parse_end_ts(m.get("endDate") or m.get("endDateIso") or m.get("end_date_iso") or m.get("end_date"))
    if end_ts is not None and end_ts < now - 60:
        return None
    strike = parse_strike(q)
    resolution_source=str(m.get("resolutionSource") or m.get("_event_resolution_source") or "")
    settlement=_settlement_kind(m,yes_label,no_label)
    return {
        "market_id": str(m.get("id") or m.get("conditionId") or m.get("condition_id") or ""),
        "condition_id": str(m.get("conditionId") or m.get("condition_id") or ""),
        "question": q,
        "slug": str(m.get("slug") or m.get("_event_slug") or ""),
        "end_ts": end_ts,
        "yes_token": str(tokens[yi]),
        "no_token": str(tokens[ni]),
        "yes_label": yes_label,
        "no_label": no_label,
        "liquidity": _f(m.get("liquidityNum") or m.get("liquidity"), 0.0) or 0.0,
        "volume": _f(m.get("volumeNum") or m.get("volume"), 0.0) or 0.0,
        "strike": strike,
        "question_kind": "FIXED_STRIKE" if strike is not None else "UNRESOLVED_REFERENCE_PRICE",
        "resolution_source": resolution_source,
        "settlement_kind": settlement,
        "discovery_source": discovery_source,
        "last_seen": now,
        "source": SOURCE_POLY,
    }


def _flatten_search(body):
    if not isinstance(body,dict):
        raise ValueError("gamma search malformed")
    out=[]
    for e in body.get("events") or []:
        if not isinstance(e,dict):
            continue
        for m in e.get("markets") or []:
            if not isinstance(m,dict):
                continue
            mm=dict(m)
            mm["_event_title"]=e.get("title") or ""
            mm["_event_slug"]=e.get("slug") or ""
            mm["_series_slug"]=e.get("seriesSlug") or ""
            mm["_event_description"]=e.get("description") or ""
            mm["_event_resolution_source"]=e.get("resolutionSource") or ""
            if not mm.get("description"):
                mm["description"]=e.get("description")
            if not mm.get("resolutionSource"):
                mm["resolutionSource"]=e.get("resolutionSource")
            out.append(mm)
    return out


def discover_markets(fetch=gamma_get, now=None):
    now = time.time() if now is None else float(now)
    candidates=[];search_errors=[]
    # Public search is more reliable for rotating 5m/15m crypto series than taking the first N generic markets.
    for q in ("bitcoin","btc"):
        try:
            body=fetch("/public-search", {"q":q,"limit_per_type":50,"keep_closed_markets":0,"search_profiles":"false","search_tags":"false"})
            candidates.extend((m,"GAMMA_PUBLIC_SEARCH:"+q) for m in _flatten_search(body))
        except Exception as exc:
            search_errors.append(type(exc).__name__)
    # Fallback/coverage path keeps fixed-strike markets that may not rank highly in search.
    try:
        body=fetch("/markets", {"limit": 500, "closed": "false", "order": "endDate", "ascending": "true"})
        if isinstance(body,list):
            candidates.extend((m,"GAMMA_MARKETS") for m in body if isinstance(m,dict))
        elif not candidates:
            raise ValueError("gamma markets malformed")
    except Exception:
        if not candidates:
            raise
    dedup={}
    for m,source in candidates:
        nm=normalize_market(m,now,source)
        if nm and nm["market_id"]:
            old=dedup.get(nm["market_id"])
            if old is None or (nm["end_ts"] or 10**20)<(old["end_ts"] or 10**20):
                dedup[nm["market_id"]]=nm
    out=list(dedup.values())
    out.sort(key=lambda x: (x["end_ts"] is None, x["end_ts"] or 10**20, -x["liquidity"]))
    return out[:MAX_MARKETS]


def persist_markets(db, markets):
    with db:
        for m in markets:
            db.execute("""INSERT INTO prediction_markets(market_id,condition_id,question,slug,end_ts,yes_token,no_token,liquidity,volume,strike,question_kind,last_seen,source,yes_label,no_label,resolution_source,settlement_kind,discovery_source)
              VALUES(:market_id,:condition_id,:question,:slug,:end_ts,:yes_token,:no_token,:liquidity,:volume,:strike,:question_kind,:last_seen,:source,:yes_label,:no_label,:resolution_source,:settlement_kind,:discovery_source)
              ON CONFLICT(market_id) DO UPDATE SET condition_id=excluded.condition_id,question=excluded.question,slug=excluded.slug,
              end_ts=excluded.end_ts,yes_token=excluded.yes_token,no_token=excluded.no_token,liquidity=excluded.liquidity,
              volume=excluded.volume,strike=excluded.strike,question_kind=excluded.question_kind,last_seen=excluded.last_seen,source=excluded.source,
              yes_label=excluded.yes_label,no_label=excluded.no_label,resolution_source=excluded.resolution_source,
              settlement_kind=excluded.settlement_kind,discovery_source=excluded.discovery_source""", m)


def _best(book, side):
    levels = book.get(side) if isinstance(book, dict) else None
    if not isinstance(levels, list) or not levels:
        return None, 0.0
    parsed = []
    for x in levels:
        if not isinstance(x, dict):
            continue
        p = _f(x.get("price")); s = _f(x.get("size"), 0.0)
        if p is not None and s is not None and 0 < p < 1 and s >= 0:
            parsed.append((p, s))
    if not parsed:
        return None, 0.0
    parsed.sort(key=lambda z:z[0],reverse=(side=="bids"))
    px,_=parsed[0]
    depth=sum(p*s for p,s in parsed[:20])
    return px,depth


def poly_book(token, fetch=clob_get):
    b = fetch("/book", {"token_id": token})
    if not isinstance(b, dict):
        raise ValueError("clob book malformed")
    bid, bid_depth = _best(b, "bids"); ask, ask_depth = _best(b, "asks")
    return {"bid": bid, "ask": ask, "bid_depth": bid_depth, "ask_depth": ask_depth}


def hyper_btc(fetch=hyper_info):
    data = fetch({"type": "metaAndAssetCtxs"})
    if not (isinstance(data, list) and len(data) == 2 and isinstance(data[0], dict) and isinstance(data[1], list)):
        raise ValueError("hyper meta malformed")
    universe = data[0].get("universe") or []
    idx = next((i for i, x in enumerate(universe) if isinstance(x, dict) and str(x.get("name")) == "BTC"), None)
    if idx is None or idx >= len(data[1]) or not isinstance(data[1][idx], dict):
        raise ValueError("hyper BTC missing")
    c = data[1][idx]
    book = fetch({"type": "l2Book", "coin": "BTC"})
    levels = book.get("levels") if isinstance(book, dict) else None
    imbalance = None
    if isinstance(levels, list) and len(levels) == 2:
        def side_notional(arr):
            total = 0.0
            for x in arr[:20] if isinstance(arr, list) else []:
                if not isinstance(x, dict): continue
                p = _f(x.get("px")); s = _f(x.get("sz"))
                if p and s: total += p*s
            return total
        bidn, askn = side_notional(levels[0]), side_notional(levels[1])
        if bidn + askn > 0:
            imbalance = (bidn - askn) / (bidn + askn)
    return {
        "mid": _f(c.get("midPx") or c.get("markPx")),
        "funding": _f(c.get("funding")),
        "premium": _f(c.get("premium")),
        "open_interest": _f(c.get("openInterest")),
        "book_imbalance": imbalance,
        "source": SOURCE_HYPER,
    }


def local_btc_state(db, now=None):
    now=time.time() if now is None else float(now)
    try:
        r=db.execute("SELECT ts_ms,price FROM major_ticks WHERE symbol='BTC' ORDER BY ts_ms DESC,trade_id DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None,None
    if not r:
        return None,None
    age=max(0.0,now-int(r["ts_ms"])/1000)
    return float(r["price"]),age


def local_btc(db, now=None, max_age=15):
    price,age=local_btc_state(db,now)
    return price if price is not None and age is not None and age<=max_age else None


def realized_vol_1m(db, now=None, minutes=60):
    """Annualization is intentionally avoided; return per-minute log-return stdev."""
    now = time.time() if now is None else float(now)
    try:
        rows = db.execute("SELECT ts_ms,price FROM major_ticks WHERE symbol='BTC' AND ts_ms>=? ORDER BY ts_ms,trade_id", (int((now-minutes*60)*1000),)).fetchall()
    except sqlite3.OperationalError:
        return None
    if len(rows) < 30:
        return None
    buckets = {}
    for r in rows:
        buckets[int(r["ts_ms"])//60000] = float(r["price"])
    vals = [buckets[k] for k in sorted(buckets)]
    if len(vals) < 10:
        return None
    rets = [math.log(vals[i]/vals[i-1]) for i in range(1,len(vals)) if vals[i-1] > 0 and vals[i] > 0]
    return statistics.pstdev(rets) if len(rets) >= 5 else None


def _binance_us_pair(fetch=binance_us_get):
    errs=[]
    for pair in ("BTCUSDT","BTCUSD"):
        try:
            body=fetch("/ticker/price",{"symbol":pair})
            price=_f(body.get("price") if isinstance(body,dict) else None)
            if price and price>0:return pair,price
        except Exception as exc:errs.append(type(exc).__name__)
    raise RuntimeError("binance.us BTC ticker unavailable:"+",".join(errs))


def _rest_vol_1m(pair, fetch=binance_us_get):
    body=fetch("/klines",{"symbol":pair,"interval":"1m","limit":61})
    if not isinstance(body,list):raise ValueError("binance.us klines malformed")
    closes=[]
    for r in body:
        if isinstance(r,list) and len(r)>=5:
            c=_f(r[4])
            if c and c>0:closes.append(c)
    if len(closes)<10:return None
    rets=[math.log(closes[i]/closes[i-1]) for i in range(1,len(closes))]
    return statistics.pstdev(rets) if len(rets)>=5 else None


def btc_market_state(db, now=None, rest_fetch=binance_us_get, max_local_age=15, cache=None):
    """Prefer fresh local Binance.US aggTrades; fall back only to Binance.US public REST.

    Never substitutes Hyperliquid or global-Binance history for spot, so venue provenance stays explicit.
    """
    now=time.time() if now is None else float(now);cache=_BTC_REST_CACHE if cache is None else cache
    local,age=local_btc_state(db,now);local_vol=realized_vol_1m(db,now)
    if local is not None and age is not None and age<=max_local_age:
        # If local trade history is still warming up, same-venue REST may fill only the volatility input.
        vol=local_vol
        if vol is None:
            try:
                pair,_=_binance_us_pair(rest_fetch);vol=_rest_vol_1m(pair,rest_fetch)
            except Exception:pass
        return {"price":local,"sigma_1m":vol,"source":SOURCE_BTC,"age_s":age,"pair":None}
    # Cache REST price briefly and kline volatility longer to avoid unnecessary public requests.
    if now-float(cache.get("price_at") or 0)>=BTC_REST_REFRESH_SECONDS or not cache.get("price"):
        pair,price=_binance_us_pair(rest_fetch);cache.update(price_at=now,price=price,pair=pair)
    pair=cache.get("pair") or "BTCUSDT";price=_f(cache.get("price"))
    if now-float(cache.get("vol_at") or 0)>=BTC_VOL_REFRESH_SECONDS or cache.get("vol") is None:
        try:cache.update(vol_at=now,vol=_rest_vol_1m(pair,rest_fetch))
        except Exception:cache.update(vol_at=now,vol=None)
    return {"price":price,"sigma_1m":_f(cache.get("vol")),"source":SOURCE_BTC_REST,"age_s":age,"pair":pair}


def normal_cdf(x):
    return 0.5*(1.0+math.erf(x/math.sqrt(2.0)))


def fair_yes_probability(spot, strike, seconds_to_expiry, sigma_1m, hyper=None):
    """Simple auditable short-horizon distribution model; research only, not a profit claim."""
    if not all(v is not None for v in (spot, strike, seconds_to_expiry, sigma_1m)):
        return None
    spot=float(spot);strike=float(strike);sec=float(seconds_to_expiry);sig=float(sigma_1m)
    if spot<=0 or strike<=0 or sec<=0 or sig<=0:
        return None
    sigma_h = sig*math.sqrt(max(sec,1.0)/60.0)
    if sigma_h <= 1e-9:
        return 1.0 if spot>strike else 0.0
    z = math.log(spot/strike)/sigma_h
    p = normal_cdf(z)
    if isinstance(hyper, dict):
        # tiny bounded microstructure adjustment only; never let one venue dominate probability.
        prem = _f(hyper.get("premium"),0.0) or 0.0
        imb = _f(hyper.get("book_imbalance"),0.0) or 0.0
        p += max(-0.04,min(0.04,prem*50.0 + imb*0.02))
    return max(0.01,min(0.99,p))


@dataclass(frozen=True)
class Signal:
    signal_type: str
    direction: str
    edge_cents: float
    confidence: float
    executable: bool
    reason: str


def evaluate(yes, no, model_yes=None, min_comp_cents=MIN_COMPLEMENT_EDGE_CENTS, min_model_cents=MIN_MODEL_EDGE_CENTS):
    signals=[]
    ya, na = yes.get("ask"), no.get("ask")
    if ya is not None and na is not None:
        cost = ya+na
        edge = (1.0-cost)*100
        if edge >= min_comp_cents:
            depth_ok = yes.get("ask_depth",0)>0 and no.get("ask_depth",0)>0
            signals.append(Signal("YES_NO_COMPLEMENT","BOTH",round(edge,4),min(.99,.65+edge/100),bool(depth_ok),"YES_ASK_PLUS_NO_ASK_BELOW_ONE"))
    if model_yes is not None:
        # compare against executable asks, not displayed midpoint.
        if ya is not None:
            edge=(model_yes-ya)*100
            if edge>=min_model_cents:
                signals.append(Signal("MODEL_DISLOCATION","YES",round(edge,4),min(.95,.55+edge/100),yes.get("ask_depth",0)>0,"MODEL_YES_ABOVE_EXECUTABLE_ASK"))
        if na is not None:
            model_no=1-model_yes;edge=(model_no-na)*100
            if edge>=min_model_cents:
                signals.append(Signal("MODEL_DISLOCATION","NO",round(edge,4),min(.95,.55+edge/100),no.get("ask_depth",0)>0,"MODEL_NO_ABOVE_EXECUTABLE_ASK"))
    return signals


def _market_rows(db, now):
    return db.execute("SELECT * FROM prediction_markets WHERE last_seen>=? ORDER BY COALESCE(end_ts,1e20),liquidity DESC LIMIT ?", (now-2*MARKET_REFRESH_SECONDS, MAX_MARKETS)).fetchall()


def cycle(db, now=None, market_fetch=gamma_get, book_fetch=clob_get, hyper_fetch=hyper_info, btc_rest_fetch=binance_us_get, force_refresh=False, btc_cache=None):
    now=time.time() if now is None else float(now)
    last=db.execute("SELECT MAX(last_seen) FROM prediction_markets").fetchone()[0]
    discovered=0
    if force_refresh or last is None or now-float(last)>=MARKET_REFRESH_SECONDS:
        markets=discover_markets(market_fetch,now);persist_markets(db,markets);discovered=len(markets)
    btc_state=None;btc_error=None
    try:btc_state=btc_market_state(db,now,btc_rest_fetch,cache=btc_cache)
    except Exception as exc:btc_error=type(exc).__name__
    btc=None if not btc_state else btc_state.get("price");sig1m=None if not btc_state else btc_state.get("sigma_1m")
    hyper=None;hyper_error=None
    try: hyper=hyper_btc(hyper_fetch)
    except Exception as exc: hyper_error=type(exc).__name__
    rows=_market_rows(db,now);observed=0;signal_count=0;errors=[];unresolved=0
    discovery_sources={}
    for m in rows:
        try:
            yes=poly_book(m["yes_token"],book_fetch);no=poly_book(m["no_token"],book_fetch)
            sec=None if m["end_ts"] is None else max(0.0,float(m["end_ts"])-now)
            model=None
            # Up/Down markets resolved from a benchmark opening price remain observation/complement-only
            # until that reference is explicitly ingested; never infer it from Binance/Hyperliquid spot.
            if m["strike"] is not None and sec and btc and sig1m:
                model=fair_yes_probability(btc,float(m["strike"]),sec,sig1m,hyper)
                if re.search(r"\b(below|lower than|under)\b",m["question"],re.I): model=1-model if model is not None else None
            else:
                unresolved+=int(str(m["question_kind"])!="FIXED_STRIKE")
            comp_cost=(yes["ask"]+no["ask"]) if yes["ask"] is not None and no["ask"] is not None else None
            comp_edge=(1-comp_cost)*100 if comp_cost is not None else None
            model_edge=None
            if model is not None and yes["ask"] is not None:model_edge=(model-yes["ask"])*100
            bsource=None if not btc_state else btc_state.get("source");bage=None if not btc_state else btc_state.get("age_s")
            raw={"yes":yes,"no":no,"yes_label":m["yes_label"],"no_label":m["no_label"],"hyper":hyper,
                 "btc_source":bsource,"btc_age_s":bage,"question_kind":m["question_kind"],"settlement_kind":m["settlement_kind"],
                 "resolution_source":m["resolution_source"],"discovery_source":m["discovery_source"]}
            with db:
                cur=db.execute("""INSERT INTO prediction_snapshots(ts,market_id,question,yes_bid,yes_ask,no_bid,no_ask,yes_depth_usd,no_depth_usd,
                  btc_spot,hl_mid,hl_funding,hl_premium,hl_open_interest,hl_book_imbalance,seconds_to_expiry,model_yes,model_edge_cents,
                  complement_cost,complement_edge_cents,raw_json,btc_source,btc_age_s) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (now,m["market_id"],m["question"],yes["bid"],yes["ask"],no["bid"],no["ask"],yes["ask_depth"],no["ask_depth"],btc,
                   None if not hyper else hyper["mid"],None if not hyper else hyper["funding"],None if not hyper else hyper["premium"],
                   None if not hyper else hyper["open_interest"],None if not hyper else hyper["book_imbalance"],sec,model,model_edge,comp_cost,comp_edge,
                   json.dumps(raw,separators=(",",":"),allow_nan=False),bsource,bage))
                sid=cur.lastrowid
                for sig in evaluate(yes,no,model):
                    source=SOURCE_POLY if sig.signal_type=="YES_NO_COMPLEMENT" else SOURCE_POLY+"+"+SOURCE_HYPER+"+"+str(bsource or "BTC_UNAVAILABLE")
                    db.execute("INSERT INTO prediction_signals(ts,market_id,signal_type,direction,edge_cents,confidence,executable,reason,snapshot_id,source) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (now,m["market_id"],sig.signal_type,sig.direction,sig.edge_cents,sig.confidence,int(sig.executable),sig.reason,sid,source))
                    signal_count+=1
            observed+=1;ds=str(m["discovery_source"] or "UNKNOWN");discovery_sources[ds]=discovery_sources.get(ds,0)+1
        except Exception as exc:
            errors.append({"market":m["market_id"],"type":type(exc).__name__})
    emit("PREDICTION_MARKET_SHADOW_OK",discovered=discovered,observed=observed,signals=signal_count,unresolved_reference=unresolved,
         btc_available=btc is not None,btc_source=None if not btc_state else btc_state.get("source"),btc_age_s=None if not btc_state else btc_state.get("age_s"),
         btc_error=btc_error,hyper_available=hyper is not None,hyper_error=hyper_error,discovery_sources=discovery_sources,
         errors=errors[:5],read_only=True,no_real_trading=True)
    return {"discovered":discovered,"observed":observed,"signals":signal_count,"btc_available":btc is not None,
            "btc_source":None if not btc_state else btc_state.get("source"),"hyper_available":hyper is not None,"errors":errors,"unresolved_reference":unresolved}


def main():
    data=Path(os.getenv("DATA_DIR","/data" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "./discovery-data"));data.mkdir(parents=True,exist_ok=True)
    db=dbopen(data/"discovery.sqlite3")
    try:
        while True:
            started=time.monotonic()
            try: cycle(db)
            except Exception as exc: emit("PREDICTION_MARKET_SHADOW_ERROR",error_type=type(exc).__name__,error=str(exc)[:180],read_only=True,no_real_trading=True)
            time.sleep(max(.5,POLL_SECONDS-(time.monotonic()-started)))
    finally: db.close()

if __name__=="__main__": main()
