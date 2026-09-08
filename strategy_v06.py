"""Strategy v0.6: persistence + parameter search + walk-forward validation.

Paper/research only. No wallet/order execution.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Iterable, Sequence, Callable, Any
import itertools, json, math, statistics, time

VERSION="strategy-v0.6.0"

@dataclass
class SignalPoint:
    ts: float
    score: float
    risk_pass: bool
    exit_pass: bool
    liquidity_pass: bool
    vbp: bool=False
    whale_confirm: bool=False
    social_confirm: bool=False

@dataclass
class PersistenceResult:
    confirmed: bool
    streak: int
    required: int
    reason: str

def persistence_check(points: Sequence[SignalPoint], required: int=3, min_score: float=65.0) -> PersistenceResult:
    if required < 1:
        raise ValueError("required must be >=1")
    streak=0
    for p in reversed(points):
        ok=(p.score>=min_score and p.risk_pass and p.exit_pass and p.liquidity_pass)
        if ok:
            streak += 1
        else:
            break
    return PersistenceResult(streak>=required, streak, required,
                             "CONFIRMED" if streak>=required else "WAIT_CONFIRMATION")

@dataclass
class Trade:
    entry_ts: float
    exit_ts: float
    pnl_pct: float
    max_drawdown_pct: float
    fees_slippage_pct: float=0.0

def net_pnl(t: Trade) -> float:
    return t.pnl_pct - t.fees_slippage_pct

def metrics(rows: Iterable[Trade]) -> dict[str,float]:
    rs=list(rows)
    if not rs:
        return {"n":0.0,"win_rate":0.0,"mean_net_pct":0.0,"median_net_pct":0.0,
                "profit_factor":0.0,"max_drawdown_pct":0.0}
    pnls=[net_pnl(r) for r in rs]
    wins=sum(x for x in pnls if x>0)
    losses=-sum(x for x in pnls if x<0)
    return {
        "n":float(len(rs)),
        "win_rate":sum(x>0 for x in pnls)/len(rs),
        "mean_net_pct":statistics.fmean(pnls),
        "median_net_pct":statistics.median(pnls),
        "profit_factor":wins/losses if losses>0 else (999.0 if wins>0 else 0.0),
        "max_drawdown_pct":min((r.max_drawdown_pct for r in rs), default=0.0),
    }

@dataclass(frozen=True)
class Params:
    min_score: float=65.0
    persistence_ticks: int=3
    max_slippage_pct: float=6.0
    pullback_pct: float=15.0
    volume_ratio: float=1.8

def parameter_grid(
    min_scores=(60.0,65.0,70.0),
    persistence_ticks=(2,3,4),
    max_slippage_pct=(4.0,6.0,8.0),
    pullback_pct=(10.0,15.0,20.0),
    volume_ratio=(1.5,1.8,2.2),
):
    for vals in itertools.product(min_scores,persistence_ticks,max_slippage_pct,pullback_pct,volume_ratio):
        yield Params(*vals)

@dataclass
class ParamScore:
    params: Params
    objective: float
    train_metrics: dict[str,float]

def objective(m:dict[str,float]) -> float:
    # Favors robust expectancy/PF and penalizes drawdown and tiny samples.
    n=m["n"]
    if n < 30:
        return -999.0
    dd=abs(m["max_drawdown_pct"])
    return (m["mean_net_pct"]*0.45 + min(m["profit_factor"],5.0)*10*0.35 +
            m["win_rate"]*100*0.20 - dd*0.35)

def optimize(train_rows: Sequence[Any],
             backtest_fn: Callable[[Sequence[Any],Params], Sequence[Trade]],
             grid: Iterable[Params]|None=None) -> ParamScore:
    best=None
    for p in (grid or parameter_grid()):
        m=metrics(backtest_fn(train_rows,p))
        s=objective(m)
        cur=ParamScore(p,s,m)
        if best is None or cur.objective>best.objective:
            best=cur
    if best is None:
        raise ValueError("empty grid")
    return best

@dataclass
class WalkForwardFold:
    train_start:int
    train_end:int
    test_start:int
    test_end:int
    params:Params
    train_metrics:dict[str,float]
    test_metrics:dict[str,float]

def walk_forward(rows: Sequence[Any],
                 backtest_fn: Callable[[Sequence[Any],Params], Sequence[Trade]],
                 train_size:int=300, test_size:int=100, step:int=100,
                 grid: Iterable[Params]|None=None) -> list[WalkForwardFold]:
    if train_size<30 or test_size<10 or step<1:
        raise ValueError("invalid sizes")
    folds=[]
    start=0
    while start+train_size+test_size <= len(rows):
        tr=rows[start:start+train_size]
        te=rows[start+train_size:start+train_size+test_size]
        best=optimize(tr,backtest_fn,grid)
        tm=metrics(backtest_fn(te,best.params))
        folds.append(WalkForwardFold(
            start,start+train_size,start+train_size,start+train_size+test_size,
            best.params,best.train_metrics,tm))
        start += step
    return folds

def walk_forward_summary(folds: Sequence[WalkForwardFold]) -> dict[str,float]:
    if not folds:
        return {"folds":0.0,"avg_test_pf":0.0,"avg_test_mean_net_pct":0.0,
                "positive_folds_pct":0.0,"worst_test_drawdown_pct":0.0}
    return {
        "folds":float(len(folds)),
        "avg_test_pf":statistics.fmean(f.test_metrics["profit_factor"] for f in folds),
        "avg_test_mean_net_pct":statistics.fmean(f.test_metrics["mean_net_pct"] for f in folds),
        "positive_folds_pct":sum(f.test_metrics["mean_net_pct"]>0 for f in folds)/len(folds),
        "worst_test_drawdown_pct":min(f.test_metrics["max_drawdown_pct"] for f in folds),
    }

def audit(kind:str,payload:dict)->str:
    return json.dumps({"version":VERSION,"ts":time.time(),"kind":kind,"payload":payload},
                      allow_nan=False,separators=(",",":"))
