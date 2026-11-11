"""Normalize quota figures already exported locally by the provider.

This module does not locate accounts, authenticate, start provider programs or
open a network connection. Callers must supply their existing source grant.
The Claude mod writes two checksummed slots because its documented fs.write
operation is not atomic. Only a complete, validated snapshot replaces a reading.
"""
from __future__ import annotations

import copy
from datetime import datetime
from decimal import Decimal
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any


CLAUDE_EXPORT_RELATIVE = Path("agent-board/usage")
CLAUDE_EXPORT_SLOTS = ("quota-v1-a.json", "quota-v1-b.json")
MAX_EXPORT_BYTES = 8192
MAX_PAYLOAD_BYTES = 2048
STALE_SECONDS = 120
_EARLIEST = 946684800  # 2000-01-01 UTC
_LATEST = 4102444800  # 2100-01-01 UTC
_MAX_RESET_DISTANCE = 8 * 86400
_CODEX_CREDIT_DECIMAL = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")
_MAX_SAFE_JSON_INTEGER = 2**53 - 1


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (OverflowError, ValueError):
        return None


def _timestamp(value: Any) -> float | None:
    number = _number(value)
    if number is None and isinstance(value, str) and len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                number = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return number if number is not None and _EARLIEST <= number < _LATEST else None


def _reading(provider: str, windows: list[dict[str, Any]], observed: float, now: float,
             source: str) -> dict[str, Any]:
    expired = any(window["reset_at"] is not None and window["reset_at"] <= now for window in windows)
    stale = now - observed > STALE_SECONDS or expired
    error = None
    if not windows:
        error = "The local session has not reported subscription usage."
    elif expired:
        error = "The reported reset has passed; waiting for another local session reading."
    elif stale:
        error = "The local session reading has not been updated recently."
    return {"id": provider, "name": "Claude" if provider == "claude" else "Codex",
            "source": source, "available": bool(windows), "updated_at": observed,
            "stale": stale, "error": error, "windows": windows, "loading": False}


def parse_codex_credits(raw: Any, *, has_credits_key: str, observed_at: float | None = None) -> dict[str, Any] | None:
    """Return an explicit finite workspace credit balance, not earned resets.

    The Codex protocol sends balances as decimal strings. Export only numbers
    that retain the reported value through the dashboard's JSON number format.
    Neither boolean availability flags nor quota percentages imply a balance.
    """
    if (not isinstance(raw, dict) or type(raw.get(has_credits_key)) is not bool
            or raw.get("unlimited") is not False):
        return None
    balance = raw.get("balance")
    if not isinstance(balance, str) or len(balance) > 64 or not _CODEX_CREDIT_DECIMAL.fullmatch(balance):
        return None
    exact = Decimal(balance)
    if exact.copy_abs() > _MAX_SAFE_JSON_INTEGER:
        return None
    amount = int(exact) if exact == exact.to_integral_value() else float(exact)
    if Decimal(str(amount)) != exact:
        return None
    credits = {"amount": amount, "unit": "credits"}
    if observed_at is not None:
        credits["observed_at"] = observed_at
    return credits


def parse_codex_observation(rate_limits: Any, observed_at: Any, *, now: float) -> dict[str, Any] | None:
    """Read a verified local token_count snapshot, never an authenticated API.

    Its timestamp is an observation of a potentially cached provider value.
    A seven-day duration identifies the weekly window; slot position does not.
    Model-specific buckets are not a general Codex subscription allowance.
    """
    current, observed = _number(now), _timestamp(observed_at)
    if (current is None or observed is None or observed > current + 60
            or not isinstance(rate_limits, dict)
            or rate_limits.get("limit_id") not in (None, "codex")):
        return None
    windows = []
    for key in ("primary", "secondary"):
        raw = rate_limits.get(key)
        if not isinstance(raw, dict) or type(raw.get("window_minutes")) is not int or raw["window_minutes"] != 10080:
            continue
        used, reset = _number(raw.get("used_percent")), _number(raw.get("resets_at"))
        if (used is None or not 0 <= used <= 100 or reset is None or not reset.is_integer()
                or not _EARLIEST <= reset < _LATEST or abs(reset - observed) > _MAX_RESET_DISTANCE):
            continue
        # Intermediate rounding can push a value below .5 over the UI threshold.
        value = {"id": "weekly", "label": "Weekly", "remaining_percent": 100 - used,
                 "reset_at": reset}
        if windows and windows[0] != value:
            windows = []
            break
        windows = [value]
    credits = parse_codex_credits(rate_limits.get("credits"), has_credits_key="has_credits", observed_at=observed)
    if not windows and credits is None:
        return None
    reading = _reading("codex", windows, observed, current, "local_record")
    if credits is not None:
        reading["credits"] = credits
        reading["available"] = True
        if not windows:
            reading["error"] = "The local session reading has not been updated recently." if reading["stale"] else None
    return reading


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate local quota field.")
        value[key] = item
    return value


def _invalid_number(_value):
    raise ValueError("Invalid local quota number.")


def _decode(raw):
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_number)


