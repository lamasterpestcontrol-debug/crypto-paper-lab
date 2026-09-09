crypto-paper-lab strategy v0.8 upload package

Upload these 7 files to the repository root and replace same-name files where applicable:
- strategy_v08.py (new)
- test_strategy_v08.py (new)
- stock_style_shadow.py (new)
- test_stock_style_shadow.py (new)
- launcher.py (replace)
- test_launcher.py (replace)
- Dockerfile.discovery (replace)

Purpose:
- Add stock-market style technical-analysis research layer to crypto paper system.
- Keep existing control paper strategy unchanged.
- Add a fourth shadow worker that records EMA/RSI/ATR/volume/trend diagnostics only.
- No real trading, no wallet APIs, no change to old position ledger.

Important data labels:
- Early-token bars are snapshot-derived close proxies, NOT true exchange OHLC candles.
- volume_h1 is a rolling-volume proxy, NOT per-candle volume.
- Therefore v0.8 signals remain research/shadow-only until true intraday K-line feed is validated.

Initial parameter variants:
Conservative: +20% sell 25%, +40% sell 25%, +65% sell 20%, runner 30%.
Balanced: +25% sell 20%, +50% sell 25%, +75% sell 25%, runner 30%.
Aggressive: +30% sell 15%, +60% sell 20%, +100% sell 25%, runner 40%.
All variants use volatility-aware hard/trailing stops and prohibit averaging down.

Local new-module tests: 13 passed.
