import tempfile, unittest
from pathlib import Path
import v08_paper as p

NOW=1_700_000_000.0
CHAIN='solana';ADDR='TokenAddress111111111111111111111111';SOURCE='GECKOTERMINAL_PUBLIC_ONCHAIN'

class T(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.db=p.dbopen(Path(self.t.name)/'x.sqlite3');self.addCleanup(self.db.close)
        self.db.executescript('''
        CREATE TABLE live_ohlcv(chain TEXT,address TEXT,network TEXT,pool_address TEXT,timeframe TEXT,ts INTEGER,open REAL,high REAL,low REAL,close REAL,volume REAL,source TEXT,PRIMARY KEY(chain,address,timeframe,ts));
        CREATE TABLE strategy_ab_observations(id INTEGER PRIMARY KEY,ts REAL,chain TEXT,address TEXT,liquidity REAL);
        CREATE TABLE strategy_v08_observations(id INTEGER PRIMARY KEY,ts REAL,chain TEXT,address TEXT,symbol TEXT,entry_ready INTEGER,reason TEXT,ema_fast REAL,ema_slow REAL,rsi14 REAL,atr_pct REAL,bar_source TEXT,recommended_variant TEXT);
        CREATE TABLE positions(id INTEGER PRIMARY KEY,status TEXT);
        CREATE TABLE bot_decisions(id INTEGER PRIMARY KEY,ts REAL,scope TEXT,chain TEXT,address TEXT,symbol TEXT,action TEXT,variant TEXT,score REAL,reasons_json TEXT,metadata_json TEXT);
        CREATE TABLE decision_context_states(id INTEGER PRIMARY KEY,ts REAL,regime TEXT,external_shock REAL,cross_asset_shock INTEGER);
        INSERT INTO positions VALUES(1,'OPEN');
        ''');self.db.commit()
    def seed(self,price=1.0,liq=100000,ready=1,variant='balanced',atr=.02,signal_source=p.TRUE_BAR_SOURCE,ts=NOW):
        self.db.execute("INSERT OR REPLACE INTO live_ohlcv VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(CHAIN,ADDR,'solana','pool','minute',int(ts),price,price,price,price,1000,SOURCE))
        self.db.execute("INSERT INTO strategy_ab_observations(ts,chain,address,liquidity) VALUES(?,?,?,?)",(ts,CHAIN,ADDR,liq))
        cur=self.db.execute("INSERT INTO strategy_v08_observations(ts,chain,address,symbol,entry_ready,reason,ema_fast,ema_slow,rsi14,atr_pct,bar_source,recommended_variant) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(ts,CHAIN,ADDR,'TOK',ready,'TEST',price*.98,price*.95,60,atr,signal_source,variant));self.db.commit();return cur.lastrowid
    def signal(self,sid):return self.db.execute('SELECT * FROM strategy_v08_observations WHERE id=?',(sid,)).fetchone()
    def approve_adaptive(self,sid,variant='balanced',ts=NOW):
        import json
        self.db.execute("INSERT INTO bot_decisions(ts,scope,chain,address,symbol,action,variant,score,reasons_json,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts,'MEME',CHAIN,ADDR,'TOK','OPEN_LONG_PAPER',variant,80,'[]',json.dumps({'signal_id':sid})))
        self.db.commit()
    def context(self,regime='NEUTRAL',shock=0.0,cross=0,ts=NOW):
        self.db.execute("INSERT INTO decision_context_states(ts,regime,external_shock,cross_asset_shock) VALUES(?,?,?,?)",(ts,regime,shock,cross));self.db.commit()
    def pos(self,variant):return self.db.execute('SELECT * FROM v08_paper_positions WHERE variant=? ORDER BY id DESC LIMIT 1',(variant,)).fetchone()
    def update(self,price,variant='balanced',ready=1,atr=.02,ts=NOW+60,liq=100000):return self.seed(price,liq,ready,variant,atr,p.TRUE_BAR_SOURCE,ts)

    def test_true_ohlcv_signal_opens_three_fixed_variants_without_adaptive_approval(self):
        sid=self.seed();out=p.cycle(self.db,now=NOW)
        self.assertEqual(out['opened'],3);self.assertEqual(self.db.execute("SELECT COUNT(*) FROM v08_paper_positions").fetchone()[0],3)
        self.assertIsNone(self.pos('adaptive'))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM positions").fetchone()[0],1)

    def test_adaptive_opens_only_after_decision_engine_approval(self):
        sid=self.seed();self.approve_adaptive(sid,'aggressive')
        out=p.cycle(self.db,now=NOW)
        self.assertEqual(out['opened'],4)
        row=self.pos('adaptive');self.assertIsNotNone(row);self.assertEqual(row['selected_at_entry'],'aggressive');self.assertEqual(row['asset_scope'],'MEME')

    def test_proxy_signal_cannot_open_trade(self):
        sid=self.seed(signal_source='MINUTE_SNAPSHOT_CLOSE_PROXY_NOT_TRUE_OHLC')
        self.assertEqual(p.cycle(self.db,now=NOW)['opened'],0)

    def test_stale_true_price_or_liquidity_fails_closed(self):
        sid=self.seed(ts=NOW-181);self.assertEqual(p.cycle(self.db,now=NOW)['opened'],0)

    def test_low_liquidity_entry_is_blocked_and_not_retried(self):
        sid=self.seed(liq=1000)
        self.assertEqual(p.cycle(self.db,now=NOW)['opened'],0)
        # use rows record signal consumption for each variant; next cycle does not keep retrying bad fills
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM v08_paper_signal_uses').fetchone()[0],3);self.assertEqual(p.scorecard(self.db)['balanced']['blocked_entries'],1);self.assertEqual(p.scorecard(self.db)['adaptive']['blocked_entries'],0)

    def test_costs_make_roundtrip_net_negative_at_flat_price(self):
        sid=self.seed();p.open_from_signal(self.db,self.signal(sid),'balanced',NOW)
        self.update(1.0,variant='halt',ts=NOW+60)
        row=self.pos('balanced');market=p.latest_true_market(self.db,CHAIN,ADDR,NOW+60);sig=p.latest_signal(self.db,CHAIN,ADDR,NOW+60)
        self.assertTrue(p._sell(self.db,row,sig,market,float(row['qty']),'TEST_CLOSE',NOW+60,True))
        row=self.pos('balanced');self.assertLess(row['net_pnl'],0)
        acts=self.db.execute('SELECT SUM(fee_usd),SUM(gas_usd) FROM v08_paper_actions WHERE position_id=?',(row['id'],)).fetchone();self.assertGreater(acts[0],0);self.assertGreater(acts[1],0)

    def test_partial_take_profit_records_sell_part(self):
        sid=self.seed();p.open_from_signal(self.db,self.signal(sid),'conservative',NOW)
        self.update(1.25,variant='conservative',ts=NOW+60)
        row=self.pos('conservative');p.manage_one(self.db,row,NOW+60)
        self.assertTrue(self.db.execute("SELECT 1 FROM v08_paper_actions WHERE position_id=? AND action='SELL_PART' AND reason='TAKE_20'",(row['id'],)).fetchone())
        self.assertGreater(self.db.execute('SELECT cash_inflow FROM v08_paper_positions WHERE id=?',(row['id'],)).fetchone()[0],0)

    def test_never_average_down(self):
        sid=self.seed();p.open_from_signal(self.db,self.signal(sid),'balanced',NOW);before=self.pos('balanced')['deployed_usd']
        self.update(.90,variant='balanced',ready=1,ts=NOW+60)
        p.manage_one(self.db,self.pos('balanced'),NOW+60)
        self.assertEqual(self.pos('balanced')['deployed_usd'],before);self.assertEqual(self.pos('balanced')['add_count'],0)

    def test_profitable_pullback_can_add_but_never_exceeds_plan(self):
        sid=self.seed();p.open_from_signal(self.db,self.signal(sid),'balanced',NOW)
        # create a peak, then pull back 8% while still >12% above average; signal remains reconfirmed
        self.update(1.35,variant='balanced',ready=1,atr=.05,ts=NOW+60);p.manage_one(self.db,self.pos('balanced'),NOW+60)
        self.update(1.24,variant='balanced',ready=1,atr=.05,ts=NOW+120);p.manage_one(self.db,self.pos('balanced'),NOW+120)
        row=self.pos('balanced');self.assertEqual(row['add_count'],1);self.assertLessEqual(row['deployed_usd'],row['planned_usd'])

    def test_adaptive_shock_halts_entry(self):
        sid=self.seed(variant='halt');self.assertIsNone(p.open_from_signal(self.db,self.signal(sid),'adaptive',NOW))

    def test_adaptive_open_position_exits_on_global_decision_shock(self):
        sid=self.seed(variant='aggressive');p.open_from_signal(self.db,self.signal(sid),'adaptive',NOW,selected_variant='aggressive')
        self.update(1.02,variant='aggressive',ts=NOW+60);self.context('SHOCK',ts=NOW+60)
        res=p.manage_one(self.db,self.pos('adaptive'),NOW+60)
        self.assertEqual(res,'CLOSED');self.assertEqual(self.pos('adaptive')['close_reason'],'GLOBAL_DECISION_SHOCK_EXIT')

    def test_exit_blocked_is_visible_when_liquidity_collapses(self):
        sid=self.seed();p.open_from_signal(self.db,self.signal(sid),'balanced',NOW)
        self.update(.60,variant='balanced',liq=100,ts=NOW+60)
        res=p.manage_one(self.db,self.pos('balanced'),NOW+60)
        self.assertEqual(res,'EXIT_BLOCKED');self.assertGreater(p.scorecard(self.db)['balanced']['blocked_exits'],0)

    def test_signal_not_reused_same_variant(self):
        sid=self.seed();sig=self.signal(sid);self.assertIsNotNone(p.open_from_signal(self.db,sig,'balanced',NOW));self.assertIsNone(p.open_from_signal(self.db,sig,'balanced',NOW))

    def test_scorecard_separates_variants(self):
        sid=self.seed();p.open_from_signal(self.db,self.signal(sid),'balanced',NOW)
        self.update(1.5,variant='balanced',ts=NOW+60);row=self.pos('balanced');m=p.latest_true_market(self.db,CHAIN,ADDR,NOW+60);s=p.latest_signal(self.db,CHAIN,ADDR,NOW+60);p._sell(self.db,row,s,m,float(row['qty']),'WIN',NOW+60,True)
        sc=p.scorecard(self.db);self.assertEqual(sc['balanced']['closed'],1);self.assertEqual(sc['conservative']['closed'],0);self.assertGreater(sc['balanced']['net_pnl'],0)

    def test_entry_regime_is_immutable_snapshot_for_calibration(self):
        self.context('RISK_ON',ts=NOW)
        sid=self.seed();pid=p.open_from_signal(self.db,self.signal(sid),'balanced',NOW)
        self.assertIsNotNone(pid);self.assertEqual(self.pos('balanced')['entry_regime'],'RISK_ON')
        self.context('RISK_OFF',ts=NOW+1)
        self.assertEqual(self.pos('balanced')['entry_regime'],'RISK_ON')

    def test_initial_capital_scales_by_variant(self):
        sid=self.seed();sig=self.signal(sid)
        for v in ('conservative','balanced','aggressive'):
            p.open_from_signal(self.db,sig,v,NOW)
        vals={v:self.pos(v)['deployed_usd'] for v in ('conservative','balanced','aggressive')}
        self.assertEqual(vals,{'conservative':35.0,'balanced':50.0,'aggressive':60.0})

if __name__=='__main__':unittest.main()
