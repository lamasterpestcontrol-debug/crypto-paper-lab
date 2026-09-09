import unittest
from decision_engine import *

class DecisionEngineTests(unittest.TestCase):
    def ctx(self,regime="NEUTRAL",**kw):
        return DecisionContext(regime,kw.pop("shock_direction",None),kw.pop("market_confidence",.8),**kw)
    def mini(self,**kw):
        base=dict(chain="solana",technical_entry_ready=True,technical_strength=82,utility_confidence=80,
                  liquidity_quality=85,contract_risk_pass=True,exit_liquidity_pass=True,
                  persistence_confirmed=True,gmgn_smart_money_score=72,gmgn_insider_risk=10,
                  chain_leader_aligned=True)
        base.update(kw);return MiniTokenInput(**base)
    def lag(self,**kw):
        base=dict(symbol="SOL",direction=1,lag_confidence=.78,reaction_completion=.25,
                  expected_move_bps=28,total_cost_bps=5,independent_catalyst_conflict=False)
        base.update(kw);return MajorLagInput(**base)

    def test_official_event_has_more_weight_than_unconfirmed_telegram(self):
        a=ExternalEvent("A",.8,1,0,False,-1,"WAR")
        d=ExternalEvent("D",.8,1,0,False,-1,"WAR")
        self.assertGreater(event_reliability(a),event_reliability(d))
    def test_telegram_only_cannot_become_high_confidence(self):
        d=ExternalEvent("D",1,1,0,False,-1,"WAR")
        self.assertLessEqual(event_reliability(d),.30)
    def test_corroborated_event_can_trigger_shock_confidence(self):
        e=ExternalEvent("A",.95,1,1,True,-1,"WAR")
        s,shock,_=external_event_score([e])
        self.assertLess(s,0);self.assertGreaterEqual(shock,.72)
    def test_shock_blocks_mini_entry(self):
        d=decide_mini_long(self.mini(),self.ctx("SHOCK",shock_direction="DOWN"))
        self.assertEqual(d.action,"BLOCK_NEW_ENTRY")
    def test_contract_gate_overrides_high_score(self):
        d=decide_mini_long(self.mini(contract_risk_pass=False),self.ctx("RISK_ON"))
        self.assertEqual(d.action,"BLOCK_NEW_ENTRY")
    def test_high_insider_risk_blocks(self):
        d=decide_mini_long(self.mini(gmgn_insider_risk=85),self.ctx("RISK_ON"))
        self.assertEqual(d.action,"BLOCK_NEW_ENTRY")
    def test_no_persistence_no_entry(self):
        d=decide_mini_long(self.mini(persistence_confirmed=False),self.ctx("RISK_ON"))
        self.assertEqual(d.action,"WATCH")
    def test_risk_on_aligned_can_be_aggressive(self):
        d=decide_mini_long(self.mini(),self.ctx("RISK_ON"))
        self.assertEqual(d.variant,"aggressive");self.assertEqual(d.action,"OPEN_LONG_PAPER")
    def test_risk_on_without_chain_alignment_not_aggressive(self):
        d=decide_mini_long(self.mini(chain_leader_aligned=False),self.ctx("RISK_ON"))
        self.assertNotEqual(d.variant,"aggressive")
    def test_risk_off_selects_conservative(self):
        d=decide_mini_long(self.mini(),self.ctx("RISK_OFF"))
        self.assertEqual(d.variant,"conservative")
    def test_major_lag_long(self):
        d=decide_major_lag(self.lag(direction=1),self.ctx("RISK_ON"))
        self.assertEqual(d.action,"OPEN_LONG_MAJOR_PAPER")
    def test_major_lag_short(self):
        d=decide_major_lag(self.lag(direction=-1),self.ctx("RISK_OFF"))
        self.assertEqual(d.action,"OPEN_SHORT_MAJOR_PAPER")
    def test_major_lag_requires_stable_relationship(self):
        d=decide_major_lag(self.lag(lag_confidence=.4),self.ctx())
        self.assertEqual(d.action,"WATCH")
    def test_major_lag_rejects_completed_reaction(self):
        d=decide_major_lag(self.lag(reaction_completion=.9),self.ctx())
        self.assertEqual(d.action,"WATCH")
    def test_major_lag_requires_cost_adjusted_edge(self):
        d=decide_major_lag(self.lag(expected_move_bps=5,total_cost_bps=7),self.ctx())
        self.assertEqual(d.action,"WATCH")
    def test_independent_catalyst_cancels_lag_trade(self):
        d=decide_major_lag(self.lag(independent_catalyst_conflict=True),self.ctx())
        self.assertEqual(d.action,"WATCH")