def parse_claude_export(raw: bytes, *, now: float) -> dict[str, Any] | None:
    """Accept only the mod's canonical quota-only v1 envelope.

    The checksum detects partial or mixed writes; it is not authentication.
    The local user's other software can alter its own quota export.
    An explicit null reset retains a percentage whose provider reset is unknown.
    """
    current = _number(now)
    if current is None or not isinstance(raw, bytes) or len(raw) > MAX_EXPORT_BYTES:
        return None
    try:
        envelope = _decode(raw.decode("utf-8"))
        if (not isinstance(envelope, dict) or set(envelope) != {"version", "payload", "sha256"}
                or type(envelope["version"]) is not int or envelope["version"] != 1):
            return None
        payload, digest = envelope["payload"], envelope["sha256"]
        if (not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES
                or not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or not hmac.compare_digest(hashlib.sha256(payload.encode("utf-8")).hexdigest(), digest)):
            return None
        value = _decode(payload)
        if (not isinstance(value, dict) or set(value) != {"provider", "observed_at_ms", "windows"}
                or value["provider"] != "claude" or type(value["observed_at_ms"]) is not int
                or not isinstance(value["windows"], list) or len(value["windows"]) > 2):
            return None
        observed = _timestamp(value["observed_at_ms"] / 1000)
        if observed is None or observed > current + 60:
            return None
        windows, canonical_windows = [], []
        prior = -1
        for item in value["windows"]:
            if (not isinstance(item, dict) or set(item) != {"id", "used_basis_points", "reset_at"}
                    or item["id"] not in ("five_hour", "weekly")
                    or type(item["used_basis_points"]) is not int or not 0 <= item["used_basis_points"] <= 10000
                    or (item["reset_at"] is not None and type(item["reset_at"]) is not int)):
                return None
            order = ("five_hour", "weekly").index(item["id"])
            reset = None if item["reset_at"] is None else _timestamp(item["reset_at"])
            if (order <= prior or (item["reset_at"] is not None
                    and (reset is None or abs(reset - observed) > _MAX_RESET_DISTANCE))):
                return None
            prior = order
            canonical_windows.append({"id": item["id"], "used_basis_points": item["used_basis_points"],
                                      "reset_at": item["reset_at"]})
            windows.append({"id": item["id"], "label": "5-hour" if item["id"] == "five_hour" else "Weekly",
                            "remaining_percent": round((10000 - item["used_basis_points"]) / 100, 2),
                            "reset_at": reset})
        canonical = {"provider": "claude", "observed_at_ms": value["observed_at_ms"], "windows": canonical_windows}
        if payload != json.dumps(canonical, separators=(",", ":"), ensure_ascii=True):
            return None
        return _reading("claude", windows, observed, current, "local_export")
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        return None


def _read_slot(root: Path, filename: str) -> bytes:
    """Walk the fixed relative path using directory descriptors and no symlinks."""
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        directory = os.open(root, flags | os.O_DIRECTORY)
        descriptors.append(directory)
        for part in CLAUDE_EXPORT_RELATIVE.parts:
            directory = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            descriptors.append(directory)
        descriptor = os.open(filename, flags, dir_fd=directory)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or before.st_size > MAX_EXPORT_BYTES):
            raise ValueError("Invalid local quota file.")
        raw = bytearray()
        while len(raw) <= MAX_EXPORT_BYTES:
            chunk = os.read(descriptor, MAX_EXPORT_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if len(raw) > MAX_EXPORT_BYTES or identity(before) != identity(os.fstat(descriptor)):
            raise ValueError("Local quota file changed while being read.")
        return bytes(raw)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class ClaudeLocalQuota:
    """Read only two fixed slots below an already authorized Claude root.

    Discard this reader when its native source grant is revoked. A failed write
    preserves the prior verified reading with its original time and a stale flag.
    It never makes an old or expired record look freshly fetched.
    """

    def __init__(self, claude_root: Path):
        self.root = Path(claude_root)
        self._cached = None
        self._seen_slots = set()

    def snapshot(self, now: float) -> dict[str, Any]:
        candidates = []
        interrupted = False
        for filename in CLAUDE_EXPORT_SLOTS:
            try:
                reading = parse_claude_export(_read_slot(self.root, filename), now=now)
                self._seen_slots.add(filename)
                interrupted |= reading is None
            except FileNotFoundError:
                interrupted |= filename in self._seen_slots
                reading = None
            except (OSError, ValueError):
                interrupted = True
                reading = None
            if reading is not None:
                reading["windows"] = [window for window in reading["windows"] if window["id"] == "weekly"]
                candidates.append(reading)
        if candidates:
            newest = max(candidates, key=lambda reading: reading["updated_at"])
            if self._cached is None or newest["updated_at"] >= self._cached["updated_at"]:
                self._cached = newest
            else:
                interrupted = True
            cached = copy.deepcopy(self._cached)
            cached = _reading("claude", cached["windows"], cached["updated_at"], now, "local_export")
            if interrupted:
                cached["stale"] = True
                cached["error"] = "A local quota export is incomplete or unavailable."
            return cached
        if self._cached is not None:
            result = _reading("claude", copy.deepcopy(self._cached["windows"]), self._cached["updated_at"], now, "local_export")
            result["stale"] = True
            result["error"] = "The local quota export is incomplete or unavailable."
            return result
        return {"id": "claude", "name": "Claude", "source": "local_export", "available": False,
                "updated_at": None, "stale": False, "error": "Connect the local Claude usage exporter to show quota.",
                "windows": [], "loading": False}

    scan = snapshot
