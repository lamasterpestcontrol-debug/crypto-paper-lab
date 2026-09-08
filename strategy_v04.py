"""Strategy v0.4 extension modules for crypto-paper-lab.

Paper-only. No wallet/order execution.

Adds 7 modules:
1) contract safety
2) whale/dev-wallet risk
3) liquidity/slippage-aware sizing
4) dynamic exit
5) market regime
6) A/B experiment bookkeeping
7) live-trading graduation gate
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, Iterable
import math, json, statistics, time

VERSION = "strategy-v0.4.0"

def _f(x, default=0.0):
    try:
        x=float(x)
        return x if math.isfinite(x) else default
    except Exception:
        return default

# ---------- 1. CONTRACT SAFETY ----------

@dataclass
class ContractRiskInput:
    mint_enabled: Optional[bool] = None
    freeze_enabled: Optional[bool] = None
    honeypot: Optional[bool] = None
    buy_tax_pct: Optional[float] = None
    sell_tax_pct: Optional[float] = None
    lp_locked_pct: Optional[float] = None
    top10_holder_pct: Optional[float] = None
    dev_holder_pct: Optional[float] = None

@dataclass
class ContractRiskResult:
    pass_check: bool
    risk_score: float
    blockers: list[str]
    warnings: list[str]

def assess_contract_risk(x: ContractRiskInput) -> ContractRiskResult:
    blockers, warnings = [], []
    score = 0.0
    if x.honeypot is True:
        blockers.append("HONEYPOT")
        score += 100
    if x.mint_enabled is True:
        warnings.append("MINT_ENABLED")
        score += 15
    if x.freeze_enabled is True:
        warnings.append("FREEZE_ENABLED")
        score += 15
    if x.sell_tax_pct is not None:
        if x.sell_tax_pct >= 20:
            blockers.append("SELL_TAX_TOO_HIGH")
            score += 40
        elif x.sell_tax_pct >= 8:
            warnings.append("SELL_TAX_HIGH")
            score += 15
    if x.buy_tax_pct is not None and x.buy_tax_pct >= 10:
        warnings.append("BUY_TAX_HIGH")
        score += 10
    if x.lp_locked_pct is not None:
        if x.lp_locked_pct < 50:
            blockers.append("LP_WEAKLY_LOCKED")
            score += 35
        elif x.lp_locked_pct < 80:
            warnings.append("LP_LOCK_MEDIUM")
            score += 10
    if x.top10_holder_pct is not None:
        if x.top10_holder_pct >= 70:
            blockers.append("TOP10_CONCENTRATION_EXTREME")
            score += 35
        elif x.top10_holder_pct >= 50:
            warnings.append("TOP10_CONCENTRATION_HIGH")
            score += 15
    if x.dev_holder_pct is not None:
        if x.dev_holder_pct >= 20:
            blockers.append("DEV_HOLDING_EXTREME")
            score += 35
        elif x.dev_holder_pct >= 10:
            warnings.append("DEV_HOLDING_HIGH")
            score += 15
    return ContractRiskResult(not blockers, score, blockers, warnings)

# ---------- 2. WHALE / DEV WALLET ----------

@dataclass
class WalletFlowInput:
    smart_money_buy_usd_1h: float = 0
    smart_money_sell_usd_1h: float = 0
    dev_sell_usd_1h: float = 0
    dev_balance_change_pct_1h: float = 0
    insider_cluster_sell_ratio: float = 0
    unique_large_buyers_1h: int = 0

@dataclass
class WalletFlowResult:
    pass_check: bool
    score: float
    reason: str

def assess_wallet_flow(x: WalletFlowInput) -> WalletFlowResult:
    if x.dev_sell_usd_1h > 5000 or x.dev_balance_change_pct_1h <= -20:
        return WalletFlowResult(False, -100, "DEV_DISTRIBUTION")
    if x.insider_cluster_sell_ratio >= 0.5:
        return WalletFlowResult(False, -80, "INSIDER_DISTRIBUTION")
    net = _f(x.smart_money_buy_usd_1h) - _f(x.smart_money_sell_usd_1h)
    score = 0.0
    if net > 0:
        score += min(40, net / 1000)
    if x.unique_large_buyers_1h >= 3:
        score += min(20, x.unique_large_buyers_1h * 3)
    reason = "SMART_MONEY_ACCUMULATION" if score >= 15 else "NEUTRAL"
    return WalletFlowResult(True, score, reason)

# ---------- 3. LIQUIDITY / SLIPPAGE ----------

@dataclass
class SlippageResult:
    expected_price_impact_pct: float
    round_trip_cost_pct: float
    max_safe_usd: float
    pass_check: bool

def estimate_slippage(liquidity_usd: float, trade_usd: float, fee_pct: float = 0.3,
                      max_round_trip_pct: float = 6.0) -> SlippageResult:
    liq=max(_f(liquidity_usd),1.0)
    trade=max(_f(trade_usd),0.0)
    # Conservative paper approximation; real DEX router quote should replace this when available.
    impact_pct=min(100.0, (trade / liq) * 100.0 * 1.5)
    round_trip = impact_pct*2 + fee_pct*2
    max_safe = max(0.0, liq * max((max_round_trip_pct - fee_pct*2)/2/1.5/100.0, 0.0))
    return SlippageResult(impact_pct, round_trip, max_safe, round_trip <= max_round_trip_pct)

# ---------- 4. DYNAMIC EXIT ----------

@dataclass
class ExitInput:
    pnl_pct: float
    drawdown_from_peak_pct: float
    liquidity_change_pct: float
    volume_change_pct_15m: float
    dev_distribution: bool = False
    smart_money_net_sell: bool = False
    age_minutes: float = 0

@dataclass
class ExitDecision:
    action: str
    reason: str
    trim_pct: float = 0.0

def dynamic_exit(x: ExitInput) -> ExitDecision:
    if x.dev_distribution:
        return ExitDecision("EXIT_ALL","DEV_DISTRIBUTION",100)
    if x.liquidity_change_pct <= -35:
        return ExitDecision("EXIT_ALL","LIQUIDITY_COLLAPSE",100)
    if x.pnl_pct <= -25:
        return ExitDecision("EXIT_ALL","HARD_STOP",100)
    if x.pnl_pct >= 100 and x.drawdown_from_peak_pct <= -20:
        return ExitDecision("EXIT_ALL","TRAIL_FROM_2X",100)
    if x.pnl_pct >= 50 and (x.volume_change_pct_15m <= -40 or x.smart_money_net_sell):
        return ExitDecision("TRIM","MOMENTUM_DECAY",50)
    if x.pnl_pct >= 25 and x.age_minutes >= 240 and x.volume_change_pct_15m <= -30:
        return ExitDecision("TRIM","STALE_MOMENTUM",33)
    return ExitDecision("HOLD","NO_EXIT_SIGNAL",0)

# ---------- 5. MARKET REGIME ----------

@dataclass
class RegimeInput:
    btc_return_24h_pct: float = 0
    btc_volatility_7d_pct: float = 0
    btc_dominance_change_7d_pct: float = 0
    stablecoin_flow_score: float = 0
    alt_breadth_pct: float = 50

def classify_regime(x: RegimeInput) -> str:
    if x.btc_return_24h_pct <= -7 or x.btc_volatility_7d_pct >= 12:
        return "RISK_OFF"
    if x.btc_dominance_change_7d_pct >= 3 and x.alt_breadth_pct < 40:
        return "BTC_DOMINANCE_EXPANSION"
    if x.alt_breadth_pct >= 70 and x.stablecoin_flow_score > 0:
        return "ALT_RISK_ON"
    if x.alt_breadth_pct >= 80 and x.btc_return_24h_pct > 0:
        return "SPECULATIVE_MANIA"
    return "NEUTRAL"

def regime_position_multiplier(regime: str) -> float:
    return {
        "RISK_OFF": 0.25,
        "BTC_DOMINANCE_EXPANSION": 0.50,
        "ALT_RISK_ON": 1.00,
        "SPECULATIVE_MANIA": 0.75,
        "NEUTRAL": 0.75,
    }.get(regime,0.5)

# ---------- 6. A/B EXPERIMENTS ----------

@dataclass
class TradeOutcome:
    variant: str
    pnl_pct: float
    max_drawdown_pct: float

def experiment_summary(rows: Iterable[TradeOutcome]) -> dict[str,dict[str,float]]:
    by: Dict[str,list[TradeOutcome]] = {}
    for r in rows:
        by.setdefault(r.variant,[]).append(r)
    out={}
    for k,rs in by.items():
        pnls=[r.pnl_pct for r in rs]
        losses=[-p for p in pnls if p<0]
        wins=[p for p in pnls if p>0]
        gross_win=sum(wins)
        gross_loss=sum(losses)
        out[k]={
            "n": float(len(rs)),
            "win_rate": sum(p>0 for p in pnls)/len(rs) if rs else 0.0,
            "mean_pnl_pct": statistics.fmean(pnls) if pnls else 0.0,
            "median_pnl_pct": statistics.median(pnls) if pnls else 0.0,
            "profit_factor": (gross_win/gross_loss) if gross_loss>0 else (999.0 if gross_win>0 else 0.0),
            "max_drawdown_pct": min((r.max_drawdown_pct for r in rs), default=0.0),
        }
    return out

# ---------- 7. LIVE-TRADING GRADUATION GATE ----------

@dataclass
class GraduationInput:
    historical_trades: int
    realtime_paper_trades: int
    historical_profit_factor: float
    realtime_profit_factor: float
    realtime_expectancy_pct: float
    max_drawdown_pct: float
    slippage_and_fees_included: bool
    no_future_leakage_verified: bool
    live_vs_backtest_gap_pct: float

@dataclass
class GraduationResult:
    ready: bool
    failed: list[str]

def graduation_gate(x: GraduationInput) -> GraduationResult:
    failed=[]
    if x.historical_trades < 300: failed.append("HISTORICAL_SAMPLE_LT_300")
    if x.realtime_paper_trades < 50: failed.append("PAPER_SAMPLE_LT_50")
    if x.historical_profit_factor < 1.3: failed.append("HISTORICAL_PF_LT_1.3")
    if x.realtime_profit_factor < 1.2: failed.append("PAPER_PF_LT_1.2")
    if x.realtime_expectancy_pct <= 0: failed.append("NONPOSITIVE_EXPECTANCY")
    if x.max_drawdown_pct < -25: failed.append("DRAWDOWN_TOO_HIGH")
    if not x.slippage_and_fees_included: failed.append("COSTS_NOT_INCLUDED")
    if not x.no_future_leakage_verified: failed.append("LEAKAGE_NOT_CLEARED")
    if abs(x.live_vs_backtest_gap_pct) > 35: failed.append("BACKTEST_LIVE_GAP_TOO_LARGE")
    return GraduationResult(not failed, failed)

def audit_record(kind: str, payload: Dict[str,Any]) -> str:
    return json.dumps({
        "version": VERSION,
        "kind": kind,
        "ts": time.time(),
        "payload": payload,
    }, allow_nan=False, separators=(",",":"))
