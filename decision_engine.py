"""Unified paper-trading decision engine for crypto-paper-lab.

This module turns already-collected structured observations into deterministic,
auditable decisions. It does not place real orders and has no wallet/account APIs.

Design goals:
- hard safety/risk gates override all scores;
- market regime selects risk posture, not a permanent "best" strategy;
- MEME and utility-new-token strategies are distinct long-only paper scopes;
- major-coin lead/lag research may emit long/short PAPER candidates;
- unstructured news/Telegram/GMGN inputs must first be normalized into scored
  structured evidence by separate ingestion workers before they can affect a trade;
- every decision carries reasons and component scores for later calibration.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import math
from typing import Iterable

VERSION = "decision-engine-0.3.0"


def _f(x: float) -> float:
    v = float(x)
    if not math.isfinite(v):
        raise ValueError("non-finite input")
    return v


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, _f(x)))


@dataclass(frozen=True)
class ExternalEvent:
    """Structured output of a news/event ingestion layer.

    source_tier: A=official original source, B=professional wire/reliable media,
    C=market-confirmed secondary source, D=social/Telegram early lead.
    direction: -1 risk-off, 0 unclear, +1 risk-on.
    """
    source_tier: str
    severity: float
    freshness: float
    corroborations: int
    market_confirmed: bool
    direction: int
    category: str = "OTHER"

    def __post_init__(self):
        if self.source_tier not in {"A", "B", "C", "D"}:
            raise ValueError("invalid source tier")
        if self.direction not in {-1, 0, 1}:
            raise ValueError("invalid direction")
        if not (0 <= self.severity <= 1 and 0 <= self.freshness <= 1):
            raise ValueError("invalid event scale")
        if self.corroborations < 0:
            raise ValueError("invalid corroborations")


def event_reliability(e: ExternalEvent) -> float:
    base = {"A": 0.90, "B": 0.75, "C": 0.60, "D": 0.25}[e.source_tier]
    corr = min(0.20, e.corroborations * 0.07)
    market = 0.10 if e.market_confirmed else 0.0
    # Social-only source cannot become highly trusted without independent confirmation.
    if e.source_tier == "D" and e.corroborations == 0 and not e.market_confirmed:
        return min(0.30, base * e.freshness)
    return min(1.0, (base + corr + market) * e.freshness)


def external_event_score(events: Iterable[ExternalEvent]) -> tuple[float, float, list[str]]:
    """Return signed score [-100,100], shock confidence [0,1], reasons."""
    signed = 0.0
    shock_conf = 0.0
    reasons: list[str] = []
    for e in events:
        rel = event_reliability(e)
        contribution = 100.0 * rel * e.severity * e.direction
        signed += contribution
        if e.severity >= 0.75 and rel >= 0.65:
            shock_conf = max(shock_conf, rel * e.severity)
            reasons.append(f"EVENT_{e.category}_{e.source_tier}")
    return max(-100.0, min(100.0, signed)), min(1.0, shock_conf), reasons


@dataclass(frozen=True)
class UtilityTokenInput:
    chain: str
    technical_entry_ready: bool
    technical_strength: float          # 0..100
    utility_confidence: float          # 0..100
    liquidity_quality: float           # 0..100
    exit_liquidity_pass: bool
    persistence_confirmed: bool
    gmgn_smart_money_score: float | None = None # optional GMGN/smart-money corroboration
    gmgn_insider_risk: float | None = None       # optional; higher worse
    chain_leader_aligned: bool = True
    contract_risk_pass: bool | None = None

    def __post_init__(self):
        for x in (self.technical_strength, self.utility_confidence, self.liquidity_quality):
            if not 0 <= float(x) <= 100:
                raise ValueError("score outside 0..100")
        for x in (self.gmgn_smart_money_score, self.gmgn_insider_risk):
            if x is not None and not 0 <= float(x) <= 100:
                raise ValueError("score outside 0..100")


@dataclass(frozen=True)
class MemeTokenInput:
    """High-volatility MEME token input. Utility evidence is intentionally not required.

    MEME risk is judged with market/chain alignment, true technical structure, liquidity,
    repeated observations, GMGN/smart-money corroboration, holder concentration and
    insider/contract safety. This scope remains long-only and PAPER-only.
    """
    chain: str
    technical_entry_ready: bool
    technical_strength: float          # 0..100
    liquidity_quality: float           # 0..100
    exit_liquidity_pass: bool
    persistence_confirmed: bool
    gmgn_smart_money_score: float | None = None
    gmgn_insider_risk: float | None = None
    gmgn_top10_pct: float | None = None
    gmgn_sighting_streak: int = 0
    chain_leader_aligned: bool = True
    contract_risk_pass: bool | None = None

    def __post_init__(self):
        for x in (self.technical_strength, self.liquidity_quality):
            if not 0 <= float(x) <= 100:
                raise ValueError("score outside 0..100")
        for x in (self.gmgn_smart_money_score, self.gmgn_insider_risk, self.gmgn_top10_pct):
            if x is not None and not 0 <= float(x) <= 100:
                raise ValueError("score outside 0..100")
        if self.gmgn_sighting_streak < 0:
            raise ValueError("invalid sighting streak")


@dataclass(frozen=True)
class MajorLagInput:
    symbol: str
    direction: int                    # +1 BTC-led rise, -1 BTC-led fall
    lag_confidence: float             # 0..1 recent stability
    reaction_completion: float        # 0..1, lower means more underreacted
    expected_move_bps: float
    total_cost_bps: float
    independent_catalyst_conflict: bool = False

    def __post_init__(self):
        if self.symbol not in {"ETH", "XRP", "SOL", "BNB"}:
            raise ValueError("unsupported lag target")
        if self.direction not in {-1, 1}:
            raise ValueError("invalid lag direction")
        if not 0 <= self.lag_confidence <= 1 or not 0 <= self.reaction_completion <= 1:
            raise ValueError("invalid lag scale")
        _f(self.expected_move_bps); _f(self.total_cost_bps)


@dataclass(frozen=True)
class DecisionContext:
    regime: str                       # RISK_ON/NEUTRAL/RISK_OFF/SHOCK
    shock_direction: str | None
    market_confidence: float
    external_signed_score: float = 0.0
    external_shock_confidence: float = 0.0
    cross_asset_score: float = 0.0
    cross_asset_quality: float = 0.0
    cross_asset_shock: bool = False
    calibrated_variant: str | None = None

    def __post_init__(self):
        if self.regime not in {"RISK_ON", "NEUTRAL", "RISK_OFF", "SHOCK"}:
            raise ValueError("invalid regime")
        if self.shock_direction not in {None, "UP", "DOWN"}:
            raise ValueError("invalid shock direction")
        if not 0 <= self.market_confidence <= 1 or not 0 <= self.external_shock_confidence <= 1:
            raise ValueError("invalid confidence")
        if not -100 <= self.external_signed_score <= 100 or not -100 <= self.cross_asset_score <= 100:
            raise ValueError("invalid external/cross-asset score")
        if not 0 <= self.cross_asset_quality <= 1:
            raise ValueError("invalid cross-asset quality")
        if self.calibrated_variant not in {None,"conservative","balanced","aggressive"}:
            raise ValueError("invalid calibrated variant")


@dataclass(frozen=True)
class Decision:
    action: str
    variant: str
    confidence: float
    score: float
    reasons: tuple[str, ...]
    metadata: dict

    def to_dict(self):
        return asdict(self)


VARIANT_BASE = {
    "conservative": 0.70,
    "balanced": 1.00,
    "aggressive": 1.20,
    "halt": 0.0,
}


def _variant(ctx: DecisionContext, chain_leader_aligned: bool = True) -> str:
    # External shock may strengthen, never weaken, a hard safety posture.
    if ctx.regime == "SHOCK" or ctx.external_shock_confidence >= 0.72 or (ctx.cross_asset_shock and ctx.cross_asset_quality >= .66):
        return "halt"
    if ctx.regime == "RISK_OFF" or ctx.external_signed_score <= -55 or (ctx.cross_asset_quality >= .66 and ctx.cross_asset_score <= -65):
        return "conservative"
    if ctx.regime == "RISK_ON" and chain_leader_aligned and ctx.external_signed_score > -25 and (ctx.cross_asset_quality < .66 or ctx.cross_asset_score > -40):
        return ctx.calibrated_variant if ctx.calibrated_variant in {"conservative","balanced","aggressive"} else "aggressive"
    if ctx.regime == "NEUTRAL" and ctx.calibrated_variant in {"conservative","balanced"}:
        return ctx.calibrated_variant
    return "balanced"


def decide_utility_long(x: UtilityTokenInput, ctx: DecisionContext) -> Decision:
    reasons: list[str] = []
    variant = _variant(ctx, x.chain_leader_aligned)
    # Hard blockers always win. No weighted score can buy through them.
    if variant == "halt":
        return Decision("BLOCK_NEW_ENTRY", "halt", 1.0, 0.0, ("MARKET_OR_EVENT_SHOCK",), {"chain": x.chain})
    if x.contract_risk_pass is False:
        return Decision("BLOCK_NEW_ENTRY", variant, 1.0, 0.0, ("CONTRACT_RISK_FAIL",), {"chain": x.chain})
    if not x.exit_liquidity_pass:
        return Decision("BLOCK_NEW_ENTRY", variant, 1.0, 0.0, ("EXIT_LIQUIDITY_FAIL",), {"chain": x.chain})
    if x.gmgn_insider_risk is not None and x.gmgn_insider_risk >= 70:
        return Decision("BLOCK_NEW_ENTRY", variant, .95, 0.0, ("INSIDER_RISK_HIGH",), {"chain": x.chain})
    # Unknown contract/insider data is never treated as zero risk. For the adaptive
    # path it prevents an aggressive posture until corroboration arrives. Fixed A/B
    # paper variants may still run separately for research.
    if variant == "aggressive" and (x.contract_risk_pass is None or x.gmgn_insider_risk is None):
        variant = "balanced"
        reasons.append("RISK_INTEL_INCOMPLETE_DOWNGRADE")
    # Utility-new-token scope requires independent utility evidence. Smart money or
    # a fast GMGN launch signal can accelerate discovery, but cannot replace utility proof.
    if x.utility_confidence < 55:
        return Decision("WATCH", variant, .90, 0.0, ("REAL_UTILITY_NOT_CONFIRMED",), {"chain": x.chain,"utility_confidence":x.utility_confidence})
    if not x.technical_entry_ready:
        return Decision("WATCH", variant, .80, 0.0, ("TECHNICAL_ENTRY_NOT_READY",), {"chain": x.chain})
    if not x.persistence_confirmed:
        return Decision("WATCH", variant, .85, 0.0, ("PERSISTENCE_NOT_CONFIRMED",), {"chain": x.chain})

    # Weighted quality score. Weights are research parameters to calibrate by walk-forward,
    # not claims of optimality.
    score = (
        0.30 * x.technical_strength
        + 0.22 * x.utility_confidence
        + 0.20 * x.liquidity_quality
        + 0.13 * (50.0 if x.gmgn_smart_money_score is None else x.gmgn_smart_money_score)
        + 0.10 * (100 if x.chain_leader_aligned else 35)
        + 0.05 * (100 if ctx.market_confidence >= .65 else 50)
    )
    # Macro/external information is secondary for utility/meme tokens unless it becomes a shock.
    score += max(-8.0, min(8.0, ctx.external_signed_score * 0.08))
    if ctx.cross_asset_quality >= .5:
        score += max(-5.0, min(5.0, ctx.cross_asset_score * 0.05))
    if x.contract_risk_pass is None:
        score -= 8.0; reasons.append("CONTRACT_RISK_UNKNOWN_PAPER_ONLY")
    if x.gmgn_smart_money_score is None:
        score -= 3.0; reasons.append("GMGN_SMART_MONEY_UNAVAILABLE")
    if x.gmgn_insider_risk is None:
        score -= 4.0; reasons.append("GMGN_INSIDER_RISK_UNAVAILABLE")
    if x.chain_leader_aligned:
        reasons.append("CHAIN_LEADER_ALIGNED")
    if x.gmgn_smart_money_score is not None and x.gmgn_smart_money_score >= 65:
        reasons.append("SMART_MONEY_SUPPORT")
    if x.utility_confidence >= 70:
        reasons.append("UTILITY_EVIDENCE_STRONG")
    threshold = {"conservative": 78.0, "balanced": 72.0, "aggressive": 68.0}[variant]
    action = "OPEN_LONG_PAPER" if score >= threshold else "WATCH"
    conf = min(.95, .50 + max(0.0, score - 50.0) / 100.0)
    reasons.append("SCORE_PASS" if action.startswith("OPEN") else "SCORE_BELOW_VARIANT_THRESHOLD")
    return Decision(action, variant, conf, round(score, 3), tuple(reasons), {"threshold": threshold, "chain": x.chain, "asset_scope": "UTILITY_NEW_TOKEN"})


def decide_meme_long(x: MemeTokenInput, ctx: DecisionContext) -> Decision:
    """Decision path for MEME coins. No utility gate is applied by design."""
    reasons: list[str] = []
    variant = _variant(ctx, x.chain_leader_aligned)
    if variant == "halt":
        return Decision("BLOCK_NEW_ENTRY", "halt", 1.0, 0.0, ("MARKET_OR_EVENT_SHOCK",), {"chain": x.chain, "asset_scope": "MEME"})
    if x.contract_risk_pass is False:
        return Decision("BLOCK_NEW_ENTRY", variant, 1.0, 0.0, ("CONTRACT_RISK_FAIL",), {"chain": x.chain, "asset_scope": "MEME"})
    if not x.exit_liquidity_pass:
        return Decision("BLOCK_NEW_ENTRY", variant, 1.0, 0.0, ("EXIT_LIQUIDITY_FAIL",), {"chain": x.chain, "asset_scope": "MEME"})
    if x.gmgn_insider_risk is not None and x.gmgn_insider_risk >= 70:
        return Decision("BLOCK_NEW_ENTRY", variant, .98, 0.0, ("INSIDER_RISK_HIGH",), {"chain": x.chain, "asset_scope": "MEME"})
    if x.gmgn_top10_pct is not None and x.gmgn_top10_pct >= 70:
        return Decision("BLOCK_NEW_ENTRY", variant, .98, 0.0, ("TOP10_CONCENTRATION_EXTREME",), {"chain": x.chain, "asset_scope": "MEME"})
    if not x.technical_entry_ready:
        return Decision("WATCH", variant, .80, 0.0, ("TECHNICAL_ENTRY_NOT_READY",), {"chain": x.chain, "asset_scope": "MEME"})
    if not x.persistence_confirmed:
        return Decision("WATCH", variant, .88, 0.0, ("PERSISTENCE_NOT_CONFIRMED",), {"chain": x.chain, "asset_scope": "MEME"})
    # Unknown safety information cannot earn risk credit. It may still remain in fixed
    # A/B paper research, but adaptive aggression is reduced until corroborated.
    if variant == "aggressive" and (x.contract_risk_pass is None or x.gmgn_insider_risk is None):
        variant = "balanced"
        reasons.append("RISK_INTEL_INCOMPLETE_DOWNGRADE")
    smart = 40.0 if x.gmgn_smart_money_score is None else x.gmgn_smart_money_score
    score = (
        0.35 * x.technical_strength
        + 0.22 * x.liquidity_quality
        + 0.18 * smart
        + 0.15 * (100 if x.chain_leader_aligned else 30)
        + 0.10 * (100 if ctx.market_confidence >= .65 else 45)
    )
    # MEME coins react strongly to crypto risk appetite. Macro is still secondary unless
    # it becomes SHOCK, which is handled above as a hard veto.
    score += max(-10.0, min(10.0, ctx.external_signed_score * 0.10))
    if ctx.cross_asset_quality >= .5:
        score += max(-6.0, min(6.0, ctx.cross_asset_score * 0.06))
    if x.gmgn_insider_risk is not None:
        score -= min(15.0, x.gmgn_insider_risk * 0.15)
    else:
        score -= 5.0; reasons.append("GMGN_INSIDER_RISK_UNAVAILABLE")
    if x.gmgn_top10_pct is not None:
        score -= max(0.0, min(10.0, (x.gmgn_top10_pct - 35.0) * .20))
    else:
        score -= 3.0; reasons.append("TOP10_CONCENTRATION_UNKNOWN")
    if x.contract_risk_pass is None:
        score -= 8.0; reasons.append("CONTRACT_RISK_UNKNOWN_PAPER_ONLY")
    if x.gmgn_smart_money_score is None:
        score -= 4.0; reasons.append("GMGN_SMART_MONEY_UNAVAILABLE")
    elif x.gmgn_smart_money_score >= 65:
        reasons.append("SMART_MONEY_SUPPORT")
    if x.gmgn_sighting_streak >= 3:
        score += 4.0; reasons.append("GMGN_REPEATED_SIGHTINGS")
    if x.chain_leader_aligned:
        reasons.append("CHAIN_LEADER_ALIGNED")
    threshold = {"conservative": 80.0, "balanced": 74.0, "aggressive": 70.0}[variant]
    action = "OPEN_LONG_PAPER" if score >= threshold else "WATCH"
    conf = min(.95, .48 + max(0.0, score - 50.0) / 100.0)
    reasons.append("SCORE_PASS" if action.startswith("OPEN") else "SCORE_BELOW_VARIANT_THRESHOLD")
    return Decision(action, variant, conf, round(score,3), tuple(reasons), {"threshold": threshold, "chain": x.chain, "asset_scope": "MEME"})


# Backward-compatible aliases for old tests/imports; runtime code uses explicit scopes.
MiniTokenInput = UtilityTokenInput
decide_mini_long = decide_utility_long


def decide_major_lag(x: MajorLagInput, ctx: DecisionContext) -> Decision:
    """Decide a PAPER candidate for major-coin BTC lead/lag research.

    Long for BTC-led rise, short for BTC-led fall. This is not used for MEME or utility-new-token positions.
    """
    reasons: list[str] = []
    if x.independent_catalyst_conflict:
        return Decision("WATCH", "lead_lag", .95, 0.0, ("TARGET_HAS_INDEPENDENT_CATALYST",), {"symbol": x.symbol})
    if ctx.regime == "SHOCK" and ctx.shock_direction and ((ctx.shock_direction == "UP") != (x.direction > 0)):
        return Decision("WATCH", "lead_lag", .90, 0.0, ("IMPULSE_CONFLICTS_WITH_SHOCK_DIRECTION",), {"symbol": x.symbol})
    if ctx.cross_asset_quality >= .66:
        if (x.direction > 0 and ctx.cross_asset_score <= -50) or (x.direction < 0 and ctx.cross_asset_score >= 50):
            return Decision("WATCH", "lead_lag", .90, 0.0, ("CROSS_ASSET_CONFLICT",), {"symbol": x.symbol,"cross_asset_score":ctx.cross_asset_score})
    if x.lag_confidence < .58:
        return Decision("WATCH", "lead_lag", .85, 0.0, ("LAG_RELATION_NOT_STABLE",), {"symbol": x.symbol})
    if x.reaction_completion >= .70:
        return Decision("WATCH", "lead_lag", .85, 0.0, ("TARGET_ALREADY_MOSTLY_REACTED",), {"symbol": x.symbol})
    edge = abs(x.expected_move_bps) - max(0.0, x.total_cost_bps)
    if edge <= 0:
        return Decision("WATCH", "lead_lag", .90, 0.0, ("NO_NET_EDGE_AFTER_COSTS",), {"symbol": x.symbol, "net_edge_bps": edge})
    underreaction = 1.0 - x.reaction_completion
    score = 100.0 * (0.50 * x.lag_confidence + 0.30 * underreaction + 0.20 * min(1.0, edge / 35.0))
    if ctx.cross_asset_quality >= .5:
        alignment = ctx.cross_asset_score if x.direction > 0 else -ctx.cross_asset_score
        score += max(-8.0,min(8.0,alignment*.08))
    # Need meaningful margin above costs, not just a tiny positive theoretical edge.
    if edge < 5.0 or score < 68.0:
        return Decision("WATCH", "lead_lag", min(.9, score/100), round(score, 3), ("EDGE_OR_SCORE_TOO_SMALL",), {"symbol": x.symbol, "net_edge_bps": round(edge,3)})
    action = "OPEN_LONG_MAJOR_PAPER" if x.direction > 0 else "OPEN_SHORT_MAJOR_PAPER"
    reasons += ["BTC_LEADS", "TARGET_UNDERREACTED", "NET_EDGE_AFTER_COSTS"]
    return Decision(action, "lead_lag", min(.96, .55 + score/220), round(score, 3), tuple(reasons), {"symbol": x.symbol, "net_edge_bps": round(edge,3)})
