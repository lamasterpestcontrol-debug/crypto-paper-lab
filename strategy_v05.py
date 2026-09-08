"""Strategy v0.5 PRE-DISCOVERY.

Paper/research only. No wallet or order execution.
Ranks projects before price momentum using independent evidence streams.
"""
from dataclasses import dataclass, asdict
from typing import Optional
import json, time

VERSION="strategy-v0.5.0"

@dataclass
class Evidence:
    ts: float
    github_activity: float=0.0       # 0..1
    product_live: float=0.0          # 0..1
    ecosystem_support: float=0.0     # 0..1
    contract_deployed: float=0.0     # 0..1
    first_pool: float=0.0            # 0..1
    onchain_adoption: float=0.0      # 0..1
    social_early_growth: float=0.0   # 0..1
    catalyst: float=0.0              # 0..1
    official_identity: float=0.0     # 0..1
    independent_sources: int=0
    paid_promo_risk: float=0.0       # 0..1
    age_hours: Optional[float]=None

@dataclass
class PreDiscoveryResult:
    stage: str
    score: float
    reason: str
    confidence: str
    route_to_risk: bool

def clamp(x):
    try: return max(0.0,min(1.0,float(x)))
    except Exception: return 0.0

def opportunity_score(e:Evidence)->float:
    # Research/product evidence deliberately dominates social hype.
    s=(
        20*clamp(e.product_live)+
        18*clamp(e.github_activity)+
        15*clamp(e.onchain_adoption)+
        12*clamp(e.ecosystem_support)+
        12*clamp(e.contract_deployed)+
        8*clamp(e.first_pool)+
        8*clamp(e.social_early_growth)+
        7*clamp(e.catalyst)
    )
    # Unverified identity and paid promotion reduce confidence/score.
    s -= 15*(1-clamp(e.official_identity))
    s -= 15*clamp(e.paid_promo_risk)
    return max(0.0,min(100.0,s))

def classify(e:Evidence)->PreDiscoveryResult:
    score=opportunity_score(e)
    if e.independent_sources < 2:
        return PreDiscoveryResult("RESEARCH",score,"INSUFFICIENT_INDEPENDENT_EVIDENCE","LOW",False)
    if e.official_identity < .7:
        return PreDiscoveryResult("RESEARCH",score,"OFFICIAL_IDENTITY_NOT_CONFIRMED","LOW",False)
    if e.product_live < .35 and e.github_activity < .35:
        return PreDiscoveryResult("REJECT",score,"NO_PRODUCT_OR_DEV_EVIDENCE","MEDIUM",False)

    confidence="HIGH" if e.independent_sources>=4 and score>=65 else "MEDIUM"
    # Pre-token/project clue.
    if e.contract_deployed < .5 and e.first_pool < .5:
        stage="PRE_TOKEN_WATCH"
        return PreDiscoveryResult(stage,score,"REAL_PROJECT_BEFORE_TOKEN/POOL",confidence,score>=50)
    # Contract exists but tradability not yet established.
    if e.contract_deployed>=.5 and e.first_pool<.5:
        return PreDiscoveryResult("CONTRACT_WATCH",score,"CONTRACT_BEFORE_FIRST_POOL",confidence,score>=55)
    # First pool: immediately hand off to existing safety/liquidity pipeline.
    if e.first_pool>=.5:
        return PreDiscoveryResult("FIRST_POOL_HANDOFF",score,"HANDOFF_TO_RISK_AND_LIQUIDITY",confidence,score>=55)
    return PreDiscoveryResult("WATCH",score,"MORE_EVIDENCE_NEEDED",confidence,False)

def freshness_weight(age_hours:Optional[float])->float:
    if age_hours is None: return 0.7
    if age_hours<=6:return 1.0
    if age_hours<=24:return .9
    if age_hours<=72:return .75
    if age_hours<=168:return .55
    return .35

def audit(e:Evidence,r:PreDiscoveryResult)->str:
    return json.dumps({"version":VERSION,"ts":time.time(),"evidence":asdict(e),"result":asdict(r)},
                      allow_nan=False,separators=(",",":"))