if __name__=='__main__':unittest.main()

class CalibrationSafetyTests(unittest.TestCase):
    def ctx(self,regime,variant):return DecisionContext(regime,None,.8,calibrated_variant=variant)
    def mini(self):return MiniTokenInput(chain='solana',technical_entry_ready=True,technical_strength=90,utility_confidence=90,liquidity_quality=90,contract_risk_pass=True,exit_liquidity_pass=True,persistence_confirmed=True,gmgn_smart_money_score=80,gmgn_insider_risk=5,chain_leader_aligned=True)
    def test_risk_off_ignores_aggressive_calibration(self):
        self.assertEqual(decide_mini_long(self.mini(),self.ctx('RISK_OFF','aggressive')).variant,'conservative')
    def test_neutral_ignores_aggressive_calibration(self):
        self.assertEqual(decide_mini_long(self.mini(),self.ctx('NEUTRAL','aggressive')).variant,'balanced')
    def test_risk_on_can_use_balanced_calibration(self):
        self.assertEqual(decide_mini_long(self.mini(),self.ctx('RISK_ON','balanced')).variant,'balanced')

class UtilityScopeTests(unittest.TestCase):
    def test_smart_money_cannot_replace_real_utility(self):
        ctx=DecisionContext("RISK_ON",None,.8)
        x=MiniTokenInput(chain="solana",technical_entry_ready=True,technical_strength=95,utility_confidence=25,
            liquidity_quality=95,contract_risk_pass=True,exit_liquidity_pass=True,persistence_confirmed=True,
            gmgn_smart_money_score=100,gmgn_insider_risk=0,chain_leader_aligned=True)
        d=decide_mini_long(x,ctx);self.assertEqual(d.action,"WATCH");self.assertIn("REAL_UTILITY_NOT_CONFIRMED",d.reasons)

class MemeScopeTests(unittest.TestCase):
    def ctx(self,regime='RISK_ON'):
        return DecisionContext(regime,None,.85)
    def meme(self,**kw):
        base=dict(chain='solana',technical_entry_ready=True,technical_strength=92,liquidity_quality=90,
                  exit_liquidity_pass=True,persistence_confirmed=True,gmgn_smart_money_score=90,
                  gmgn_insider_risk=5,gmgn_top10_pct=25,gmgn_sighting_streak=3,
                  chain_leader_aligned=True,contract_risk_pass=True)
        base.update(kw);return MemeTokenInput(**base)
    def test_meme_does_not_require_utility(self):
        d=decide_meme_long(self.meme(),self.ctx())
        self.assertEqual(d.action,'OPEN_LONG_PAPER');self.assertEqual(d.metadata['asset_scope'],'MEME')
    def test_meme_insider_hard_block(self):
        self.assertEqual(decide_meme_long(self.meme(gmgn_insider_risk=80),self.ctx()).action,'BLOCK_NEW_ENTRY')
    def test_meme_top10_hard_block(self):
        self.assertEqual(decide_meme_long(self.meme(gmgn_top10_pct=80),self.ctx()).action,'BLOCK_NEW_ENTRY')
    def test_meme_shock_halts(self):
        self.assertEqual(decide_meme_long(self.meme(),self.ctx('SHOCK')).variant,'halt')
