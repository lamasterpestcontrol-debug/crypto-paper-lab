import base64
import hashlib
import os
import tempfile
import unittest
import zlib
from pathlib import Path

import runtime_bootstrap as rb


def packed(raw: bytes) -> str:
    return base64.b64encode(zlib.compress(raw)).decode("ascii")


def split_exact(s: str, parts: int) -> list[str]:
    cuts = [(len(s) * i) // parts for i in range(parts + 1)]
    return [s[cuts[i]:cuts[i + 1]] for i in range(parts)]


class RuntimeBootstrapTests(unittest.TestCase):
    def test_one_part_bundle(self):
        raw = b"print('one')\n"
        env = {"OPS0": packed(raw), "OPS_API_SHA256": hashlib.sha256(raw).hexdigest()}
        with tempfile.TemporaryDirectory() as td:
            target = str(Path(td) / "ops.py")
            rb.decode_hashed_bundle("OPS", 3, "OPS_API_SHA256", target, env)
            self.assertEqual(Path(target).read_bytes(), raw)

    def test_two_part_bundle(self):
        raw = b"x = 'two-part payload'\n" * 20
        enc = packed(raw)
        p0, p1 = split_exact(enc, 2)
        env = {
            "OPS0": p0,
            "OPS1": p1,
            "OPS_API_SHA256": hashlib.sha256(raw).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as td:
            target = str(Path(td) / "ops.py")
            rb.decode_hashed_bundle("OPS", 3, "OPS_API_SHA256", target, env)
            self.assertEqual(Path(target).read_bytes(), raw)

    def test_three_part_bundle(self):
        raw = b"x = 'three-part payload'\n" * 40
        enc = packed(raw)
        p0, p1, p2 = split_exact(enc, 3)
        env = {
            "AS0": p0,
            "AS1": p1,
            "AS2": p2,
            "ACTIVE_SAMPLER_SHA256": hashlib.sha256(raw).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as td:
            target = str(Path(td) / "as.py")
            rb.decode_hashed_bundle("AS", 3, "ACTIVE_SAMPLER_SHA256", target, env)
            self.assertEqual(Path(target).read_bytes(), raw)

    def test_hash_mismatch_fails_closed(self):
        raw = b"print('bad hash')\n"
        env = {"OPS0": packed(raw), "OPS_API_SHA256": "0" * 64}
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                rb.decode_hashed_bundle(
                    "OPS", 3, "OPS_API_SHA256", str(Path(td) / "ops.py"), env
                )
        self.assertIn("OPS_SIDELOAD_INVALID", str(cm.exception))

    def test_missing_hash_fails_closed(self):
        with self.assertRaises(SystemExit) as cm:
            rb.decode_hashed_bundle("OPS", 3, "OPS_API_SHA256", "/tmp/nope", {"OPS0": "x"})
        self.assertEqual(str(cm.exception), "MISSING_HASH_OPS_API_SHA256")

    def test_live_mode_is_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            rb.enforce_paper_only({"MODE": "LIVE"})
        self.assertEqual(str(cm.exception), "UNSAFE_MODE_LIVE")

    def test_real_trading_flag_is_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            rb.enforce_paper_only({"MODE": "PAPER", "ENABLE_REAL_TRADING": "true"})
        self.assertEqual(str(cm.exception), "UNSAFE_TRADING_FLAG_ENABLE_REAL_TRADING")

    def test_portfolio_requires_all_six_parts(self):
        with self.assertRaises(SystemExit) as cm:
            rb.decode_portfolio_bundle(env={"PF0": "abc"})
        self.assertEqual(str(cm.exception), "PF_SIDELOAD_MISSING")


if __name__ == "__main__":
    unittest.main()
