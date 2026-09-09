V11 Railway pre-deploy test isolation fix

Upload ONLY test_discovery.py to the root of:
lamasterpestcontrol-debug/crypto-paper-lab

Replace the existing test_discovery.py.
Do not delete or change any other file.

What this changes:
- Isolates the malformed-feed unit test from Railway's production volume guard.
- Does NOT weaken or remove the production persistent-volume safety check.
- Verified under simulated Railway environment.
- Full v11 suite: 257 tests passed, 0 failed.
