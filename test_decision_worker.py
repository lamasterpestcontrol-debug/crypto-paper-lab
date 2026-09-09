import json,tempfile,time,unittest
from pathlib import Path
import decision_worker as d

class T(unittest.TestCase):
    def seed(self,db,now):
        db.execute("CREATE TABLE candidates(chain TEXT,address TEXT,score REAL,evidence TEXT)")
        db.execute("INSERT INTO candidates VALUES('solana','abc',9,'profile_description;utility_terms:ai;website')")
        db.execute("""CREATE TABLE strategy_ab_observations(id INTEGER PRIMARY KEY,ts REAL,chain TEXT,address TEXT,liquidity REAL,persistence_confirmed INTEGER)""")
        db.execute("INSERT INTO strategy_ab_observations(ts,chain,address,liquidity,persistence_confirmed) VALUES(?,?,?,?,?)",(now,'solana','abc',100000,1))
        db.execute("""CREATE TABLE strategy_v08_observations(id INTEGER PRIMARY KEY,ts REAL,chain TEXT,address TEXT,symbol TEXT,entry_ready INTEGER,bar_source TEXT,rsi14 REAL,volume_ratio REAL,raw_json TEXT)""")
        db.execute("INSERT INTO strategy_v08_observations VALUES(1,?,?,?,?,?,?,?,?,?)",(now,'solana','abc','ABC',1,d.TRUE_BAR_SOURCE,60,2,json.dumps({'bullish_structure':True})))
        db.commit()
    def test_unknown_gmgn_is_not_zero_risk_credit(self):
        with tempfile.TemporaryDirectory() as td:
            db=d.dbopen(Path(td)/'x.db');now=1700000000;self.seed(db,now)
            ctx=d.DecisionContext('NEUTRAL',None,.8);n,o=d.process_mini(db,ctx,None,now)
            row=db.execute('select reasons_json,metadata_json from bot_decisions').fetchone();self.assertIn('GMGN_SMART_MONEY_UNAVAILABLE',row[0]);self.assertIn('UNAVAILABLE',row[1]);db.close()
    def test_gmgn_high_insider_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            db=d.dbopen(Path(td)/'x.db');now=1700000000;self.seed(db,now)
            db.execute("insert into token_intel_signals(ts,chain,address,source,smart_money_score,insider_risk,contract_risk_pass,independent_catalyst,raw_json) values(?,?,?,?,?,?,?,?,?)",(now,'solana','abc','GMGN',80,90,1,0,'{}'));db.commit()
            d.process_mini(db,d.DecisionContext('RISK_ON',None,.9),None,now)
            self.assertEqual(db.execute('select action from bot_decisions').fetchone()[0],'BLOCK_NEW_ENTRY');db.close()
    def test_context_defaults_safe(self):
        with tempfile.TemporaryDirectory() as td:
            db=d.dbopen(Path(td)/'x.db');ctx,rg=d.context(db,1700000000);self.assertEqual(ctx.regime,'NEUTRAL');self.assertIsNone(rg);db.close()
    def test_context_uses_scope_specific_safe_calibration(self):
        with tempfile.TemporaryDirectory() as td:
            db=d.dbopen(Path(td)/'x.db');now=1700000000
            db.execute("CREATE TABLE strategy_calibration(id INTEGER PRIMARY KEY,ts REAL,regime TEXT,asset_scope TEXT,recommended_variant TEXT,active INTEGER,reason TEXT,min_trades INTEGER,stats_json TEXT,worker_version TEXT)")
            db.execute("INSERT INTO strategy_calibration(ts,regime,asset_scope,recommended_variant,active,reason,min_trades,stats_json,worker_version) VALUES(?,?,?,?,?,?,?,?,?)",(now,'NEUTRAL','MEME','conservative',1,'ok',20,'{}','test'));db.commit()
            ctx,_=d.context(db,now);self.assertIsNone(ctx.calibrated_variant)
            scoped=d.scope_context(db,ctx,'MEME',now);self.assertEqual(scoped.calibrated_variant,'conservative')
            other=d.scope_context(db,ctx,'UTILITY_NEW_TOKEN',now);self.assertIsNone(other.calibrated_variant);db.close()
    def test_technical_strength(self):
        class R(dict):
            __getattr__=dict.get
        r={'raw_json':json.dumps({'bullish_structure':True,'breakout20':True}),'rsi14':60,'volume_ratio':2}
        self.assertGreater(d.technical_strength(r),70)

class GMGNOnlyInputTests(unittest.TestCase):
    def test_gmgn_only_market_input_requires_three_sightings(self):
        with tempfile.TemporaryDirectory() as td:
            db=d.dbopen(Path(td)/"x.db");now=1000
            db.execute("CREATE TABLE strategy_ab_observations(id INTEGER PRIMARY KEY,ts REAL,chain TEXT,address TEXT,liquidity REAL,persistence_confirmed INTEGER)")
            db.execute("CREATE TABLE gmgn_discovery_candidates(chain TEXT,address TEXT,liquidity REAL,last_seen REAL,sighting_streak INTEGER)")
            db.execute("INSERT INTO gmgn_discovery_candidates VALUES(?,?,?,?,?)",("solana","abc",50000,now,2));db.commit()
            self.assertFalse(d.market_inputs(db,"solana","abc",now)["persistence_confirmed"])
            db.execute("UPDATE gmgn_discovery_candidates SET sighting_streak=3");db.commit()
            self.assertTrue(d.market_inputs(db,"solana","abc",now)["persistence_confirmed"]);db.close()

