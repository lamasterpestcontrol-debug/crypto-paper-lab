"""Strategy v0.7: fast routing + independent P&L attribution.

Paper/research only. No wallet/order execution.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Iterable
import json, math, statistics, time

VERSION="strategy-v0.7.0"

@dataclass
class FastInput:
    liquidity_usd: float
    volume_h1_usd: float
    buys_h1: int
    sells_h1: int
    age_minutes: float
    basic_risk_block: bool=False

@dataclass
class FastRoute:
    route: str
    reason: str
    deep_analysis: bool
    priority: float

def fast_route(x:FastInput)->FastRoute:
    if x.basic_risk_block:
        return FastRoute("REJECT_FAST","BASIC_RISK_BLOCK",False,0)
    if x.liquidity_usd < 5_000:
        return FastRoute("REJECT_FAST","LIQUIDITY_TOO_LOW",False,0)
    if x.volume_h1_usd < 1_000 and x.buys_h1 < 5:
        return FastRoute("COLD_WATCH","INSUFFICIENT_ACTIVITY",False,5)
    pressure=x.buys_h1/max(1,x.sells_h1)
    priority=min(100.0,
        25*min(1,x.liquidity_usd/50_000)+
        30*min(1,x.volume_h1_usd/25_000)+
        25*min(1,pressure/2)+
        20*(1 if x.age_minutes<=120 else .4))
    if priority>=55:
        return FastRoute("DEEP_ANALYSIS","FAST_SCOUT_PASS",True,priority)
    return FastRoute("WARM_WATCH","FAST_SCOUT_BORDERLINE",False,priority)

@dataclass
class StrategyTrade:
    strategy: str
    entry_ts: float
    exit_ts: float
    gross_pnl_usd: float
    fees_usd: float=0
    slippage_usd: float=0
    gas_usd: float=0
    max_drawdown_pct: float=0
    avoided_loss_usd: float=0
    missed_upside_usd: float=0

def net_usd(t:StrategyTrade)->float:
    return t.gross_pnl_usd-t.fees_usd-t.slippage_usd-t.gas_usd

def attribution(rows:Iterable[StrategyTrade])->dict:
    groups={}
    for r in rows: groups.setdefault(r.strategy,[]).append(r)
    out={}
    for name,rs in groups.items():
        nets=[net_usd(r) for r in rs]
        wins=sum(v for v in nets if v>0); losses=-sum(v for v in nets if v<0)
        out[name]={
            "trades":len(rs),
            "net_pnl_usd":sum(nets),
            "win_rate":sum(v>0 for v in nets)/len(rs),
            "mean_net_usd":statistics.fmean(nets),
            "profit_factor":wins/losses if losses else (999.0 if wins else 0.0),
            "max_drawdown_pct":min(r.max_drawdown_pct for r in rs),
            "avoided_loss_usd":sum(r.avoided_loss_usd for r in rs),
            "missed_upside_usd":sum(r.missed_upside_usd for r in rs),
        }
    return out

def strategy_health(m:dict,min_trades:int=30)->str:
    if m.get("trades",0)<min_trades: return "INSUFFICIENT_SAMPLE"
    if m.get("net_pnl_usd",0)<=0 or m.get("profit_factor",0)<1.1: return "DOWNWEIGHT"
    if m.get("profit_factor",0)>=1.3 and m.get("max_drawdown_pct",0)>=-25: return "KEEP"
    return "WATCH"

def audit(kind,payload):
    return json.dumps({"version":VERSION,"ts":time.time(),"kind":kind,"payload":payload},
                      allow_nan=False,separators=(",",":"))
