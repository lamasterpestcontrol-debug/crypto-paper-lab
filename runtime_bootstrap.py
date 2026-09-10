#!/usr/bin/env python3
"""Resilient PAPER-only runtime bootstrap for paper-scanner.

Core scanner services fail closed. The auxiliary OPS API is isolated: an OPS
side-load/configuration failure is reported, but it cannot take down discovery,
history replay, strategy research, market-regime, OHLCV, paper ledgers, or the
intelligence hub.

Safety properties:
- refuses explicit LIVE/REAL modes and real-trading flags;
- validates required AS/MR/WF/B payloads with Base64 + zlib + SHA256;
- supports valid contiguous one-, two-, or three-part side-load layouts;
- treats OPS as auxiliary and isolates its decode/runtime failure from core;
- supervises all core children and stops the group if any core child exits;
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


class BundleDecodeError(RuntimeError):
    """A side-loaded runtime bundle failed integrity/format validation."""


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
    """Decode the first valid contiguous prefix0..prefixN bundle matching SHA256."""
    env = os.environ if env is None else env
    expected = env.get(sha_key, "").strip().lower()
    if not expected:
        raise BundleDecodeError("MISSING_HASH_" + sha_key)

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

    raise BundleDecodeError(
        prefix + "_SIDELOAD_INVALID:" + ",".join(errors[-4:])
    )


def decode_optional_hashed_bundle(
    prefix: str,
    max_parts: int,
    sha_key: str,
    target: str,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Decode an auxiliary bundle without allowing it to kill core services."""
    try:
        return decode_hashed_bundle(prefix, max_parts, sha_key, target, env)
    except BundleDecodeError as exc:
        _emit(
            "RUNTIME_AUX_DEGRADED",
            bundle=prefix,
            error_type=type(exc).__name__,
            reason=str(exc)[:160],
            core_continues=True,
        )
        return None


def decode_portfolio_bundle(
    target: str = "/tmp/portfolio_api.py",
    env: Mapping[str, str] | None = None,
) -> str:
    """Decode the required legacy six-part PF bundle."""
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
    try:
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
    except BundleDecodeError as exc:
        _emit(
            "PAPER_RUNTIME_REQUIRED_BOOTSTRAP_FAIL",
            error_type=type(exc).__name__,
            reason=str(exc)[:160],
        )
        raise

    ops_api = decode_optional_hashed_bundle(
        "OPS", 3, "OPS_API_SHA256", "/tmp/ops_api.py"
    )

    core_commands = (
        [sys.executable, "-u", portfolio],
        [sys.executable, "-u", active_sampler],
        [sys.executable, "-u", "launcher.py"],
        [sys.executable, "-u", major_replay],
        [sys.executable, "-u", walkforward],
        [sys.executable, "-u", major_backfill],
    )

    core: list[subprocess.Popen[bytes]] = []
    aux: subprocess.Popen[bytes] | None = None
    stop_requested = False

    def request_stop(signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True
        _emit("PAPER_RUNTIME_STOP_REQUESTED", signal=signum)

    old_term = signal.signal(signal.SIGTERM, request_stop)
    old_int = signal.signal(signal.SIGINT, request_stop)

    try:
        for command in core_commands:
            core.append(subprocess.Popen(command))

        if ops_api is not None:
            aux = subprocess.Popen([sys.executable, "-u", ops_api])

        _emit(
            "PAPER_RUNTIME_BOOTSTRAP_OK",
            paper_only=True,
            read_only=True,
            no_real_trading=True,
            core_children=len(core),
            ops_aux_available=aux is not None,
        )
        print("CORE_SCANNER_STARTED_OPS_ISOLATED", flush=True)

        while not stop_requested and all(proc.poll() is None for proc in core):
            if aux is not None and aux.poll() is not None:
                _emit(
                    "RUNTIME_AUX_EXIT",
                    bundle="OPS",
                    returncode=aux.returncode,
                    core_continues=True,
                )
                aux = None
            time.sleep(0.5)

        if stop_requested:
            return_code = 0
        else:
            exited = next((p for p in core if p.poll() is not None), None)
            return_code = (
                exited.returncode
                if exited is not None and exited.returncode is not None
                else 1
            )
            _emit(
                "PAPER_RUNTIME_CORE_EXIT",
                returncode=return_code,
                core_continues=False,
            )

        children = list(core)
        if aux is not None:
            children.append(aux)
        _terminate_children(children)
        return int(return_code)
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        children = list(core)
        if aux is not None:
            children.append(aux)
        if children:
            _terminate_children(children)


if __name__ == "__main__":
    raise SystemExit(run())
