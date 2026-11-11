"""Bounded status and recent-reply reads directly from the signed-in provider.

``ChatGPTStatusReader(codex_root).scan(ids, now=None)`` returns a dict keyed by
canonical conversation UUID. Each value contains ``status`` (the provider enum),
``observed_at`` (local receipt time), ``fresh`` (at most 15 seconds old), and
``error``/``http_status`` (fixed local diagnostics, never response text).

scan never waits for the network. Two daemon workers drain a fair queue. Metadata
discovery runs every ten seconds, including when no local IDs exist. Due streaming
conversations and fresh activity hints are checked next, then those least recently
attempted. Every fourth status dispatch reserves a slot for work waiting 30 seconds;
input order breaks ties. COMPLETE is only an observation, never a new completion
event: callers must preserve their own conversation/event baseline.

An explicit ``reply_candidates={id: metadata_revision}`` opts recent/changed
conversations into the latest-turn reader after COMPLETE. Its response may
contain text, but only a current terminal assistant marker leaves that reader.
``completed_replies()`` exposes fresh markers; ``ack_completed_replies()`` is
called after durable application so a transient consumer failure remains retryable.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import http.client
import itertools
import json
import math
import os
from pathlib import Path
import ssl
import stat
import re
import threading
import time
from typing import Iterable
import urllib.error
import urllib.request
import uuid

from privacy_network import ProviderRequestError, RejectRedirects
from chatgpt_reply_source import read_last_reply

_STATUSES = frozenset({"IS_STREAMING", "COMPLETE", "FAILURE", "UNAVAILABLE"})
_ORIGIN = "https://chatgpt.com"
_CATALOG_JOB = "conversation-metadata"
_LIST_ROUTE = "/backend-api/conversations?offset=0&limit=100&order=updated&expand=false&hide_snorlax=false&is_archived=false"
_STREAM_ROUTE = re.compile(r"/backend-api/conversation/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/stream_status\Z")


def _canonical_id(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        result = str(uuid.UUID(value))
    except ValueError:
        return None
    return result if result == value.lower() else None


def _bounded_credential(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 16384 and all(32 < ord(c) < 127 for c in value)


def _read_credentials(codex_root: Path) -> tuple[str, str | None] | None:
    """Use the existing Codex OAuth login; do not refresh or inspect browser cookies."""
    descriptor = None
    try:
        descriptor = os.open(codex_root / "auth.json", os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not 0 < info.st_size <= 128 * 1024:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            return None
        data = json.loads(raw)
        tokens = data.get("tokens") if isinstance(data, dict) else None
        if not isinstance(tokens, dict) or not _bounded_credential(tokens.get("access_token")):
            return None
        account = tokens.get("account_id")
        if account is not None and not _bounded_credential(account):
            return None
        return tokens["access_token"], account
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_json(route: str, token: str, account_id: str | None, *, timeout: float, max_bytes: int):
    if ((route != _LIST_ROUTE and not _STREAM_ROUTE.fullmatch(route)) or not _bounded_credential(token)
            or (account_id is not None and not _bounded_credential(account_id))
            or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 10
            or type(max_bytes) is not int or not 0 < max_bytes <= 1024 * 1024):
        raise ProviderRequestError("request")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": "AgentBoard/0.2.5"}
    if account_id is not None:
        headers["ChatGPT-Account-ID"] = account_id
    request = urllib.request.Request(_ORIGIN + route, method="GET", headers=headers)
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), RejectRedirects(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ProviderRequestError("oversized")
        return json.loads(raw)
    except ProviderRequestError:
        raise
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise ProviderRequestError("http", status) from None
    except (OSError, ValueError, UnicodeError, RecursionError, urllib.error.URLError, http.client.HTTPException):
        raise ProviderRequestError("unavailable") from None


def read_stream_status(conversation_id: str, token: str, account_id: str | None, *, timeout: float = 8) -> str:
    """GET one fixed first-party route, accepting only the observed provider enum."""
    identifier = _canonical_id(conversation_id)
    if identifier is None:
        raise ProviderRequestError("request")
    data = _read_json("/backend-api/conversation/" + identifier + "/stream_status",
                      token, account_id, timeout=timeout, max_bytes=16384)
    status = data.get("status") if isinstance(data, dict) else None
    if not isinstance(status, str) or status not in _STATUSES:
        raise ProviderRequestError("response")
    return status


def _provider_time(value, now):
    if isinstance(value, str) and len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            value = parsed.timestamp() if parsed.tzinfo is not None else None
        except (ValueError, OverflowError, OSError):
            return None
    return float(value) if type(value) in (int, float) and math.isfinite(value) and 0 < value <= now + 2 else None


def read_conversation_metadata(token: str, account_id: str | None, *, timeout: float = 8) -> dict[str, dict]:
    """Read only the first 100 recent conversation summaries, with expansion off."""
    data = _read_json(_LIST_ROUTE, token, account_id, timeout=timeout, max_bytes=1024 * 1024)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list) or len(data["items"]) > 100:
        raise ProviderRequestError("response")
    now = time.time()
    result = {}
    for item in data["items"]:
        if (not isinstance(item, dict) or item.get("is_archived") is not False
                or item.get("is_temporary_chat") is True):
            continue
        # An endpoint/schema change must not quietly turn this reader into a
        # transcript collector, even if the requested expand=false is ignored.
        if item.get("mapping") is not None or item.get("messages") is not None:
            raise ProviderRequestError("response")
        identifier = _canonical_id(item.get("id"))
        title = item.get("title")
        updated = _provider_time(item.get("update_time"), now)
        if (identifier is None or not isinstance(title, str) or len(title) > 4096 or updated is None
                or any(0xd800 <= ord(char) <= 0xdfff for char in title)):
            continue
        fields = {"title": " ".join(title.split())[:160] or "ChatGPT chat",
                  "source_created_at": _provider_time(item.get("create_time"), now),
                  "source_updated_at": updated}
        status = item.get("async_status")
        if type(status) is int and 1 <= status <= 7:
            fields["async_status"] = status
        reply_at = _provider_time(item.get("latest_assistant_turn_created_at"), now)
        if reply_at is not None:
            fields["latest_assistant_turn_created_at"] = reply_at
        if identifier in result:
            raise ProviderRequestError("response")
        result[identifier] = fields
    return result


class ChatGPTStatusReader:
    def __init__(self, codex_root: Path | str, *, request=read_stream_status,
                 metadata_request=read_conversation_metadata, reply_request=read_last_reply, clock=time.time):
        self.codex_root = Path(codex_root)
        self._request = request
        self._metadata_request = metadata_request
        self._reply_request = reply_request
        self._clock = clock
        self._lock = threading.Lock()
        self._credentials: tuple[str, str | None] | None = None
        self._fingerprint: bytes | None = None
        self._account_identity: str | None = None
        self._generation = 0
        self._cache: dict[str, dict] = {}
        self._discovered: dict[str, dict] = {}
        self._discovered_at: float | None = None
        self._reply_candidates: dict[str, float] = {}
        self._replies: dict[str, dict] = {}
        self._reply_revisions: dict[str, float] = {}
        self._reply_next_poll: dict[str, float] = {}
        self._reply_acks: dict[str, str] = {}
        self._reply_empty_attempts: dict[str, int] = {}
        self._last_attempt: dict[str, float] = {}
        self._first_requested: dict[str, float] = {}
        self._next_poll: dict[str, float] = {}
        self._queue: list[str] = []
        self._inflight: set[tuple[int, str]] = set()
        self._workers = 0
        self._status_dispatches = 0
        self._backoff_until = 0.0
        self._closed = False

    def scan(self, conversation_ids: Iterable[str], now: float | None = None,
             *, reply_candidates: dict[str, float] | None = None) -> dict[str, dict]:
        now = self._clock() if now is None else now
        ids = list(dict.fromkeys(identifier for value in itertools.islice(conversation_ids, 512)
                                 if (identifier := _canonical_id(value)) is not None))[:256]
        credentials = _read_credentials(self.codex_root)
        fingerprint = (hashlib.sha256(json.dumps(credentials).encode()).digest() if credentials else None)
        with self._lock:
            if self._closed:
                return {}
            if fingerprint != self._fingerprint:
                self._generation += 1
                self._fingerprint = fingerprint
                self._cache.clear()
                self._discovered.clear()
                self._discovered_at = None
                self._reply_candidates.clear()
                self._replies.clear()
                self._reply_revisions.clear()
                self._reply_next_poll.clear()
                self._reply_acks.clear()
                self._reply_empty_attempts.clear()
                self._last_attempt.clear()
                self._first_requested.clear()
                self._next_poll.clear()
                self._queue.clear()
                self._backoff_until = 0
                self._status_dispatches = 0
                if credentials is not None:
                    # Token refresh keeps the account scope. Without an account
                    # ID, do not claim continuity across a credential change.
                    scope = credentials[1] if credentials[1] is not None else str(uuid.uuid4())
                    self._account_identity = hashlib.sha256(scope.encode()).hexdigest()
            self._credentials = credentials
            if credentials is None:
                return {identifier: {"status": "UNAVAILABLE", "observed_at": now, "fresh": False,
                                     "error": "credentials", "http_status": None} for identifier in ids}
            # Drop deselected work, and retain only the bounded requested cache.
            wanted = set(ids) | {_CATALOG_JOB}
            for mapping in [self._cache, self._last_attempt, self._first_requested, self._next_poll,
                            self._replies, self._reply_revisions, self._reply_next_poll,
                            self._reply_acks, self._reply_empty_attempts]:
                for identifier in mapping.keys() - wanted:
                    del mapping[identifier]
            candidates = {identifier: revision for identifier, revision in (reply_candidates or {}).items()
                          if identifier in wanted and identifier != _CATALOG_JOB
                          and type(revision) in (int, float) and math.isfinite(revision) and 0 < revision <= now + 2}
            for identifier, revision in candidates.items():
                if self._reply_candidates.get(identifier) != revision:
                    self._replies.pop(identifier, None)
                    self._reply_revisions.pop(identifier, None)
                    self._reply_next_poll.pop(identifier, None)
                    self._reply_acks.pop(identifier, None)
                    self._reply_empty_attempts.pop(identifier, None)
            self._reply_candidates = candidates
            for identifier in ids:
                self._first_requested.setdefault(identifier, now)
            order = {identifier: index for index, identifier in enumerate(ids)}
            hints = set()
            if self._discovery_fresh(now):
                for identifier in ids:
                    metadata = self._discovered.get(identifier, {})
                    attempted = self._last_attempt.get(identifier)
                    if (attempted is None or attempted < self._discovered_at) and (
                            metadata.get("async_status") == 3 or
                            attempted is not None and metadata.get("source_updated_at", 0) > attempted):
                        hints.add(identifier)
            due = [identifier for identifier in ids
                   if (self._generation, identifier) not in self._inflight
                   and (self._next_poll.get(identifier, 0) <= now or
                        self._reply_due(identifier, now) and self._cache.get(identifier, {}).get("status") == "COMPLETE" or
                        identifier in hints and self._cache.get(identifier, {}).get("status") in {"COMPLETE", "FAILURE"})]
            due.sort(key=lambda identifier: (
                0 if self._cache.get(identifier, {}).get("status") == "IS_STREAMING"
                else 1 if identifier in hints or self._reply_due(identifier, now) else 2,
                self._last_attempt.get(identifier, 0), order[identifier]))
            if ((self._generation, _CATALOG_JOB) not in self._inflight
                    and self._next_poll.get(_CATALOG_JOB, 0) <= now):
                due.insert(0, _CATALOG_JOB)
            self._queue = due if now >= self._backoff_until else []
            while self._queue and self._workers < 2:
                self._workers += 1
                threading.Thread(target=self._work, daemon=True, name="agent-board-chatgpt-status").start()
            return {identifier: {**reading, "fresh": 0 <= now - reading["observed_at"] <= 15}
                    for identifier in ids if (reading := self._cache.get(identifier)) is not None}

    def discovery_available(self, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        with self._lock:
            return self._discovery_fresh(now)

    def _reply_due(self, identifier: str, now: float) -> bool:
        revision = self._reply_candidates.get(identifier)
        if revision is None or self._reply_next_poll.get(identifier, 0) > now:
            return False
        if self._reply_revisions.get(identifier) != revision:
            return True
        reading = self._replies.get(identifier)
        return reading is not None and self._reply_acks.get(identifier) != reading["reply_id"]

    def ack_completed_replies(self, markers: dict[str, dict]) -> None:
        """Consume only markers the caller has durably applied and checkpointed."""
        with self._lock:
            for identifier, marker in markers.items():
                reading = self._replies.get(identifier)
                if (reading is not None and isinstance(marker, dict)
                        and all(reading[field] == marker.get(field) for field in ("reply_id", "revision", "account_scope"))
                        and reading["revision"] == self._reply_candidates.get(identifier)
                        and reading["account_scope"] == self._account_identity):
                    self._reply_acks[identifier] = reading["reply_id"]

    def completed_replies(self, now: float | None = None) -> dict[str, dict]:
        """Fresh allowlisted current-leaf markers, scoped to the signed-in account."""
        now = self._clock() if now is None else now
        with self._lock:
            if self._closed or self._credentials is None:
                return {}
            return {identifier: {**reading, "fresh": True} for identifier, reading in self._replies.items()
                    if 0 <= now - reading["observed_at"] <= 15
                    and self._reply_candidates.get(identifier) == reading["revision"]
                    and reading["account_scope"] == self._account_identity
                    and self._cache.get(identifier, {}).get("status") == "COMPLETE"}

    def account_identity(self) -> str | None:
        """Opaque account scope, excluding tokens and retained through a brief outage."""
        with self._lock:
            return self._account_identity

    def _discovery_fresh(self, now: float) -> bool:
        return (not self._closed and self._credentials is not None and self._discovered_at is not None
                and 0 <= now - self._discovered_at <= 30)

    def discovered_chats(self, now: float | None = None) -> dict[str, dict]:
        """Fresh exact IDs and sanitized metadata; no response bodies or tokens."""
        now = self._clock() if now is None else now
        with self._lock:
            return {identifier: dict(fields) for identifier, fields in self._discovered.items()} if self._discovery_fresh(now) else {}

    def _work(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._closed or not self._queue or self._credentials is None:
                        return
                    index = 0
                    if self._queue[0] != _CATALOG_JOB and self._status_dispatches % 4 == 3:
                        # Most slots favor live work. A reserved aged slot keeps
                        # old history moving even with continuous active hints.
                        oldest = min((i for i, ident in enumerate(self._queue) if ident != _CATALOG_JOB),
                                     key=lambda i: self._last_attempt.get(self._queue[i], self._first_requested[self._queue[i]]))
                        candidate = self._queue[oldest]
                        waiting_since = self._last_attempt.get(candidate, self._first_requested[candidate])
                        if self._clock() - waiting_since >= 30:
                            index = oldest
                    identifier = self._queue.pop(index)
                    if identifier != _CATALOG_JOB:
                        self._status_dispatches += 1
                    generation = self._generation
                    credentials = self._credentials
                    self._inflight.add((generation, identifier))
                    self._last_attempt[identifier] = self._clock()
                    reply_revision = (self._reply_candidates.get(identifier)
                                      if self._reply_due(identifier, self._clock()) else None)
                status, error, http_status = "UNAVAILABLE", None, None
                metadata = None
                reply, reply_error, reply_http_status = None, None, None
                try:
                    if identifier == _CATALOG_JOB:
                        metadata = self._metadata_request(*credentials)
                        if not isinstance(metadata, dict) or len(metadata) > 100:
                            raise ProviderRequestError("response")
                    else:
                        status = self._request(identifier, *credentials)
                        if status not in _STATUSES:
                            raise ProviderRequestError("response")
                        if status == "COMPLETE" and reply_revision is not None:
                            try:
                                reply = self._reply_request(identifier, *credentials)
                                if reply is not None and not self._valid_reply(reply):
                                    raise ProviderRequestError("response")
                            except ProviderRequestError as failure:
                                reply_error, reply_http_status = failure.reason, failure.status
                            except Exception:
                                reply_error = "unavailable"
                except ProviderRequestError as failure:
                    status, error, http_status = "UNAVAILABLE", failure.reason, failure.status
                except Exception:
                    status, error = "UNAVAILABLE", "unavailable"
                observed_at = self._clock()
                with self._lock:
                    self._inflight.discard((generation, identifier))
                    if generation != self._generation or self._closed:
                        continue
                    if identifier == _CATALOG_JOB:
                        if error is None:
                            self._discovered = metadata
                            self._discovered_at = observed_at
                        interval = 10 if error is None else 30
                    else:
                        self._cache[identifier] = {"status": status, "observed_at": observed_at,
                                                   "error": error, "http_status": http_status}
                        interval = 4 if status == "IS_STREAMING" else 12 if status in {"COMPLETE", "FAILURE"} else 30
                        if status == "IS_STREAMING":
                            self._replies.pop(identifier, None)
                            self._reply_revisions.pop(identifier, None)
                            self._reply_next_poll.pop(identifier, None)
                            self._reply_acks.pop(identifier, None)
                            self._reply_empty_attempts.pop(identifier, None)
                        elif status == "COMPLETE" and reply_revision is not None and self._reply_candidates.get(identifier) == reply_revision:
                            if reply_error is None and reply is not None:
                                self._replies[identifier] = {"reply_id": reply["reply_id"],
                                    "created_at": reply["created_at"], "updated_at": reply["updated_at"],
                                    "observed_at": observed_at, "revision": reply_revision,
                                    "account_scope": self._account_identity}
                                self._reply_revisions[identifier] = reply_revision
                                self._reply_empty_attempts.pop(identifier, None)
                            else:
                                self._replies.pop(identifier, None)
                                if reply_error is None:
                                    count = self._reply_empty_attempts.get(identifier, 0) + 1
                                    self._reply_empty_attempts[identifier] = count
                                    if count >= 3:
                                        self._reply_revisions[identifier] = reply_revision
                            self._reply_next_poll[identifier] = observed_at + 30
                    self._next_poll[identifier] = observed_at + interval
                    if http_status in {401, 429} or (http_status == 403 and identifier == _CATALOG_JOB) or reply_http_status in {401, 429}:
                        self._backoff_until = observed_at + (60 if 429 in {http_status, reply_http_status} else 30)
                        self._queue.clear()
        finally:
            with self._lock:
                self._workers -= 1

    def _valid_reply(self, reply) -> bool:
        return (isinstance(reply, dict) and isinstance(reply.get("reply_id"), str)
                and "updated_at" in reply
                and re.fullmatch(r"[0-9a-f]{64}", reply["reply_id"]) is not None
                and type(reply.get("created_at")) in (int, float) and math.isfinite(reply["created_at"])
                and 0 < reply["created_at"] <= self._clock() + 2
                and (reply.get("updated_at") is None or (
                    type(reply["updated_at"]) in (int, float) and math.isfinite(reply["updated_at"])
                    and reply["created_at"] <= reply["updated_at"] <= self._clock() + 2)))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._queue.clear()
            self._cache.clear()
            self._discovered.clear()
            self._discovered_at = None
            self._replies.clear()
            self._reply_candidates.clear()
            self._reply_revisions.clear()
            self._reply_next_poll.clear()
            self._reply_acks.clear()
            self._reply_empty_attempts.clear()
            self._credentials = None
