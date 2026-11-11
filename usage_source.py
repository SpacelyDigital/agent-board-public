"""Read subscription usage without running a model turn.

Codex uses its documented app-server account/rateLimits/read method. Claude uses
its installed desktop app's existing, active-account OAuth credential and the
same read-only usage endpoint as the desktop Plan Usage view. Credential reads
are noninteractive and isolated; credentials never leave the reader process
except as an Authorization header to the fixed Anthropic endpoint.
"""
from __future__ import annotations

import base64
import copy
import ctypes
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from local_quota import parse_codex_credits
from process_reader import OutputLimitExceeded, read_process, start_process, stop_process
from privacy_network import ProviderRequestError, read_provider_json

REFRESH_SECONDS = 60
STALE_SECONDS = 120
REQUEST_TIMEOUT = 3.0
CODEX_TIMEOUT = 12.0
CLAUDE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_DESKTOP = Path.home() / "Library/Application Support/Claude"
_KEYCHAIN_INTERACTION_LOCK = threading.Lock()


class UsageUnavailable(Exception):
    """A safe, actionable error string that contains no credentials or responses."""

    def __init__(self, message: str, *, code: str | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code = code if isinstance(code, str) and code in {"claude_account_access_required", "rate_limited"} else None
        self.retry_after = (float(retry_after) if type(retry_after) in (int, float)
                            and 0 <= retry_after <= 86400 and math.isfinite(retry_after) else None)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _epoch(value: Any) -> float | None:
    numeric = _number(value)
    if numeric is not None:
        return numeric if numeric > 0 else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            return parsed.timestamp()
        except (ValueError, OverflowError):
            pass
    return None


def _window(ident: str, label: str, used: Any, reset: Any) -> dict[str, Any] | None:
    percent, reset_at = _number(used), _epoch(reset)
    if percent is None or not 0 <= percent <= 100 or reset_at is None:
        return None
    # Preserve precision until the dashboard rounds once to a whole percent.
    return {"id": ident, "label": label, "remaining_percent": 100 - percent, "reset_at": reset_at}


def parse_codex_usage(response: dict[str, Any], *, include_credits: bool = False) -> list[dict[str, Any]] | dict[str, Any]:
    if not isinstance(response, dict):
        raise UsageUnavailable("Codex returned an unsupported account usage response.")
    by_id = response.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and by_id:
        bucket = by_id.get("codex")
    else:
        bucket = response.get("rateLimits")
    if not isinstance(bucket, dict) or bucket.get("limitId") not in (None, "codex"):
        raise UsageUnavailable("The Codex account did not return its subscription usage window.")
    windows = []
    for key in ("primary", "secondary"):
        value = bucket.get(key)
        if isinstance(value, dict) and value.get("windowDurationMins") == 10080:
            window = _window("weekly", "Weekly", value.get("usedPercent"), value.get("resetsAt"))
            if window:
                windows = [window]
                break
    if include_credits:
        credits = parse_codex_credits(bucket.get("credits"), has_credits_key="hasCredits")
        if credits is not None:
            return {"windows": windows, "credits": credits}
        if windows:
            return {"windows": windows}
    elif windows:
        return windows
    raise UsageUnavailable("The Codex account did not return a weekly usage window.")


def parse_claude_usage(response: dict[str, Any], *, include_credits: bool = False) -> list[dict[str, Any]] | dict[str, Any]:
    if not isinstance(response, dict):
        raise UsageUnavailable("Claude returned an unsupported account usage response.")
    windows = []
    for source_id, ident, label in (("five_hour", "five_hour", "5-hour"), ("seven_day", "weekly", "Weekly")):
        value = response.get(source_id)
        if isinstance(value, dict):
            window = _window(ident, label, value.get("utilization", value.get("used_percentage")), value.get("resets_at"))
            if window:
                windows.append(window)
    credits = None
    extra = response.get("extra_usage")
    # A monthly spending cap is not a prepaid balance. The account can
    # explicitly report that credits were turned off without returning money.
    if (isinstance(extra, dict) and extra.get("is_enabled") is False
            and extra.get("user_disabled") is True):
        credits = {"status": "disabled", "unit": "USD"}
    if include_credits and (windows or credits is not None):
        return {"windows": windows, **({"credits": credits} if credits is not None else {})}
    if not windows:
        raise UsageUnavailable("The Claude account did not return usage percentages and reset times.")
    return windows


def _codex_binary() -> str:
    found = shutil.which("codex")
    if found:
        return found
    for path in (
        "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
        "/Applications/Codex.app/Contents/Resources/codex",
        str(Path.home() / "Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"),
        str(Path.home() / "Applications/Codex.app/Contents/Resources/codex"),
        "/opt/homebrew/bin/codex", "/usr/local/bin/codex",
    ):
        if Path(path).is_file() and os.access(path, os.X_OK):
            return path
    raise UsageUnavailable("Codex is not installed or its account reader could not be found.")


def fetch_codex_usage(*, include_credits: bool = False) -> list[dict[str, Any]] | dict[str, Any]:
    """Use only initialize + account/rateLimits/read; never start a thread/turn."""
    process = None
    selector = selectors.DefaultSelector()
    pending = b""
    deadline = time.monotonic() + CODEX_TIMEOUT
    try:
        process = start_process(
            [_codex_binary(), "app-server", "--stdio", "-c", "analytics.enabled=false"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0,
            env={**{key: value for key, value in os.environ.items()
                    if not key.startswith(("OTEL_", "SENTRY_"))},
                 "OTEL_SDK_DISABLED": "true"},
        )
        selector.register(process.stdout, selectors.EVENT_READ)

        def send(message: dict[str, Any]) -> None:
            process.stdin.write(json.dumps(message).encode() + b"\n")
            process.stdin.flush()

        def receive(ident: int) -> dict[str, Any]:
            nonlocal pending
            while time.monotonic() < deadline:
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(value, dict) and value.get("id") == ident:
                        if "error" in value:
                            raise UsageUnavailable("Codex could not read account usage. Check its existing sign-in.")
                        result = value.get("result")
                        if isinstance(result, dict):
                            return result
                        raise UsageUnavailable("Codex returned an unsupported account usage response.")
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                pending += chunk
                if len(pending) > 1024 * 1024:
                    raise UsageUnavailable("Codex returned an unsupported account usage response.")
            raise UsageUnavailable("The Codex account usage read timed out or the reader stopped.")

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "agent_board", "version": "1.0.0"},
            "capabilities": {"experimentalApi": False},
        }})
        receive(1)
        send({"jsonrpc": "2.0", "method": "initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})
        return parse_codex_usage(receive(2), include_credits=include_credits)
    except UsageUnavailable:
        raise
    except (OSError, subprocess.SubprocessError):
        raise UsageUnavailable("The Codex account reader could not be started.") from None
    finally:
        selector.close()
        if process:
            stop_process(process)


def _copy_keychain_item(security, query, result, *, allow_interaction: bool):
    # The query also disables authentication UI. Guard the legacy file Keychain
    # at process level before making a background request.
    with _KEYCHAIN_INTERACTION_LOCK:
        if allow_interaction:
            return security.SecItemCopyMatching(query, result)
        try:
            get_interaction = security.SecKeychainGetUserInteractionAllowed
            set_interaction = security.SecKeychainSetUserInteractionAllowed
        except AttributeError:
            raise UsageUnavailable("The Mac's Keychain could not be read without permission prompts.") from None
        get_interaction.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        get_interaction.restype = ctypes.c_int32
        set_interaction.argtypes = [ctypes.c_ubyte]
        set_interaction.restype = ctypes.c_int32
        previous = ctypes.c_ubyte()
        if get_interaction(ctypes.byref(previous)) != 0 or set_interaction(0) != 0:
            raise UsageUnavailable("The Mac's Keychain could not be read without permission prompts.")
        try:
            return security.SecItemCopyMatching(query, result)
        finally:
            if set_interaction(previous.value) != 0:
                raise UsageUnavailable("The Mac's Keychain interaction setting could not be restored.")


def _keychain_password(service: str, *, allow_interaction: bool = False) -> bytes:
    """Read the named item; only the explicit account-access action permits UI."""
    if sys.platform != "darwin":
        raise UsageUnavailable("Claude account usage currently requires this Mac's existing desktop sign-in.")
    from ctypes import c_void_p, c_char_p, c_long, c_uint32, c_int32
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    core.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
    core.CFStringCreateWithCString.restype = c_void_p
    core.CFDictionaryCreateMutable.argtypes = [c_void_p, c_long, c_void_p, c_void_p]
    core.CFDictionaryCreateMutable.restype = c_void_p
    core.CFDictionarySetValue.argtypes = [c_void_p, c_void_p, c_void_p]
    core.CFRelease.argtypes = [c_void_p]
    core.CFDataGetLength.argtypes = [c_void_p]
    core.CFDataGetLength.restype = c_long
    core.CFDataGetBytePtr.argtypes = [c_void_p]
    core.CFDataGetBytePtr.restype = c_void_p
    security.SecItemCopyMatching.argtypes = [c_void_p, ctypes.POINTER(c_void_p)]
    security.SecItemCopyMatching.restype = c_int32
    constant = lambda name: c_void_p.in_dll(security, name).value
    query = core.CFDictionaryCreateMutable(None, 0, None, None)
    service_string = core.CFStringCreateWithCString(None, service.encode(), 0x08000100)
    result = c_void_p()
    try:
        for key, value in (
            (constant("kSecClass"), constant("kSecClassGenericPassword")),
            (constant("kSecAttrService"), service_string),
            (constant("kSecReturnData"), c_void_p.in_dll(core, "kCFBooleanTrue").value),
            (constant("kSecMatchLimit"), constant("kSecMatchLimitOne")),
            (constant("kSecUseAuthenticationUI"), constant("kSecUseAuthenticationUIAllow" if allow_interaction else "kSecUseAuthenticationUIFail")),
        ):
            core.CFDictionarySetValue(query, key, value)
        status = _copy_keychain_item(security, query, ctypes.byref(result), allow_interaction=allow_interaction)
        if status in (-25308, -25293, -128):
            if allow_interaction:
                raise UsageUnavailable("Claude account access was not allowed. You can try again from Setup and Permissions.")
            raise UsageUnavailable("Keychain has not allowed access to Claude Safe Storage. No permission prompt was opened.",
                                   code="claude_account_access_required")
        if status == -25300:
            raise UsageUnavailable("Claude Safe Storage was not found in this Mac's available Keychains.")
        if status != 0 or not result.value:
            raise UsageUnavailable(f"Claude Safe Storage could not be read (Keychain status {status}).")
        length = core.CFDataGetLength(result)
        if not 0 < length <= 4096:
            raise UsageUnavailable("Claude Safe Storage returned an unsupported credential format.")
        return ctypes.string_at(core.CFDataGetBytePtr(result), length)
    finally:
        if result.value:
            core.CFRelease(result)
        core.CFRelease(query)
        core.CFRelease(service_string)


def authorize_claude_account() -> dict[str, Any]:
    """User-invoked Keychain grant only, with no network read or secret output."""
    try:
        _keychain_password("Claude Safe Storage", allow_interaction=True)
        return {"ok": True}
    except UsageUnavailable as error:
        return {"ok": False, "error": str(error)}
    except Exception:
        return {"ok": False, "error": "Claude account access could not be granted. Open Claude and check its sign-in, then try again."}


def _decrypt_chromium(raw: bytes, password: bytes) -> bytes:
    """macOS Electron/Chromium v10 AES-CBC using the named app storage key."""
    if raw[:3] != b"v10" or not raw[3:] or len(raw[3:]) % 16:
        raise UsageUnavailable("Claude's encrypted credential format is not supported by this version.")
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
    encrypted = raw[3:]
    output = ctypes.create_string_buffer(len(encrypted) + 16)
    moved = ctypes.c_size_t()
    common = ctypes.CDLL("/usr/lib/system/libcommonCrypto.dylib")
    common.CCCrypt.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    common.CCCrypt.restype = ctypes.c_int32
    status = common.CCCrypt(1, 0, 1, key, len(key), b" " * 16, encrypted, len(encrypted), output, len(output), ctypes.byref(moved))
    if status:
        raise UsageUnavailable("Claude's existing credential cache could not be decrypted.")
    return output.raw[:moved.value]


def _active_claude_org(folder: Path, password: bytes) -> str:
    database = folder / "Cookies"
    try:
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2) as conn:
            rows = conn.execute("SELECT host_key, value, encrypted_value FROM cookies WHERE name='lastActiveOrg' AND host_key IN ('claude.ai', '.claude.ai') LIMIT 3").fetchall()
    except (sqlite3.Error, OSError):
        raise UsageUnavailable("Claude's active organization could not be read from its existing desktop session.") from None
    values = set()
    if len(rows) > 2:
        raise UsageUnavailable("Claude's active organization could not be identified safely.")
    for host, value, encrypted in rows:
        if not value and encrypted:
            decrypted = _decrypt_chromium(bytes(encrypted), password)
            host_hash = hashlib.sha256(host.encode()).digest()
            if decrypted.startswith(host_hash):
                decrypted = decrypted[len(host_hash):]
            value = decrypted.decode("utf8")
        if value:
            values.add(value)
    if len(values) != 1:
        raise UsageUnavailable("Claude's active organization could not be identified safely.")
    return values.pop()


def _claude_token(folder: Path, now: float) -> str:
    try:
        config_path = folder / "config.json"
        if config_path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError
        config = json.loads(config_path.read_text())
        active = config.get("lastKnownAccountUuid")
        encoded = config.get("oauth:tokenCacheV2")
        if not isinstance(active, str) or not isinstance(encoded, str):
            raise UsageUnavailable("Open Claude and sign in to its desktop app to make account usage available.")
        password = _keychain_password("Claude Safe Storage")
        cache = json.loads(_decrypt_chromium(base64.b64decode(encoded, validate=True), password))
        org = _active_claude_org(folder, password)
        prefix = "acct:" + active + "|"
        candidates = []
        for cache_key, entry in cache.items():
            if not isinstance(cache_key, str) or not cache_key.startswith(prefix) or not isinstance(entry, dict):
                continue
            parts = cache_key[len(prefix):].split(":", 2)
            if len(parts) != 3 or parts[1] != org or not parts[2].startswith("https://api.anthropic.com:"):
                continue
            expires = _number(entry.get("expiresAt"))
            if (isinstance(entry.get("token"), str) and 0 < len(entry["token"]) <= 16384
                    and not any(ord(char) < 33 or ord(char) > 126 for char in entry["token"])
                    and expires and expires > now * 1000):
                # Prefer base Claude Code profile scope, matching native Plan Usage.
                scope = parts[2].removeprefix("https://api.anthropic.com:")
                candidates.append(("user:profile" in scope.split(), expires, entry["token"]))
        if not candidates:
            raise UsageUnavailable("Claude's active desktop credential has expired or is unavailable. Open Claude to refresh its own sign-in.")
        return max(candidates, key=lambda entry: entry[:2])[2]
    except UsageUnavailable:
        raise
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise UsageUnavailable("Claude's existing desktop sign-in could not be read.") from None


def _fetch_claude_in_process(now: float, folder: Path = CLAUDE_DESKTOP, *, include_credits: bool = False) -> list[dict[str, Any]] | dict[str, Any]:
    token = _claude_token(folder, now)
    try:
        value = read_provider_json(CLAUDE_ENDPOINT, token, purpose="usage",
                                   timeout=REQUEST_TIMEOUT, max_bytes=1024 * 1024)
        return parse_claude_usage(value, include_credits=include_credits)
    except UsageUnavailable:
        raise
    except ProviderRequestError as error:
        if error.reason == "redirect":
            raise UsageUnavailable("The Claude usage endpoint unexpectedly redirected the request.") from None
        if error.status in (401, 403):
            raise UsageUnavailable("Claude did not accept its existing desktop sign-in for account usage. Open Claude to check its sign-in.") from None
        if error.status == 429:
            raise UsageUnavailable("Usage checks are temporarily rate limited.", code="rate_limited",
                                   retry_after=error.retry_after) from None
        if error.status is not None:
            raise UsageUnavailable(f"Claude account usage is unavailable (HTTP {error.status}).",
                                   retry_after=error.retry_after) from None
        raise UsageUnavailable("The Claude account usage read failed or timed out.") from None


def fetch_claude_usage() -> list[dict[str, Any]] | dict[str, Any]:
    # Security.framework can stall even with authentication UI forbidden. Keep the
    # complete credential read + fixed-origin request in an isolated subprocess;
    # its only output is normalized usage or a sanitized error, never a credential.
    try:
        result = read_process(
            [sys.executable, *([] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve())]), "--read-claude-usage"],
            timeout=REQUEST_TIMEOUT + 2, max_output=64 * 1024,
        )
    except subprocess.TimeoutExpired:
        raise UsageUnavailable("Claude's noninteractive Keychain or account usage read timed out. No permission prompt was requested.") from None
    except OSError:
        raise UsageUnavailable("The Claude account usage reader could not be started.") from None
    except OutputLimitExceeded:
        raise UsageUnavailable("The Claude account usage reader returned an oversized result.") from None
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError
        if value.get("error"):
            raise UsageUnavailable(str(value["error"]), code=value.get("error_code"), retry_after=value.get("retry_after"))
        windows = value.get("windows")
        credits = value.get("credits")
        credits_disabled = credits == {"status": "disabled", "unit": "USD"}
        if not isinstance(windows, list) or (not windows and not credits_disabled):
            raise ValueError
        # Keep only normalized public fields even across the helper boundary.
        normalized = []
        for row in windows:
            if not isinstance(row, dict) or row.get("id") not in ("five_hour", "weekly"):
                raise ValueError
            remaining, reset = _number(row.get("remaining_percent")), _epoch(row.get("reset_at"))
            if remaining is None or not 0 <= remaining <= 100 or reset is None:
                raise ValueError
            normalized.append({"id": row["id"], "label": "5-hour" if row["id"] == "five_hour" else "Weekly",
                               "remaining_percent": remaining, "reset_at": reset})
        return {"windows": normalized, "credits": {"status": "disabled", "unit": "USD"}} if credits_disabled else normalized
    except UsageUnavailable:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise UsageUnavailable("The Claude account usage reader did not return a usable result.") from None


