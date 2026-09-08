"""Deterministic engineering tests. Artificial prices are NOT performance evidence."""
import base64
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from paperlab import App, Config, D, Ledger, Market, PAIRS, make_handler, parse_market, persistent_ready, public_get

NOW = 1_800_000_123
BAR = (NOW // 900 - 1) * 900
PASS = 'test-only-password-not-for-deployment'


def market(symbol='BTC', bid='100', ask='100.1', now=NOW, up=True):
    prices = tuple(D(80 + i if up else 120 - i) for i in range(21))
    return Market(symbol, D(bid), D(ask), now - 1, (int(now) // 900 - 1) * 900, prices)


def snapshot(now=NOW, up=True):
    return {s: market(s, now=now, up=up) for s in PAIRS}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'ledger.sqlite3'
        self.cfg = Config()
        self.ledger = Ledger(self.path, self.cfg)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_initial_capital_and_no_trades(self):
        s = self.ledger.status()
        self.assertEqual(D(s['equity']), D(10000))
        self.assertEqual(s['trade_events'], 0)

    def test_buy_records_fees_and_conservative_equity(self):
        self.ledger.process(snapshot(), NOW)
        s = self.ledger.status()
        self.assertEqual(s['trade_events'], 3)
        self.assertEqual(len(s['positions']), 3)
        self.assertGreaterEqual(D(s['cash']), D(7000))
        self.assertLess(D(s['equity']), D(10000))
        for t in s['recent_trades']:
            self.assertEqual(D(t['fee']), D(t['qty']) * D(t['fill']) * D(self.cfg.fee))
            self.assertGreater(D(t['fill']), D('100.1'))

    def test_same_bar_is_idempotent(self):
        self.ledger.process(snapshot(), NOW)
        self.ledger.process(snapshot(), NOW + 1)
        self.assertEqual(self.ledger.status()['trade_events'], 3)

    def test_stop_loss_and_net_realized_pnl(self):
        self.ledger.process(snapshot(), NOW)
        previous = self.ledger.status()['positions']['BTC']
        prices = snapshot()
        prices['BTC'] = market(bid='95', ask='95.1')
        decisions = self.ledger.process(prices, NOW + 1)
        self.assertEqual(decisions['BTC'], 'STOP_LOSS')
        trade = self.ledger.status()['recent_trades'][0]
        self.assertEqual(trade['side'], 'SELL')
        self.assertEqual(D(trade['pnl']), D(trade['qty']) * D(trade['fill']) - D(trade['fee']) - D(previous['cost']))
        self.assertLess(D(trade['pnl']), D(0))

    def test_no_same_bar_reentry_after_stop(self):
        self.test_stop_loss_and_net_realized_pnl()
        self.ledger.process(snapshot(), NOW + 2)
        s = self.ledger.status()
        self.assertNotIn('BTC', s['positions'])
        self.assertEqual(s['trade_events'], 4)

    def test_take_profit(self):
        self.ledger.process(snapshot(), NOW)
        prices = snapshot()
        prices['BTC'] = market(bid='110', ask='110.1')
        self.assertEqual(self.ledger.process(prices, NOW + 1)['BTC'], 'TAKE_PROFIT')
        self.assertEqual(self.ledger.status()['wins'], 1)

    def test_new_closed_bar_trend_exit(self):
        self.ledger.process(snapshot(), NOW)
        self.assertEqual(self.ledger.process(snapshot(NOW + 900, up=False), NOW + 900)['BTC'], 'TREND_EXIT')
        self.assertEqual(len(self.ledger.status()['positions']), 0)

    def test_no_buy_without_signal(self):
        self.ledger.process(snapshot(up=False), NOW)
        self.assertEqual(self.ledger.status()['trade_events'], 0)

    def test_stale_quote_rejected_without_order(self):
        with self.assertRaises(ValueError):
            self.ledger.process(snapshot(), NOW + 121)
        self.assertEqual(self.ledger.status()['trade_events'], 0)

    def test_incomplete_batch_rejected_without_order(self):
        prices = snapshot()
        del prices['ETH']
        with self.assertRaises(ValueError):
            self.ledger.process(prices, NOW)
        self.assertEqual(self.ledger.status()['trade_events'], 0)

    def test_symbol_mismatch_rejected(self):
        prices = snapshot()
        prices['ETH'] = market('BTC')
        with self.assertRaises(ValueError):
            self.ledger.process(prices, NOW)

    def test_restart_preserves_ledger_and_idempotency(self):
        self.ledger.process(snapshot(), NOW)
        before = self.ledger.status()['cash']
        self.ledger.close()
        self.ledger = Ledger(self.path, self.cfg)
        self.ledger.process(snapshot(), NOW + 1)
        self.assertEqual(self.ledger.status()['cash'], before)
        self.assertEqual(self.ledger.status()['trade_events'], 3)

    def test_config_change_refuses_silent_history_reset(self):
        with self.assertRaises(ValueError):
            Ledger(self.path, replace(self.cfg, fee='0.005'))
        self.assertEqual(self.ledger.status()['initial_cash'], '10000')

    def test_transaction_rolls_back_partial_orders(self):
        original = self.ledger._trade
        calls = []
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('synthetic interruption')
        with patch.object(self.ledger, '_trade', side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.ledger.process(snapshot(), NOW)
        s = self.ledger.status()
        self.assertEqual(s['trade_events'], 0)
        self.assertEqual(s['positions'], {})
        self.assertEqual(D(s['cash']), D(10000))

    def test_drawdown_halt_persists_across_restart(self):
        self.ledger.process(snapshot(), NOW)
        prices = {s: market(s, bid='50', ask='50.1') for s in PAIRS}
        self.ledger.process(prices, NOW + 1)
        self.assertTrue(self.ledger.status()['halted'])
        self.assertEqual(self.ledger.status()['positions'], {})
        self.ledger.close()
        self.ledger = Ledger(self.path, self.cfg)
        self.ledger.process(snapshot(NOW + 900), NOW + 900)
        self.assertEqual(self.ledger.status()['positions'], {})

    def test_csv_has_all_trade_rows(self):
        self.ledger.process(snapshot(), NOW)
        text = self.ledger.trades_csv().decode('utf-8-sig')
        self.assertEqual(len(text.splitlines()), 4)
        self.assertIn('BTC,BUY', text)

    def test_error_is_logged_without_changing_cash(self):
        self.ledger.error(NOW, 'synthetic outage')
        s = self.ledger.status()
        self.assertEqual(s['errors'], 1)
        self.assertEqual(s['last_scan']['status'], 'PAUSED')
        self.assertEqual(D(s['cash']), D(10000))
        self.assertIsNone(s['last_ok_ts'])

    def test_benchmark_includes_same_cost_model(self):
        self.ledger.process(snapshot(up=False), NOW)
        s = self.ledger.status()
        self.assertLess(D(s['benchmark_equity']), D(10000))
        self.assertEqual(D(s['equity']), D(10000))

    def test_candle_time_cannot_move_backwards(self):
        self.ledger.process(snapshot(), NOW)
        with self.assertRaises(ValueError):
            self.ledger.process(snapshot(NOW - 900), NOW - 900)
        self.assertEqual(self.ledger.status()['trade_events'], 3)


class DataTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        rows = []
        for i in range(22):
            price = 80 + i
            rows.append([BAR - (20-i)*900, str(price), str(price+1), str(price-1), str(price), str(price), '1', 1])
        self.ohlc = {'XXBTZUSD': rows, 'last': BAR + 900}
        self.spread = {'XXBTZUSD': [[NOW-1, '100', '100.1']], 'last': NOW}

    def test_uncommitted_candle_is_excluded(self):
        self.ohlc['XXBTZUSD'][-1][4] = '999999'
        m = parse_market('BTC', self.ohlc, self.spread, NOW, self.cfg)
        self.assertEqual(m.closes[-1], D(100))
        self.assertEqual(m.bar_ts, BAR)

    def test_latest_quote_wins_when_timestamp_is_tied(self):
        self.spread['XXBTZUSD'].append([NOW-1, '100.02', '100.12'])
        m = parse_market('BTC', self.ohlc, self.spread, NOW, self.cfg)
        self.assertEqual(m.bid, D('100.02'))

    def test_missing_candle_rejected(self):
        self.ohlc['XXBTZUSD'][10][0] += 1
        with self.assertRaises(ValueError):
            parse_market('BTC', self.ohlc, self.spread, NOW, self.cfg)

    def test_bad_ohlc_bounds_rejected(self):
        self.ohlc['XXBTZUSD'][10][2] = '1'
        with self.assertRaises(ValueError):
            parse_market('BTC', self.ohlc, self.spread, NOW, self.cfg)

    def test_no_recent_spread_rejected(self):
        self.spread['XXBTZUSD'] = []
        with self.assertRaises(ValueError):
            parse_market('BTC', self.ohlc, self.spread, NOW, self.cfg)

    def test_crossed_quote_rejected(self):
        with self.assertRaises(ValueError):
            market(bid='101', ask='100').validate(NOW, self.cfg)

    def test_nonfinite_price_rejected(self):
        with self.assertRaises(ValueError):
            market(bid='NaN').validate(NOW, self.cfg)

    def test_future_quote_rejected(self):
        with self.assertRaises(ValueError):
            replace(market(), quote_ts=NOW+60).validate(NOW, self.cfg)

    def test_unclosed_candle_rejected(self):
        with self.assertRaises(ValueError):
            replace(market(), bar_ts=BAR+900).validate(NOW, self.cfg)

    def test_live_order_endpoints_prohibited(self):
        with self.assertRaises(ValueError):
            public_get('AddOrder')

    def test_invalid_config_rejected(self):
        with self.assertRaises(ValueError):
            Config(initial_cash='NaN')
        with self.assertRaises(ValueError):
            Config(allocation='0.5')

    def test_directory_alone_is_not_persistent_railway_storage(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'RAILWAY_ENVIRONMENT_ID':'test', 'RAILWAY_VOLUME_MOUNT_PATH':temp}):
            self.assertFalse(persistent_ready(Path(temp)))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config()
        self.ledger = Ledger(Path(self.tmp.name) / 'ledger.sqlite3', self.cfg)
        self.app = App(self.ledger, self.cfg, Path(self.tmp.name), PASS)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.root = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.ledger.close()
        self.tmp.cleanup()

    def request(self, path, auth=False, method='GET'):
        headers = {}
        if auth:
            headers['Authorization'] = 'Basic ' + base64.b64encode(('viewer:'+PASS).encode()).decode()
        req = urllib.request.Request(self.root+path, headers=headers, method=method)
        return urllib.request.urlopen(req, timeout=3)

    def test_health_identifies_actual_application(self):
        with self.request('/healthz') as r:
            body = json.load(r)
        self.assertEqual(body['mode'], 'PAPER_ONLY')
        self.assertIn('paper-baseline', body['application'])

    def test_not_ready_before_successful_scan(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/readyz')
        self.assertEqual(caught.exception.code, 503)

    def test_dashboard_denies_anonymous_request(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/api/status')
        self.assertEqual(caught.exception.code, 401)

    def test_dashboard_authenticated(self):
        with self.request('/api/status', auth=True) as r:
            self.assertEqual(json.load(r)['cash'], '10000')

    def test_no_order_mutation_route(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request('/orders', auth=True, method='POST')
        self.assertEqual(caught.exception.code, 501)

    def test_valid_scan_does_not_mask_subsequent_failure(self):
        self.app.loop_alive = True
        self.app.storage_ready = True
        with patch('paperlab.time.time', return_value=NOW):
            self.ledger.process(snapshot(), NOW)
            self.assertTrue(self.app.snapshot()['ready'])
            self.ledger.error(NOW+1, 'synthetic outage')
            self.assertFalse(self.app.snapshot()['ready'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