class ExplicitScopeTests(unittest.TestCase):
    def base_tables(self,db,now,address='meme1',evidence='meme;profile_description',score=1):
        db.execute("CREATE TABLE candidates(chain TEXT,address TEXT,score REAL,evidence TEXT)")
        db.execute("INSERT INTO candidates VALUES(?,?,?,?)",('solana',address,score,evidence))
        db.execute("CREATE TABLE strategy_ab_observations(id INTEGER PRIMARY KEY,ts REAL,chain TEXT,address TEXT,liquidity REAL,persistence_confirmed INTEGER)")
        db.execute("INSERT INTO strategy_ab_observations(ts,chain,address,liquidity,persistence_confirmed) VALUES(?,?,?,?,1)",(now,'solana',address,120000))
        db.execute("CREATE TABLE strategy_v08_observations(id INTEGER PRIMARY KEY,ts REAL,chain TEXT,address TEXT,symbol TEXT,entry_ready INTEGER,bar_source TEXT,rsi14 REAL,volume_ratio REAL,raw_json TEXT)")
        db.execute("INSERT INTO strategy_v08_observations VALUES(1,?,?,?,?,?,?,?,?,?)",(now,'solana',address,'MEME',1,d.TRUE_BAR_SOURCE,60,2,json.dumps({'bullish_structure':True,'breakout20':True})))
        db.execute("CREATE TABLE gmgn_discovery_candidates(chain TEXT,address TEXT,last_seen REAL,sighting_streak INTEGER,liquidity REAL)")
        db.execute("INSERT INTO gmgn_discovery_candidates VALUES(?,?,?,?,?)",('solana',address,now,3,120000))
        db.execute("INSERT INTO token_intel_signals(ts,chain,address,source,smart_money_score,insider_risk,top10_pct,contract_risk_pass,independent_catalyst,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?)",(now,'solana',address,'GMGN',95,5,.25,1,0,'{}'))
        db.commit()
    def test_meme_scope_does_not_require_utility(self):
        with tempfile.TemporaryDirectory() as td:
            db=d.dbopen(Path(td)/'x.db');now=1700000000;self.base_tables(db,now)
            counts=d.process_tokens(db,d.DecisionContext('RISK_ON',None,.9),None,now)
            row=db.execute("SELECT scope,action FROM bot_decisions").fetchone()
            self.assertEqual(row['scope'],'MEME');self.assertEqual(row['action'],'OPEN_LONG_PAPER');self.assertEqual(counts['MEME'],1);db.close()
    def test_utility_scope_still_requires_utility(self):
        with tempfile.TemporaryDirectory() as td:
            db=d.dbopen(Path(td)/'x.db');now=1700000000;self.base_tables(db,now,address='util1',evidence='profile_description;utility_terms:ai;website',score=9)
            counts=d.process_tokens(db,d.DecisionContext('RISK_ON',None,.9),None,now)
            row=db.execute("SELECT scope FROM bot_decisions").fetchone();self.assertEqual(row['scope'],'UTILITY_NEW_TOKEN');self.assertEqual(counts['UTILITY_NEW_TOKEN'],1);db.close()

class PublicMemeRoutingTests(unittest.TestCase):
    def make_db(self,td,rows):
        db=d.dbopen(Path(td)/'x.db')
        db.execute("CREATE TABLE candidates(chain TEXT,address TEXT,symbol TEXT,name TEXT,score REAL,evidence TEXT)")
        db.executemany("INSERT INTO candidates VALUES(?,?,?,?,?,?)",rows);db.commit();return db
    def test_obvious_public_meme_symbols_route_without_gmgn(self):
        with tempfile.TemporaryDirectory() as td:
            db=self.make_db(td,[
                ('solana','woof','WOOF','Woof',1,''),
                ('solana','pepe','WENPEPE','Wen Pepe',1,''),
                ('solana','inu','INCOGINU','Incoginu',1,''),
            ])
            for address in ('woof','pepe','inu'):
                self.assertEqual(d.token_scope(db,'solana',address,1700000000)[0],'MEME')
            db.close()
    def test_boundary_safety_catalog_is_not_cat_meme(self):
        with tempfile.TemporaryDirectory() as td:
            db=self.make_db(td,[('solana','catalog','CATALOG','Catalog Protocol',1,'')])
            self.assertEqual(d.token_scope(db,'solana','catalog',1700000000)[0],'UNCLASSIFIED');db.close()
    def test_inu_requires_suffix_not_arbitrary_substring(self):
        self.assertTrue(d._meme_hint('INCOGINU','',''))
        self.assertFalse(d._meme_hint('MINUTE','Minute Network',''))
    def test_utility_scope_wins_even_when_symbol_looks_meme(self):
        with tempfile.TemporaryDirectory() as td:
            db=self.make_db(td,[('solana','utilitywoof','WOOF','Utility Woof',9,'profile_description;utility_terms:ai;website')])
            self.assertEqual(d.token_scope(db,'solana','utilitywoof',1700000000)[0],'UTILITY_NEW_TOKEN');db.close()

if __name__=='__main__':
    unittest.main()
