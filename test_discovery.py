import tempfile, unittest
from pathlib import Path
from discovery import assess, best_pair, Store


def pair(**kw):
    p={"chainId":"base","pairAddress":"pair1","baseToken":{"address":"0x1234567890123456789012345678901234567890","symbol":"UTIL","name":"Utility"},"priceUsd":"0.01","marketCap":200000,"liquidity":{"usd":50000},"volume":{"h24":80000},"txns":{"h1":{"buys":20,"sells":10}},"pairCreatedAt":1_000_000,"info":{"websites":[{"url":"https://x"}],"socials":[{"platform":"x","handle":"u"}]},"url":"https://dex"}
    p.update(kw); return p

class Tests(unittest.TestCase):
    def test_best_pair_uses_liquidity(self):
        a=pair(); b=pair(pairAddress="pair2",liquidity={"usd":90000})
        self.assertEqual(best_pair([a,b],a["baseToken"]["address"])["pairAddress"],"pair2")
    def test_utility_candidate_eligible(self):
        now=1_000_000+24*3600*1000
        a=assess({"chainId":"base","tokenAddress":pair()["baseToken"]["address"],"description":"AI data infrastructure protocol for developers","links":[{"url":"https://x"}]},pair(),now)
        self.assertTrue(a.eligible); self.assertGreaterEqual(a.score,7)
    def test_meme_rejected(self):
        now=1_000_000+24*3600*1000
        a=assess({"description":"fun meme dog community coin with AI"},pair(),now)
        self.assertFalse(a.eligible)
    def test_short_utility_keywords_require_word_boundaries(self):
        now=1_000_000+24*3600*1000
        for desc in ("Claim rewards today", "A fair launch for everyone", "Join our daily celebration", "Rapidly growing community"):
            with self.subTest(desc=desc):
                a=assess({"description":desc,"links":[{"url":"https://x"}]},pair(),now)
                self.assertFalse(a.eligible)
                self.assertNotIn("utility_terms:ai",a.evidence)
                self.assertNotIn("utility_terms:api",a.evidence)

    def test_meme_keywords_require_word_boundaries(self):
        now=1_000_000+24*3600*1000
        a=assess({"description":"AI data developer catalog for enterprise","links":[{"url":"https://x"}]},pair(),now)
        self.assertTrue(a.eligible)
        self.assertNotIn("meme",a.evidence.lower())

    def test_multiword_utility_phrase_matches_across_punctuation(self):
        now=1_000_000+24*3600*1000
        a=assess({"description":"Real-world asset tokenization infrastructure","links":[{"url":"https://x"}]},pair(),now)
        self.assertTrue(a.eligible)
        self.assertIn("real world asset",a.evidence)
    def test_paper_position_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            s=Store(Path(d)/"x.db"); self.addCleanup(s.db.close); now=1_000_000+24*3600*1000
            a=assess({"description":"AI data infrastructure protocol","links":[{"url":"https://x"}]},pair(),now)
            self.assertTrue(s.maybe_open(a,now/1000))
            p2=pair(priceUsd="0.021")
            b=assess({"description":"AI data infrastructure protocol","links":[{"url":"https://x"}]},p2,now+1000)
            self.assertEqual(s.update_position(b,now/1000+1),"TAKE_+100%")
            snap=s.snapshot(); self.assertEqual(snap["closed"],1); self.assertGreater(snap["realized_pnl"],0)
            s.db.close()


