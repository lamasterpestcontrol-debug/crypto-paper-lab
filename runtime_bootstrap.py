#!/usr/bin/env python3
"""Resilient PAPER-only runtime bootstrap for paper-scanner.

This file replaces the long Railway inline start command so the production
runtime is versioned, testable, and reproducible from GitHub.

Safety properties:
- refuses explicit LIVE/REAL modes and real-trading flags;
- validates AS/MR/WF/B/OPS side-loaded payloads with Base64 + zlib + SHA256;
- supports valid contiguous one-, two-, or three-part payload layouts;
- supervises all runtime children and stops the group if any child exits;
- never places real trades.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import zlib
from pathlib import Path
from typing import Mapping, MutableSequence


UNSAFE_MODES = {"LIVE", "REAL", "LIVE_TRADING", "REAL_TRADING"}
UNSAFE_FLAG_NAMES = (
    "REAL_TRADING",
    "LIVE_TRADING",
    "ENABLE_REAL_TRADING",
    "ENABLE_LIVE_TRADING",
)
TRUTHY = {"1", "true", "yes", "on"}


def _emit(event: str, **fields: object) -> None:
    print(
        json.dumps({"event": event, **fields}, separators=(",", ":"), allow_nan=False),
        flush=True,
    )


def enforce_paper_only(env: Mapping[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    mode = env.get("MODE", "PAPER").strip().upper()
    if mode in UNSAFE_MODES:
        raise SystemExit("UNSAFE_MODE_" + mode)

    for key in UNSAFE_FLAG_NAMES:
        if env.get(key, "").strip().lower() in TRUTHY:
            raise SystemExit("UNSAFE_TRADING_FLAG_" + key)


def _decode_zlib_b64(encoded: str) -> bytes:
    return zlib.decompress(base64.b64decode(encoded, validate=True))


def decode_hashed_bundle(
    prefix: str,
    max_parts: int,
    sha_key: str,
    target: str,
    env: Mapping[str, str] | None = None,
) -> str:
    """Decode the first valid contiguous prefix0..prefixN bundle matching SHA256.

    This deliberately tries 1..max_parts. That makes a payload resilient to
    Railway variable splitting changes while still requiring the expected hash.
    """
    env = os.environ if env is None else env
    expected = env.get(sha_key, "").strip().lower()
    if not expected:
        raise SystemExit("MISSING_HASH_" + sha_key)

    errors: list[str] = []
    for n in range(1, max_parts + 1):
        values = [env.get(f"{prefix}{i}", "") for i in range(n)]
        if not all(values):
            break
        try:
            raw = _decode_zlib_b64("".join(values))
            actual = hashlib.sha256(raw).hexdigest()
            if actual != expected:
                errors.append(f"{n}p:HASH")
                continue

            Path(target).write_bytes(raw)
            _emit(
                "RUNTIME_SIDELOAD_OK",
                bundle=prefix,
                parts=n,
                sha256=actual,
            )
            return target
        except Exception as exc:
            errors.append(f"{n}p:{type(exc).__name__}")

    raise SystemExit(prefix + "_SIDELOAD_INVALID:" + ",".join(errors[-4:]))


def decode_portfolio_bundle(
    target: str = "/tmp/portfolio_api.py",
    env: Mapping[str, str] | None = None,
) -> str:
    """Decode the legacy six-part PF bundle.

    There is currently no expected PF SHA256 variable in production, so this
    preserves the existing production behavior exactly: strict Base64/zlib
    decoding, with failure closed if any part is missing or corrupt.
    """
    env = os.environ if env is None else env
    parts = [env.get(f"PF{i}", "") for i in range(6)]
    if not all(parts):
        raise SystemExit("PF_SIDELOAD_MISSING")
    try:
        raw = _decode_zlib_b64("".join(parts))
    except Exception as exc:
        raise SystemExit("PF_SIDELOAD_INVALID:" + type(exc).__name__) from exc

    Path(target).write_bytes(raw)
    return target


def _terminate_children(children: MutableSequence[subprocess.Popen[bytes]]) -> None:
    for proc in children:
        if proc.poll() is None:
            proc.terminate()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and any(p.poll() is None for p in children):
        time.sleep(0.05)

    for proc in children:
        if proc.poll() is None:
            proc.kill()


def run() -> int:
    enforce_paper_only()

    portfolio = decode_portfolio_bundle()
    active_sampler = decode_hashed_bundle(
        "AS", 3, "ACTIVE_SAMPLER_SHA256", "/tmp/active_paper_sampler.py"
    )
    major_replay = decode_hashed_bundle(
        "MR", 2, "MAJOR_1M_REPLAY_SHA256", "/tmp/major_1m_replay_lab.py"
    )
    walkforward = decode_hashed_bundle(
        "WF", 2, "WALKFORWARD_SHA256", "/tmp/major_walkforward.py"
    )
    major_backfill = decode_hashed_bundle(
        "B", 2, "MAJOR_BACKFILL_SHA256", "/tmp/major_1m_backfill.py"
    )
    ops_api = decode_hashed_bundle(
        "OPS", 3, "OPS_API_SHA256", "/tmp/ops_api.py"
    )

    commands = (
        [sys.executable, "-u", portfolio],
        [sys.executable, "-u", active_sampler],
        [sys.executable, "-u", "launcher.py"],
        [sys.executable, "-u", major_replay],
        [sys.executable, "-u", walkforward],
        [sys.executable, "-u", major_backfill],
        [sys.executable, "-u", ops_api],
    )

    children: list[subprocess.Popen[bytes]] = []
    stop_requested = False

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True
        _emit("PAPER_RUNTIME_STOP_REQUESTED", signal=signum)

    old_term = signal.signal(signal.SIGTERM, request_stop)
    old_int = signal.signal(signal.SIGINT, request_stop)

    try:
        for command in commands:
            children.append(subprocess.Popen(command))

        _emit(
            "PAPER_RUNTIME_BOOTSTRAP_OK",
            paper_only=True,
            read_only=True,
            no_real_trading=True,
            children=len(children),
        )
        print(
            "PORTFOLIO_ACTIVE_SAMPLER_1M_REPLAY_WF_BACKFILL_AND_OPS_STARTED",
            flush=True,
        )

        while not stop_requested and all(proc.poll() is None for proc in children):
            time.sleep(0.5)

        if stop_requested:
            return_code = 0
        else:
            exited = next((p for p in children if p.poll() is not None), None)
            return_code = exited.returncode if exited and exited.returncode is not None else 1
            _emit("PAPER_RUNTIME_CHILD_EXIT", returncode=return_code)

        _terminate_children(children)
        return int(return_code)
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        if children:
            _terminate_children(children)


if __name__ == "__main__":
    raise SystemExit(run())
