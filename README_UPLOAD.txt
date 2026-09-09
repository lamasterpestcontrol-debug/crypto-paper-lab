V12 MEME public-routing fix — PAPER ONLY

Upload ONLY these 2 code files to the repository root and replace the same-name files:
1. decision_worker.py
2. test_decision_worker.py

Repository:
lamasterpestcontrol-debug/crypto-paper-lab

What this fixes:
- Obvious public meme candidates such as WOOF / WENPEPE / *INU can enter the existing MEME research path even when GMGN_API_KEY is absent.
- Utility-new-token classification still wins first when real-utility evidence is strong.
- Boundary safety prevents false substring matches such as CATALOG -> cat or MINUTE -> inu.
- This changes classification/routing only. It does NOT lower entry thresholds or bypass true-OHLC, liquidity, persistence, insider, concentration, contract-risk, market-regime, or shock gates.
- PAPER ONLY. No wallet/order API added.

Local validation performed:
- Python compile: OK
- Isolated token_scope routing validation: OK
- 4 new targeted routing tests: 4/4 OK

After upload, Railway should auto-deploy. Cloud validation must still pass before this is called deployed/working.