class AccountingRegressionTests(unittest.TestCase):
    """Accounting fixtures only; no public data or real order APIs."""
    NOW = 1700000000.0

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'accounting.sqlite3')
        self.addCleanup(self.store.db.close)
        self.seq = 0

    def position(self, status='CLOSED', pnl=5.0, **kw):
        self.seq += 1
        record = dict(chain='solana', address=f'Token{self.seq}', symbol='TEST',
                      opened_at=self.NOW-1000+self.seq, closed_at=self.NOW-500+self.seq,
                      entry_price=10.0, exit_price=10.0+float(pnl)/10,
                      usd=100.0, qty=10.0, status=status, reason='SYNTHETIC', pnl=pnl)
        if status == 'OPEN': record.update(closed_at=None, exit_price=None, pnl=0.0)
        record.update(kw)
        keys = list(record)
        cur = self.store.db.execute('INSERT INTO positions('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',list(record.values()))
        self.store.db.commit()
        return cur.lastrowid

    def mark(self, pid, price=8.0, ts=None, liquidity=50000):
        r = self.store.db.execute('SELECT * FROM positions WHERE id=?',(pid,)).fetchone()
        self.store.db.execute('INSERT INTO candidates(chain,address,symbol,current_price,last_seen,liquidity) VALUES(?,?,?,?,?,?)',
                              (r['chain'],r['address'],r['symbol'],price,self.NOW if ts is None else ts,liquidity))
        self.store.db.commit()

    def report(self): return self.store.accounting_snapshot(now=self.NOW)

    def test_empty_ledger_is_not_proof_of_profitability(self):
        r=self.report();self.assertEqual(r['positions'],0)
        self.assertIsNone(r['gross_win_rate']);self.assertIsNone(r['realized_net_usd'])
        self.assertFalse(r['profitability_verified'])

    def test_all_250_records_count_not_last_100(self):
        for i in range(250): self.position(pnl=1.0)
        r=self.report();self.assertEqual((r['closed'],r['gross_wins']),(250,250))
        self.assertAlmostEqual(float(r['realized_gross_usd']),250.0)
        self.assertTrue(r['gross_ledger_reconciled'])
        s=self.store.snapshot();self.assertEqual((s['closed'],s['positions_total'],len(s['positions'])),(250,250,100))
        self.assertAlmostEqual(s['realized_pnl'],250.0)

    def test_large_old_loss_is_not_hidden_by_100_recent_wins(self):
        self.position(pnl=-90)
        for _ in range(100): self.position(pnl=0.5)
        r=self.report();self.assertEqual((r['gross_wins'],r['gross_losses']),(100,1))
        self.assertEqual(float(r['realized_gross_usd']),-40.0)
        self.assertAlmostEqual(r['gross_win_rate'],100/101)
        self.assertEqual(self.store.snapshot()['realized_pnl'],-40.0)

    def test_breakeven_is_not_a_win(self):
        self.position(pnl=0);r=self.report()
        self.assertEqual((r['gross_wins'],r['gross_losses'],r['gross_breakeven']),(0,0,1))

    def test_missing_costs_stay_unknown_not_zero(self):
        self.position();r=self.report()
        for k in ['fees_usd','slippage_usd','gas_usd','realized_net_usd','combined_net_usd']: self.assertIsNone(r[k])
        self.assertEqual(r['cost_accounting'],'MISSING_NOT_ZERO')

    def test_open_loss_offsets_closed_gain_in_combined_gross(self):
        self.position(pnl=10);pid=self.position(status='OPEN');self.mark(pid,price=8)
        r=self.report();self.assertEqual(float(r['realized_gross_usd']),10.0)
        self.assertEqual(float(r['unrealized_gross_usd']),-20.0)
        self.assertEqual(float(r['combined_gross_usd']),-10.0);self.assertIsNone(r['combined_net_usd'])

    def test_missing_open_mark_does_not_become_zero_loss(self):
        self.position(status='OPEN');r=self.report()
        self.assertIsNone(r['unrealized_gross_usd']);self.assertIsNone(r['combined_gross_usd'])
        self.assertIn('MISSING_OR_INVALID_OPEN_MARK',r['issue_counts'])

    def test_stale_mark_does_not_become_current_valuation(self):
        pid=self.position(status='OPEN');self.mark(pid,ts=self.NOW-181);r=self.report()
        self.assertIsNone(r['unrealized_gross_usd']);self.assertIn('STALE_OPEN_MARK',r['issue_counts'])

    def test_future_mark_is_rejected(self):
        pid=self.position(status='OPEN');self.mark(pid,ts=self.NOW+1);r=self.report()
        self.assertIsNone(r['unrealized_gross_usd']);self.assertIn('FUTURE_OR_PRE_ENTRY_MARK',r['issue_counts'])

    def test_pre_entry_mark_is_rejected(self):
        pid=self.position(status='OPEN');self.mark(pid,ts=self.NOW-2000)
        self.assertIn('FUTURE_OR_PRE_ENTRY_MARK',self.report()['issue_counts'])

    def test_zero_liquidity_does_not_justify_a_close_value(self):
        pid=self.position(status='OPEN');self.mark(pid,liquidity=0);r=self.report()
        self.assertIn('NO_OBSERVED_EXIT_LIQUIDITY',r['issue_counts']);self.assertIsNone(r['unrealized_gross_usd'])

    def test_partial_valuation_is_explicit(self):
        self.mark(self.position(status='OPEN'),price=12);self.position(status='OPEN');r=self.report()
        self.assertEqual(r['fresh_marked_open'],1)
        self.assertEqual(float(r['unrealized_gross_valid_subset_usd']),20.0)
        self.assertIsNone(r['unrealized_gross_usd'])

    def test_recomputed_pnl_mismatch_blocks_verified_gross(self):
        self.position(exit_price=9,pnl=50);r=self.report()
        self.assertFalse(r['gross_ledger_reconciled'])
        self.assertEqual(float(r['computed_realized_gross_partial_usd']),-10)
        self.assertIsNone(r['realized_gross_usd']);self.assertIsNone(r['gross_win_rate'])
        self.assertIn('REALIZED_PNL_MISMATCH',r['issue_counts'])

    def test_missing_pnl_is_not_assumed_breakeven(self):
        self.position(pnl=0,exit_price=11)
        self.store.db.execute('UPDATE positions SET pnl=NULL');self.store.db.commit();r=self.report()
        self.assertIsNone(r['realized_gross_usd']);self.assertIn('INVALID_CLOSED_LEDGER',r['issue_counts'])

    def test_entry_basis_mismatch_blocks_reconciliation(self):
        self.position(qty=11);self.assertIn('ENTRY_BASIS_MISMATCH',self.report()['issue_counts'])

    def test_negative_quantity_is_invalid(self):
        self.position(qty=-10);self.assertIsNone(self.report()['realized_gross_usd'])

    def test_nonfinite_and_text_numbers_are_invalid(self):
        for bad in [float('inf'),'NaN','invalid']: self.position(qty=bad)
        r=self.report();self.assertEqual(r['issue_counts']['INVALID_ENTRY_LEDGER'],3)
        self.assertIsNone(r['realized_gross_usd'])

    def test_invalid_status_is_visible(self):
        self.position(status='BROKEN');r=self.report();self.assertEqual(r['invalid_status'],1)
        self.assertFalse(r['gross_ledger_reconciled']);self.assertIsNone(r['combined_gross_usd'])

    def test_future_close_cannot_be_counted(self):
        self.position(closed_at=self.NOW+1);self.assertIsNone(self.report()['realized_gross_usd'])

    def test_close_before_open_cannot_be_counted(self):
        self.position(closed_at=self.NOW-5000);self.assertIsNone(self.report()['realized_gross_usd'])

    def test_open_position_with_exit_fields_is_not_counted_closed(self):
        pid=self.position(status='OPEN',exit_price=12);self.mark(pid);r=self.report()
        self.assertEqual(r['closed'],0);self.assertIsNone(r['unrealized_gross_usd'])

    def test_same_symbol_different_addresses_do_not_share_marks(self):
        p1=self.position(status='OPEN',symbol='SAME');self.position(status='OPEN',symbol='SAME')
        self.mark(p1);r=self.report();self.assertEqual(r['fresh_marked_open'],1)
        self.assertIsNone(r['unrealized_gross_usd'])

    def test_shadow_confirmations_are_not_executed_trades(self):
        import shadow_ab
        db=shadow_ab.dbopen(Path(self.temp.name)/'accounting.sqlite3');db.close()
        for i in range(3):
            self.store.db.execute('INSERT INTO strategy_ab_observations(ts,chain,address,persistence_confirmed,challenger_state) VALUES(?,?,?,?,?)',
                                 (self.NOW-i,'solana','Same',1,'ENTER'))
        self.store.db.commit();r=self.report()
        self.assertEqual(r['shadow_ab']['confirmed_observations'],3)
        self.assertIsNone(r['shadow_ab']['executed_trades']);self.assertIsNone(r['shadow_ab']['pnl_usd'])
        self.assertEqual(r['positions'],0)

    def test_read_only_report_does_not_change_data(self):
        import sqlite3
        self.position();before=self.store.db.total_changes
        forbidden={sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,
                   sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_ALTER_TABLE,sqlite3.SQLITE_DROP_TABLE}
        self.store.db.set_authorizer(lambda action,*args: sqlite3.SQLITE_DENY if action in forbidden else sqlite3.SQLITE_OK)
        try: self.report();self.store.snapshot();self.store.positions_csv_bytes()
        finally: self.store.db.set_authorizer(None)
        self.assertEqual(before,self.store.db.total_changes)

    def test_repeated_reports_do_not_double_count(self):
        self.position();self.assertEqual(self.report(),self.report())

    def test_csv_exports_more_than_100_positions(self):
        import csv,io
        for _ in range(125): self.position()
        rows=list(csv.reader(io.StringIO(self.store.positions_csv_bytes().decode('utf-8-sig'))))
        self.assertEqual(len(rows),126)

    def test_csv_market_text_cannot_be_formula(self):
        import csv,io
        self.position(symbol='=1+2')
        rows=list(csv.reader(io.StringIO(self.store.positions_csv_bytes().decode('utf-8-sig'))))
        self.assertEqual(rows[1][rows[0].index('symbol')],"'=1+2")

    def test_no_fabricated_equity_or_drawdown(self):
        self.position(pnl=10);r=self.report()
        self.assertIsNone(r['equity_return_pct']);self.assertIsNone(r['max_drawdown_pct'])

    def test_report_is_json_serializable_without_nonfinite_numbers(self):
        import json
        self.position();self.position(qty=float('inf'));json.dumps(self.report(),allow_nan=False)

    def test_nested_transaction_is_not_committed_by_report(self):
        self.store.db.execute('BEGIN')
        self.store.db.execute("INSERT INTO scans(ts,status,body) VALUES(1,'TEST','{}')")
        self.report();self.assertTrue(self.store.db.in_transaction);self.store.db.rollback()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM scans').fetchone()[0],0)

    def test_ui_does_not_describe_gross_as_net(self):
        import discovery
        self.assertIn('全历史已实现模拟毛盈亏',discovery.SCRIPT)
        self.assertIn('费用和滑点未记录，净利润未核实',discovery.SCRIPT)
        self.assertIn('esc(x[1](r))',discovery.SCRIPT)

    def test_http_endpoints_require_existing_dashboard_auth(self):
        import discovery,threading,urllib.request,urllib.error,base64,json
        from http.server import ThreadingHTTPServer
        self.position();app=discovery.App(self.store,Path(self.temp.name),'test-password-123456')
        server=ThreadingHTTPServer(('127.0.0.1',0),discovery.make_handler(app))
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            base=f'http://127.0.0.1:{server.server_address[1]}'
            for path in ['/api/scorecard','/positions.csv']:
                with self.assertRaises(urllib.error.HTTPError) as err: urllib.request.urlopen(base+path,timeout=2)
                self.assertEqual(err.exception.code,401);err.exception.close()
            token=base64.b64encode(b'viewer:test-password-123456').decode()
            request=urllib.request.Request(base+'/api/scorecard',headers={'Authorization':'Basic '+token})
            with urllib.request.urlopen(request,timeout=2) as response: r=json.load(response)
            self.assertEqual(r['positions'],1)
        finally: server.shutdown();server.server_close();worker.join(timeout=2)

if __name__=="__main__": unittest.main()

