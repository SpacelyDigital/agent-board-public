"""Read Claude account chat metadata using its existing desktop sign-in.

The isolated helper sends only bounded GET requests to two fixed claude.ai
routes. It never reads a conversation transcript, refreshes a credential, or
requests Keychain interaction. Only allowlisted metadata leaves the helper.
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import http.client
import json
import math
from pathlib import Path
import re
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from typing import Callable
import urllib.error
import urllib.request
import uuid

from privacy_network import ProviderRequestError, RejectRedirects, _retry_after
from process_reader import OutputLimitExceeded, read_process
from usage_source import CLAUDE_DESKTOP, UsageUnavailable, _active_claude_org, _decrypt_chromium, _keychain_password

MAX_CHATS = 100
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_HELPER_BYTES = 512 * 1024
FRESH_SECONDS = 15.0
IDLE_POLL_SECONDS = 10.0
ACTIVE_POLL_SECONDS = 5.0
FETCH_TIMEOUT = 13.0
_ERROR_CODES = {"unavailable", "authentication", "account_mismatch", "rate_limited", "access_required", "invalid_response"}


def _uuid(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = str(uuid.UUID(value))
        return parsed if parsed == value.lower() else None
    except (ValueError, AttributeError):
        return None


def _timestamp(value: object, now: float) -> float | None:
    try:
        if isinstance(value, str) and len(value) <= 64:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            result = parsed.timestamp()
        elif type(value) in (int, float):
            result = float(value)
        else:
            return None
        return result if math.isfinite(result) and 0 < result <= now + 30 else None
    except (ValueError, OverflowError, OSError):
        return None


def _enum(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value):
        raise ProviderRequestError("response")
    return value


def _session_id(value: object) -> str | None:
    # The list uses provider session identifiers, not necessarily UUIDs.
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else None


def parse_conversations(data: object, now: float) -> dict[str, dict]:
    """Keep identity and lifecycle metadata; discard summaries and settings."""
    if not isinstance(data, list) or len(data) > MAX_CHATS:
        raise ProviderRequestError("response")
    result: dict[str, dict] = {}
    seen: set[str] = set()
    for row in data:
        if not isinstance(row, dict):
            raise ProviderRequestError("response")
        # The observed list route has no transcript fields. Fail closed if that
        # contract changes, rather than quietly retaining a transcript-bearing
        # response. Tool settings can contain schema keys called 'content'; the
        # whole settings object is discarded and is never normalized or logged.
        if any(row.get(key) for key in ("chat_messages", "messages", "mapping", "content", "body")):
            raise ProviderRequestError("response")
        identifier = _uuid(row.get("uuid"))
        if not identifier or identifier in seen:
            raise ProviderRequestError("response")
        seen.add(identifier)
        if row.get("is_archived") is not False or row.get("is_temporary") is not False:
            continue
        if row.get("platform") != "CLAUDE_AI":
            continue
        if "live_status" not in row or "needs_input" not in row:
            continue  # Missing status metadata cannot prove a settled reply.
        needs_input = row["needs_input"]
        if needs_input is not None and type(needs_input) is not bool:
            raise ProviderRequestError("response")
        updated = _timestamp(row.get("updated_at"), now)
        if updated is None:
            continue
        title = row.get("name")
        title = " ".join(title.split())[:160] if isinstance(title, str) else ""
        result[identifier] = {
            "title": title or "Claude chat",
            "created_at": _timestamp(row.get("created_at"), now),
            "updated_at": updated,
            "last_settled_at": _timestamp(row.get("last_settled_at"), now),
            "current_leaf_message_uuid": _uuid(row.get("current_leaf_message_uuid")),
            "live_status": _enum(row["live_status"]),
            "live_status_started_at": _timestamp(row.get("live_status_started_at"), now),
            "needs_input": needs_input,
            "needs_input_kind": _enum(row.get("needs_input_kind")),
            # Ordinary web chats can have workspace IDs. Keep the exact ID for
            # overlap resolution, without guessing that it is a Code project.
            "workspace_session_id": _session_id(row.get("workspace_session_id")),
            "project_uuid": _uuid(row.get("project_uuid")),
        }
    return result


def _config_account(folder: Path) -> str:
    path = folder / "config.json"
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError
    with path.open("rb") as handle:
        raw = handle.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError
    data = json.loads(raw)
    account = _uuid(data.get("lastKnownAccountUuid")) if isinstance(data, dict) else None
    if not account:
        raise ValueError
    return account


def _cookie_rows(folder: Path) -> list[tuple]:
    with sqlite3.connect((folder / "Cookies").resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2) as connection:
        return connection.execute(
            "SELECT name,host_key,value,encrypted_value,expires_utc FROM cookies "
            "WHERE name IN ('sessionKey','lastActiveOrg') "
            "AND host_key IN ('claude.ai','.claude.ai') AND path='/' "
            "ORDER BY name,host_key LIMIT 5").fetchall()


def credential_revision(folder: Path = CLAUDE_DESKTOP) -> str | None:
    """Local-only account/cookie fingerprint; no decryption or Keychain read."""
    try:
        account = _config_account(folder)
        rows = _cookie_rows(folder)
        if not 1 <= len(rows) <= 4 or not any(row[0] == "sessionKey" for row in rows):
            return None
        digest = hashlib.sha256(account.encode())
        for row in rows:
            for value in row:
                blob = value if isinstance(value, bytes) else str(value).encode()
                if len(blob) > 32768:
                    return None
                digest.update(len(blob).to_bytes(4, "big"))
                digest.update(blob)
        return digest.hexdigest()
    except (OSError, ValueError, TypeError, sqlite3.Error, RecursionError):
        return None


def _credentials(folder: Path, now: float) -> tuple[str, str, str]:
    account = _config_account(folder)
    password = _keychain_password("Claude Safe Storage")
    org = _uuid(_active_claude_org(folder, password))
    if not org:
        raise ProviderRequestError("authentication")
    rows = [row for row in _cookie_rows(folder) if row[0] == "sessionKey"]
    if len(rows) != 1:
        raise ProviderRequestError("authentication")
    _, host, value, encrypted, expiry = rows[0]
    if type(expiry) not in (int, float) or (expiry != 0 and expiry / 1_000_000 - 11644473600 <= now):
        raise ProviderRequestError("authentication")
    if not value and encrypted:
        raw = _decrypt_chromium(bytes(encrypted), password)
        binding = hashlib.sha256(host.encode()).digest()
        value = (raw[len(binding):] if raw.startswith(binding) else raw).decode("utf8")
    if (not isinstance(value, str) or not 0 < len(value) <= 16384
            or any(ord(char) < 33 or ord(char) > 126 or char == ";" for char in value)):
        raise ProviderRequestError("authentication")
    return account, org, value


def _read_json(route: str, org: str, cookie: str, *, max_bytes: int) -> object:
    """Fixed first-party metadata routes, verified TLS, no redirects/proxies."""
    if (not _uuid(org) or route not in ("/api/account", "/api/organizations/" + org + "/chat_conversations?limit=100&is_archived=false")
            or not isinstance(cookie, str) or not 0 < len(cookie) <= 16384
            or any(ord(char) < 33 or ord(char) > 126 or char == ";" for char in cookie)
            or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_METADATA_BYTES):
        raise ProviderRequestError("request")
    request = urllib.request.Request("https://claude.ai" + route, method="GET", headers={
        "Cookie": "sessionKey=" + cookie + "; lastActiveOrg=" + org,
        "Accept": "application/json", "User-Agent": "AgentBoard/0.2.5"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), RejectRedirects(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=5.0) as response:
            raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ProviderRequestError("response")
        return json.loads(raw)
    except ProviderRequestError:
        raise
    except urllib.error.HTTPError as error:
        try:
            status = error.code
            retry_after = _retry_after(error.headers.get("Retry-After") if error.headers else None)
        finally:
            error.close()
        raise ProviderRequestError("http", status, retry_after=retry_after) from None
    except (OSError, ValueError, TypeError, RecursionError, http.client.HTTPException):
        raise ProviderRequestError("response") from None


def read_account_chats(folder: Path = CLAUDE_DESKTOP, *, now: float | None = None) -> dict:
    """Helper entry: credentials and raw provider data stay in this process."""
    now = time.time() if now is None else now
    revision = credential_revision(folder)
    if revision is None:
        raise ProviderRequestError("authentication")
    account, org, cookie = _credentials(folder, now)
    profile = _read_json("/api/account", org, cookie, max_bytes=256 * 1024)
    if not isinstance(profile, dict) or _uuid(profile.get("uuid")) != account:
        raise ProviderRequestError("account_mismatch")
    del profile
    data = _read_json("/api/organizations/" + org + "/chat_conversations?limit=100&is_archived=false",
                      org, cookie, max_bytes=MAX_METADATA_BYTES)
    conversations = parse_conversations(data, now)
    del data
    if credential_revision(folder) != revision:
        raise ProviderRequestError("authentication")
    return {"account_scope": hashlib.sha256((account + "\0" + org).encode()).hexdigest(),
            "conversations": conversations}


def helper_main() -> None:
    """Emit normalized metadata or fixed errors, never exception/provider text."""
    try:
        result = read_account_chats()
    except ProviderRequestError as error:
        code = ("rate_limited" if error.status == 429 else
                "authentication" if error.status in (401, 403) or error.reason == "authentication" else
                "account_mismatch" if error.reason == "account_mismatch" else "unavailable")
        result = {"error_code": code, "retry_after": error.retry_after}
    except UsageUnavailable as error:
        result = {"error_code": "access_required" if error.code == "claude_account_access_required" else "authentication"}
    except Exception:
        result = {"error_code": "unavailable"}
    print(json.dumps(result, separators=(",", ":"), allow_nan=False))


def _fetch_snapshot() -> dict:
    try:
        command = [sys.executable, *([] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve())]), "--read-claude-chats"]
        process = read_process(command, timeout=FETCH_TIMEOUT, max_output=MAX_HELPER_BYTES)
        data = json.loads(process.stdout)
        if process.returncode != 0 or not isinstance(data, dict):
            raise ValueError
        return data
    except (OSError, ValueError, TypeError, RecursionError, subprocess.TimeoutExpired, OutputLimitExceeded):
        return {"error_code": "unavailable"}


def _validated_result(data: object, now: float) -> tuple[str, dict[str, dict]]:
    """Validate the helper protocol independently of the provider parser."""
    if not isinstance(data, dict) or not isinstance(data.get("account_scope"), str) or not re.fullmatch(r"[a-f0-9]{64}", data["account_scope"]):
        raise ValueError
    rows = data.get("conversations")
    if not isinstance(rows, dict) or len(rows) > MAX_CHATS:
        raise ValueError
    raw = []
    for identifier, row in rows.items():
        if not isinstance(row, dict):
            raise ValueError
        raw.append({**row, "uuid": identifier, "name": row.get("title"), "platform": "CLAUDE_AI",
                    "is_archived": False, "is_temporary": False})
    parsed = parse_conversations(raw, now)
    if len(parsed) != len(rows):
        raise ValueError
    return data["account_scope"], parsed


class ClaudeChatAccountReader:
    """Nonblocking cached metadata with account isolation and bounded retries."""
    def __init__(self, *, fetcher: Callable[[], dict] | None = None,
                 revision_reader: Callable[[], str | None] | None = None,
                 clock: Callable[[], float] = time.time):
        self._fetcher = fetcher or _fetch_snapshot
        self._revision_reader = revision_reader or credential_revision
        self._clock = clock
        self._lock = threading.Lock()
        self._revision: str | None = None
        self._generation = 0
        self._busy = False
        self._closed = False
        self._at: float | None = None
        self._scope: str | None = None
        self._conversations: dict[str, dict] = {}
        self._error: str | None = None
        self._next_at = float("-inf")
        self._failures = 0
        self._rate_floor = 0.0

    def scan(self, now: float) -> dict:
        # This bounded local check avoids showing another account's rows while
        # an old helper finishes. It never decrypts cookies or opens Keychain.
        try:
            revision = self._revision_reader()
        except Exception:
            revision = None
        with self._lock:
            if revision != self._revision:
                self._revision = revision
                self._generation += 1
                self._at = None
                self._conversations = {}
                self._error = None
                self._next_at = float("-inf")
                self._failures = 0
            if revision is None:
                self._error = "authentication"
            if not self._closed and revision is not None and not self._busy and now >= self._next_at:
                self._busy = True
                try:
                    threading.Thread(target=self._refresh, args=(self._generation, revision), daemon=True,
                                     name="claude-chat-metadata").start()
                except Exception:
                    # A transient OS thread limit must not leave a nonexistent
                    # worker marked busy for the rest of this app session.
                    self._busy = False
                    self._error = "unavailable"
                    self._next_at = now + max(30.0, self._rate_floor)
            fresh = bool(not self._closed and revision is not None and self._at is not None
                         and 0 <= now - self._at <= FRESH_SECONDS)
            return {"available": fresh, "fresh": fresh, "observed_at": self._at,
                    "account_scope": self._scope, "conversations": copy.deepcopy(self._conversations) if fresh else {},
                    "error_code": self._error, "retry_at": self._next_at if math.isfinite(self._next_at) and self._next_at > now else None}

    def _refresh(self, generation: int, revision: str) -> None:
        try:
            data = self._fetcher()
        except Exception:
            data = {"error_code": "unavailable"}
        try:
            now = self._clock()
        except Exception:
            now = time.time()
        try:
            current_revision = self._revision_reader()
        except Exception:
            current_revision = None
        with self._lock:
            self._busy = False
            if self._closed or generation != self._generation or current_revision != revision:
                return
            try:
                scope, conversations = _validated_result(data, now)
            except (ValueError, TypeError, ProviderRequestError):
                error = data.get("error_code") if isinstance(data, dict) else None
                self._error = error if isinstance(error, str) and error in _ERROR_CODES else "invalid_response"
                self._failures += 1
                delay = min(300.0, 30.0 * 2 ** min(self._failures - 1, 4))
                hint = data.get("retry_after") if isinstance(data, dict) else None
                hint = float(hint) if type(hint) in (int, float) and 0 <= hint <= 86400 and math.isfinite(hint) else 0.0
                if self._error == "rate_limited":
                    self._rate_floor = max(300.0, min(1800.0, self._rate_floor * 2), hint)
                elif self._error in {"authentication", "account_mismatch", "access_required"}:
                    delay = max(delay, 300.0)
                    self._at = None
                    self._conversations = {}
                self._next_at = now + max(delay, hint, self._rate_floor)
                return
            self._scope, self._conversations, self._at = scope, conversations, now
            self._error = None
            self._failures = 0
            active = any(row["live_status"] == "running" or row["needs_input"] is True for row in conversations.values())
            self._next_at = now + max(ACTIVE_POLL_SECONDS if active else IDLE_POLL_SECONDS, self._rate_floor)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._generation += 1
            self._conversations = {}
            self._at = None


if __name__ == "__main__" and sys.argv[1:] == ["--read-claude-chats"]:
    helper_main()