class UsageSource:
    """Retain each provider's learned polling pace without losing usage history."""

    def __init__(self, fetchers: dict[str, Callable[[], list[dict[str, Any]] | dict[str, Any]]] | None = None):
        self._fetchers = fetchers or {"codex": lambda: fetch_codex_usage(include_credits=True), "claude": fetch_claude_usage}
        self._cache: dict[str, dict[str, Any]] = {}
        self._attempts: dict[str, float] = {}
        self._next_attempts: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._refresh_floors: dict[str, float] = {}

    def scan(self, now: float) -> list[dict[str, Any]]:
        entries = []
        for ident, name in (("claude", "Claude"), ("codex", "Codex")):
            cached = self._cache.get(ident)
            last_attempt = self._attempts.get(ident)
            if last_attempt is None or now >= self._next_attempts.get(ident, 0) or now < last_attempt:
                self._attempts[ident] = now
                try:
                    result = self._fetchers[ident]()
                    windows = result["windows"] if isinstance(result, dict) else result
                    credits = result.get("credits") if isinstance(result, dict) else None
                    windows = [window for window in windows if window.get("id") == "weekly"]
                    if not windows and credits is None:
                        raise UsageUnavailable("Weekly account usage is currently unavailable.")
                    cached = {"id": ident, "name": name, "available": True, "updated_at": now, "stale": False, "error": None, "error_code": None, "retry_at": None, "windows": windows, "loading": False}
                    self._failures[ident] = 0
                    self._next_attempts[ident] = now + self._refresh_floors.get(ident, REFRESH_SECONDS)
                    if credits is not None:
                        cached["credits"] = {**credits, "observed_at": now}
                except Exception as error:
                    message = str(error) if isinstance(error, UsageUnavailable) else "Account usage could not be read."
                    code = error.code if isinstance(error, UsageUnavailable) else None
                    failures = self._failures.get(ident, 0) + 1
                    self._failures[ident] = failures
                    base = 300 if code == "rate_limited" else REFRESH_SECONDS
                    delay = min(1800, base * 2 ** min(failures - 1, 5))
                    floor = self._refresh_floors.get(ident, REFRESH_SECONDS)
                    if code == "rate_limited":
                        # A successful retry does not prove that minute polling
                        # is permitted. Remember the slower pace for this run;
                        # another rate limit increases it even after success.
                        delay = max(delay, min(1800, max(300, floor * 2)))
                    delay = max(delay, floor)
                    if isinstance(error, UsageUnavailable) and error.retry_after is not None:
                        delay = max(delay, error.retry_after)
                    if code == "rate_limited":
                        self._refresh_floors[ident] = delay
                    self._next_attempts[ident] = now + delay
                    if cached and cached.get("available"):
                        cached = {**cached, "stale": True, "error": message, "error_code": code}
                    else:
                        cached = {"id": ident, "name": name, "available": False, "updated_at": None, "stale": False, "error": message, "error_code": code, "windows": [], "loading": False}
                    cached["retry_at"] = self._next_attempts[ident]
                self._cache[ident] = cached
            entry = copy.deepcopy(cached)
            if entry.get("updated_at") is not None and now - entry["updated_at"] > STALE_SECONDS:
                entry["stale"] = True
            if "credits" in entry:
                entry["credits"]["stale"] = entry["stale"]
            # Never roll an expired window forward or infer that it has reset to
            # 100%. Keep its last observed timestamp and mark the reading stale.
            if any(window["reset_at"] <= now for window in entry["windows"]):
                entry["stale"] = True
                entry["error"] = entry.get("error") or "A reported usage reset has passed; waiting for an updated account reading."
            entries.append(entry)
        return entries


if __name__ == "__main__" and sys.argv[1:] == ["--authorize-claude"]:
    print(json.dumps(authorize_claude_account()))

if __name__ == "__main__" and sys.argv[1:] == ["--read-claude-usage"]:
    try:
        print(json.dumps(_fetch_claude_in_process(time.time(), include_credits=True)))
    except UsageUnavailable as error:
        print(json.dumps({"error": str(error), "error_code": error.code, "retry_after": error.retry_after}))
    except Exception:
        print(json.dumps({"error": "Claude account usage could not be read."}))
