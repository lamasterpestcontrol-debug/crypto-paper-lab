"""Strategy v0.3 modules for paper-scanner.

Paper-only. No wallet/order APIs.
Adds:
- volume-before-price
- exit-first sizing
- wait-for-pullback state
- social x on-chain hooks
- data freshness
- audit trail
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
import json, math, time

VERSION = "strategy-v0.3.0"

@dataclass
class MarketSnapshot:
    ts: float
    price: float
    liquidity: float
    volume_h1: float
    volume_h24: float
    buys_h1: int
    sells_h1: int
    price_change_h1_pct: float = 0.0
    social_score: Optional[float] = None
    social_ts: Optional[float] = None
    holder_score: Optional[float] = None
    holder_ts: Optional[float] = None

@dataclass
class Decision:
    state: str
    reason: str
    score: float = 0.0
    max_safe_usd: float = 0.0
    audit: Optional[Dict[str, Any]] = None

def _finite(x, default=0.0):
    try:
        x=float(x)
        return x if math.isfinite(x) else default
    except Exception:
        return default

def data_freshness_ok(now: float, *timestamps: Optional[float], max_age_sec: float = 900.0) -> bool:
    vals=[t for t in timestamps if t is not None]
    if not vals:
        return True
    return all((now - float(t)) <= max_age_sec for t in vals)

def volume_before_price(curr: MarketSnapshot, prev: Optional[MarketSnapshot]) -> tuple[bool,float]:
    if not prev:
        return False, 0.0
    prev_vol=max(_finite(prev.volume_h1),1.0)
    vol_ratio=_finite(curr.volume_h1)/prev_vol
    price_abs=abs(_finite(curr.price_change_h1_pct))
    signal = vol_ratio >= 1.8 and price_abs <= 8.0 and curr.buys_h1 >= max(5, int(curr.sells_h1*1.15))
    strength=max(0.0,(vol_ratio-1.0)) * max(0.0,1.0-price_abs/20.0)
    return signal, strength

def estimate_max_safe_usd(liquidity_usd: float, target_price_impact_pct: float = 2.0) -> float:
    liq=max(0.0,_finite(liquidity_usd))
    # Conservative rule of thumb for paper sizing, not an AMM exact quote.
    return liq * min(max(target_price_impact_pct/100.0,0.001),0.05) * 0.5

def exit_first_ok(liquidity_usd: float, intended_usd: float, max_impact_pct: float = 2.0) -> tuple[bool,float]:
    max_safe=estimate_max_safe_usd(liquidity_usd,max_impact_pct)
    return intended_usd <= max_safe, max_safe

def social_onchain_confirm(s: MarketSnapshot) -> tuple[bool,str]:
    if s.social_score is None:
        return True, "SOCIAL_UNKNOWN"
    buy_pressure = s.buys_h1 >= max(5, int(s.sells_h1*1.20))
    if s.social_score >= 0.7 and not buy_pressure:
        return False, "SOCIAL_AHEAD_OF_ONCHAIN"
    if s.social_score >= 0.5 and buy_pressure:
        return True, "SOCIAL_ONCHAIN_CONFIRM"
    return True, "NO_STRONG_SOCIAL_SIGNAL"

def pullback_state(first_seen_price: float, current_price: float, local_high_price: float,
                   invalidation_price: float, reentry_recovery_pct: float = 5.0) -> str:
    if min(first_seen_price,current_price,local_high_price,invalidation_price) <= 0:
        return "WATCH"
    if current_price >= local_high_price * 0.98:
        return "WAIT_PULLBACK"
    if current_price <= invalidation_price:
        return "EXPIRE"
    recovered = (current_price / invalidation_price - 1.0) * 100.0
    if recovered >= reentry_recovery_pct and current_price < local_high_price * 0.90:
        return "RECHECK"
    return "WATCH"

def build_decision(curr: MarketSnapshot, prev: Optional[MarketSnapshot], intended_usd: float,
                   first_seen_price: float, local_high_price: float, invalidation_price: float,
                   now: Optional[float]=None) -> Decision:
    now = now or time.time()
    audit = {"version": VERSION, "ts": now, "curr": asdict(curr)}

    if not data_freshness_ok(now, curr.social_ts, curr.holder_ts):
        return Decision("REJECT","STALE_DATA",audit=audit)

    exit_ok,max_safe = exit_first_ok(curr.liquidity,intended_usd)
    audit["max_safe_usd"]=max_safe
    if not exit_ok:
        return Decision("REJECT","EXIT_TOO_THIN",max_safe_usd=max_safe,audit=audit)

    social_ok,social_reason = social_onchain_confirm(curr)
    audit["social_reason"]=social_reason
    if not social_ok:
        return Decision("WATCH",social_reason,max_safe_usd=max_safe,audit=audit)

    vbp,strength = volume_before_price(curr,prev)
    audit["volume_before_price"]=vbp
    audit["vbp_strength"]=strength

    pstate = pullback_state(first_seen_price,curr.price,local_high_price,invalidation_price)
    audit["pullback_state"]=pstate

    if pstate == "EXPIRE":
        return Decision("REJECT","INVALIDATION_BROKEN",max_safe_usd=max_safe,audit=audit)
    if pstate == "WAIT_PULLBACK":
        return Decision("WATCH","WAIT_PULLBACK",score=strength,max_safe_usd=max_safe,audit=audit)
    if pstate == "RECHECK" and vbp:
        return Decision("ENTER","RECHECK_CONFIRMED_VBP",score=strength,max_safe_usd=max_safe,audit=audit)
    if vbp:
        return Decision("WATCH","VOLUME_BEFORE_PRICE",score=strength,max_safe_usd=max_safe,audit=audit)
    return Decision("WATCH","NO_EDGE_YET",max_safe_usd=max_safe,audit=audit)

def audit_json(decision: Decision) -> str:
    return json.dumps(asdict(decision), allow_nan=False, separators=(",",":"))
