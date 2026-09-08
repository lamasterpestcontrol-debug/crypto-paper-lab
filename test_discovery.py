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

if __name__=="__main__": unittest.main()
