"""Strategy v0.8: stock-market style risk management adapted for crypto paper trading.

Paper-only. No wallet/order APIs. Parameters are candidates for walk-forward calibration,
not claims of profitability.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import math
from typing import Iterable
VERSION = "strategy-v0.8.0"

def _f(x):
    x=float(x)
    if not math.isfinite(x): raise ValueError("non-finite numeric input")
    return x

@dataclass(frozen=True)
class Bar:
    ts: float; open: float; high: float; low: float; close: float; volume: float
    def __post_init__(self):
        for v in (self.ts,self.open,self.high,self.low,self.close,self.volume): _f(v)
        if self.ts<=0 or min(self.open,self.high,self.low,self.close)<=0 or self.volume<0: raise ValueError("invalid bar")
        if self.high < max(self.open,self.close,self.low) or self.low > min(self.open,self.close,self.high): raise ValueError("invalid OHLC bounds")

def ema(values: Iterable[float], period: int) -> float:
    xs=[_f(x) for x in values]
    if period<2 or len(xs)<period: raise ValueError("insufficient EMA history")
    k=2/(period+1); out=sum(xs[:period])/period
    for x in xs[period:]: out=x*k+out*(1-k)
    return out

def rsi(closes: Iterable[float], period: int=14) -> float:
    xs=[_f(x) for x in closes]
    if len(xs)<period+1: raise ValueError("insufficient RSI history")
    ds=[b-a for a,b in zip(xs[-period-1:-1],xs[-period:])]
    gain=sum(max(d,0) for d in ds)/period; loss=sum(max(-d,0) for d in ds)/period
    if loss==0: return 100.0 if gain>0 else 50.0
    rs=gain/loss; return 100-(100/(1+rs))

def atr(bars: Iterable[Bar], period: int=14) -> float:
    xs=list(bars)
    if len(xs)<period+1: raise ValueError("insufficient ATR history")
    trs=[]
    for p,c in zip(xs[-period-1:-1],xs[-period:]):
        trs.append(max(c.high-c.low,abs(c.high-p.close),abs(c.low-p.close)))
    return sum(trs)/period

@dataclass(frozen=True)
class TrendSnapshot:
    close: float; ema20: float; ema50: float; rsi14: float; atr14: float; atr_pct: float
    volume_ratio: float; breakout20: bool; bullish_structure: bool; overextended: bool
    entry_ready: bool; reason: str

def analyze_bars(bars: Iterable[Bar], fast_period: int=20, slow_period: int=50, breakout_period: int=20) -> TrendSnapshot:
    xs=list(bars)
    need=max(slow_period,15,breakout_period+1)
    if len(xs)<need: raise ValueError(f"need at least {need} bars")
    closes=[b.close for b in xs]; e20=ema(closes,fast_period); e50=ema(closes,slow_period); rs=rsi(closes,14); av=atr(xs,14)
    close=xs[-1].close; atr_pct=av/close
    window=xs[-(breakout_period+1):-1]
    avg_vol=sum(b.volume for b in window)/len(window)
    vr=xs[-1].volume/avg_vol if avg_vol>0 else 0
    prior_high=max(b.high for b in window); breakout=close>prior_high
    structure=close>e20>e50; overextended=(close-e20)>2.5*av
    momentum_ok=50<=rs<=78; volume_ok=vr>=1.2
    ready=structure and momentum_ok and volume_ok and not overextended
    if not structure: reason="TREND_NOT_CONFIRMED"
    elif not momentum_ok: reason="RSI_OUTSIDE_ENTRY_ZONE"
    elif not volume_ok: reason="VOLUME_NOT_CONFIRMED"
    elif overextended: reason="TOO_EXTENDED_ABOVE_EMA20"
    elif breakout: reason="BREAKOUT_CONFIRMED"
    else: reason="TREND_PULLBACK_ZONE"
    return TrendSnapshot(close,e20,e50,rs,av,atr_pct,vr,breakout,structure,overextended,ready,reason)

@dataclass(frozen=True)
class ProfitTier:
    gain: float; sell_fraction_initial: float

@dataclass(frozen=True)
class Params:
    tiers: tuple[ProfitTier,...]
    runner_fraction_initial: float
    trail_activate_gain: float
    trail_atr_mult: float
    hard_stop_atr_mult: float=2.5
    hard_stop_floor: float=0.12
    hard_stop_cap: float=0.30
    first_entry_fraction: float=0.50
    add_fraction: float=0.25
    max_deployed_fraction: float=1.00
    add_min_gain: float=0.12
    pullback_min: float=0.04
    pullback_max: float=0.15
    add_rsi_low: float=50.0
    add_rsi_high: float=75.0
    def __post_init__(self):
        sold=sum(t.sell_fraction_initial for t in self.tiers)
        if abs(sold+self.runner_fraction_initial-1)>1e-9: raise ValueError("tiers plus runner must equal 1")
        if tuple(t.gain for t in self.tiers)!=tuple(sorted(t.gain for t in self.tiers)): raise ValueError("tiers ascending")
        if not (0<self.first_entry_fraction<=self.max_deployed_fraction<=1): raise ValueError("invalid deployment")

CONSERVATIVE=Params((ProfitTier(.20,.25),ProfitTier(.40,.25),ProfitTier(.65,.20)),.30,.20,2.2,add_fraction=.20,add_min_gain=.10)
BALANCED=Params((ProfitTier(.25,.20),ProfitTier(.50,.25),ProfitTier(.75,.25)),.30,.25,2.5)
AGGRESSIVE=Params((ProfitTier(.30,.15),ProfitTier(.60,.20),ProfitTier(1.00,.25)),.40,.30,3.0,add_min_gain=.15)

@dataclass
class PositionState:
    planned_usd: float; deployed_usd: float; qty: float; avg_entry: float; peak_price: float; initial_qty: float
    realized_cash: float=0.0; tier_done: set[int]=field(default_factory=set); add_count: int=0
    def __post_init__(self):
        for v in (self.planned_usd,self.deployed_usd,self.qty,self.avg_entry,self.peak_price,self.initial_qty,self.realized_cash): _f(v)
        if min(self.planned_usd,self.deployed_usd,self.qty,self.avg_entry,self.peak_price,self.initial_qty)<=0: raise ValueError("invalid position")
        if self.deployed_usd>self.planned_usd+1e-9: raise ValueError("position exceeds plan")

@dataclass(frozen=True)
class Action:
    kind: str; usd: float=0.0; qty: float=0.0; reason: str=""

def initial_entry_budget(planned_usd, signal: TrendSnapshot, params: Params=BALANCED):
    planned=_f(planned_usd)
    return planned*params.first_entry_fraction if planned>0 and signal.entry_ready else 0.0

def hard_stop_price(state: PositionState, atr_value: float, params: Params=BALANCED):
    pct=min(params.hard_stop_cap,max(params.hard_stop_floor,params.hard_stop_atr_mult*_f(atr_value)/state.avg_entry))
    return state.avg_entry*(1-pct)

def trailing_stop_price(state: PositionState, atr_value: float, params: Params=BALANCED):
    if state.peak_price/state.avg_entry-1 < params.trail_activate_gain: return None
    return max(state.avg_entry,state.peak_price-params.trail_atr_mult*_f(atr_value))

def pyramiding_allowed(state: PositionState, price: float, signal: TrendSnapshot, params: Params=BALANCED):
    price=_f(price)
    if state.deployed_usd>=state.planned_usd or not signal.entry_ready: return False
    if price/state.avg_entry-1<params.add_min_gain: return False
    dd=1-price/state.peak_price
    return params.pullback_min<=dd<=params.pullback_max and params.add_rsi_low<=signal.rsi14<=params.add_rsi_high and price>signal.ema20>signal.ema50

def manage_position(state: PositionState, price: float, signal: TrendSnapshot, liquidity_ok=True, params: Params=BALANCED):
    price=_f(price); actions=[]; state.peak_price=max(state.peak_price,price)
    if not liquidity_ok: return [Action("EXIT_ALL",qty=state.qty,reason="LIQUIDITY_BREAK")]
    if price<=hard_stop_price(state,signal.atr14,params): return [Action("EXIT_ALL",qty=state.qty,reason="VOLATILITY_HARD_STOP")]
    ts=trailing_stop_price(state,signal.atr14,params)
    if ts is not None and price<=ts: return [Action("EXIT_ALL",qty=state.qty,reason="TRAILING_PROFIT_PROTECTION")]
    gain=price/state.avg_entry-1
    for i,t in enumerate(params.tiers):
        if i not in state.tier_done and gain>=t.gain:
            q=min(state.qty,state.initial_qty*t.sell_fraction_initial)
            if q>0:
                actions.append(Action("SELL_PART",qty=q,reason=f"TAKE_{int(t.gain*100)}")); state.qty-=q; state.realized_cash+=q*price; state.tier_done.add(i)
    if pyramiding_allowed(state,price,signal,params):
        remaining=state.planned_usd-state.deployed_usd; usd=min(remaining,state.planned_usd*params.add_fraction)
        if usd>0:
            actions.append(Action("ADD",usd=usd,reason="PROFITABLE_PULLBACK_RECONFIRMED"))
            old_cost=state.avg_entry*state.qty; q=usd/price; state.qty+=q; state.deployed_usd+=usd; state.avg_entry=(old_cost+usd)/state.qty; state.add_count+=1
    return actions

def variant_grid(): return {"conservative":CONSERVATIVE,"balanced":BALANCED,"aggressive":AGGRESSIVE}


def analyze_early_crypto(bars: Iterable[Bar]) -> TrendSnapshot:
    """Short-history profile for new tokens: EMA9/21 with RSI14/ATR14 and 20-bar breakout."""
    return analyze_bars(bars,fast_period=9,slow_period=21,breakout_period=20)

def analyze_mature_crypto(bars: Iterable[Bar]) -> TrendSnapshot:
    """Longer-history profile: EMA20/50, appropriate once real daily/hourly history exists."""
    return analyze_bars(bars,fast_period=20,slow_period=50,breakout_period=20)
