"""Read-only status adapter for local Codex rollout logs.

Codex does not publish this storage format as an integration API. This adapter
uses explicit turn lifecycle records, never assistant text or inactivity as proof
of completion. It does not start, resume, modify, or send anything to Codex.
"""
from __future__ import annotations

import hashlib
import copy
import json
import math
import mmap
import sqlite3
import stat as stat_module
import os
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from types import SimpleNamespace

from source_access import require_source_path, require_source_directory, source_glob
from claude_source import (_TranscriptBudget, _TranscriptCursor, _transcript_object,
                           _transcript_constant, _transcript_float, _transcript_coverage, TRANSCRIPT_MAX_BUFFER_BYTES,
                           TRANSCRIPT_RECORD_BYTES, TRANSCRIPT_READ_CHUNK)
from local_quota import parse_codex_credits

LOOKBACK_SECONDS = 24 * 60 * 60
STALE_SECONDS = 30 * 60
MAX_TAIL_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
MAX_THREADS = 500
RECENT_TRANSCRIPT_SECONDS = 60
# Reserve a small part of each existing poll budget for older histories. Recent
# appends still get work on every poll, without starving cold history indexing.
BACKGROUND_BUDGET_FRACTION = 1 / 8
# Covers ordinary larger indexes while keeping one cached snapshot per source.
# Attempts still share this byte allowance and the cooperative one-second cap.
_INDEX_COPY_BYTES = 128 * 1024 * 1024
_INDEX_COPY_ATTEMPTS = 2
_INDEX_COPY_SECONDS = 1.0
_INDEX_RETRY_SECONDS = 60.0


class _IndexChanged(ValueError):
    pass


class _IndexCopyTooLarge(ValueError):
    pass


class _IndexSnapshot:
    """A query-only memory database with its pinned source validation."""
    def __init__(self, connection, validate):
        self.connection = connection
        self.validate = validate


def _index_signature(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _index_wal_signature(value):
    return (_index_signature(value), value.st_mode, value.st_uid, value.st_gid,
            value.st_nlink, getattr(value, "st_flags", 0))


def _check_index_sidecars(directory, wal, expected):
    try:
        os.stat("state_5.sqlite-journal", dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise _IndexChanged("The Codex index has a rollback journal.")
    try:
        current = os.stat("state_5.sqlite-wal", dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        if wal is None:
            return
        raise _IndexChanged("The empty Codex index WAL disappeared.") from None
    if (wal is None or _index_wal_signature(current) != expected
            or _index_wal_signature(os.fstat(wal)) != expected):
        raise _IndexChanged("The Codex index WAL changed while being read.")


def _open_empty_index_wal(directory):
    try:
        before = os.stat("state_5.sqlite-wal", dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None, None
    if (not stat_module.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
            or before.st_nlink != 1 or before.st_size != 0):
        raise _IndexChanged("Only a stable owned empty Codex index WAL is supported.")
    wal = os.open("state_5.sqlite-wal", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                  dir_fd=directory)
    try:
        expected = _index_wal_signature(before)
        _check_index_sidecars(directory, wal, expected)
    except BaseException:
        os.close(wal)
        raise
    return wal, expected

def _index_identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
            value.st_nlink, getattr(value, "st_flags", 0))


@contextmanager
def _readonly_index_snapshot(root):
    """Keep a normal SQLite reader open; every scan starts a new transaction."""
    root = require_source_directory(root, root)
    directory = source = connection = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        original_root = os.fstat(directory)
        source = os.open("state_5.sqlite", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=directory)
        original_source = os.fstat(source)
        if (not stat_module.S_ISREG(original_source.st_mode) or original_source.st_uid != os.getuid()
                or original_source.st_nlink != 1):
            raise _IndexChanged("The Codex index is not a supported regular file.")

        def sidecars():
            result = {}
            for name in ("state_5.sqlite-wal", "state_5.sqlite-shm", "state_5.sqlite-journal"):
                try:
                    value = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (not stat_module.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
                        or value.st_nlink != 1):
                    raise _IndexChanged("The Codex index has an unsupported sidecar.")
                result[name] = _index_identity(value)
            return result

        # Reject substituted existing sidecars even if they change during open.
        initial_sidecars = sidecars()
        connection = sqlite3.connect((root / "state_5.sqlite").as_uri() + "?mode=ro", uri=True,
                                     timeout=0.2, check_same_thread=False)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
        # A read-only WAL connection may initialize its shared-memory index.
        connection.execute("PRAGMA schema_version").fetchone()
        expected_sidecars = sidecars()
        if any(expected_sidecars.get(name) != value for name, value in initial_sidecars.items()):
            raise _IndexChanged("The Codex index sidecars changed during open.")

        def validate():
            current_root = root.lstat()
            current = os.stat("state_5.sqlite", dir_fd=directory, follow_symlinks=False)
            if (_index_identity(current) != _index_identity(original_source)
                    or _index_identity(os.fstat(source)) != _index_identity(original_source)
                    or (current_root.st_dev, current_root.st_ino, current_root.st_mode, current_root.st_uid, current_root.st_gid)
                    != (original_root.st_dev, original_root.st_ino, original_root.st_mode,
                        original_root.st_uid, original_root.st_gid)
                    or expected_sidecars != sidecars()):
                raise _IndexChanged("The Codex index identity changed while being read.")
        validate()
        yield _IndexSnapshot(connection, validate)
        validate()
    finally:
        if connection is not None:
            connection.close()
        for descriptor in (source, directory):
            if descriptor is not None:
                os.close(descriptor)


@contextmanager
def _private_index_snapshot(root):
    """Read a stable checkpointed WAL image into memory without source writes.

    Anonymous mmap bounds the input allocation and releases it immediately
    after deserialize copies it. Peak allocation is about two index images,
    plus SQLite/query overhead. No database image is written to a scratch file.
    """
    if not callable(getattr(sqlite3.Connection, "deserialize", None)):
        raise ValueError("In-memory index snapshots are unavailable.")
    root = require_source_directory(root, root)
    directory = source = wal = connection = buffer = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        original_directory = os.fstat(directory)
        # Retain one sidecar baseline across every copy attempt and query.
        wal, wal_signature = _open_empty_index_wal(directory)
        _check_index_sidecars(directory, wal, wal_signature)
        deadline = time.monotonic() + _INDEX_COPY_SECONDS
        remaining = _INDEX_COPY_BYTES

        def stable(before):
            _check_index_sidecars(directory, wal, wal_signature)
            current = os.stat("state_5.sqlite", dir_fd=directory, follow_symlinks=False)
            current_root = root.lstat()
            if (_index_wal_signature(os.fstat(source)) != _index_wal_signature(before)
                    or _index_wal_signature(current) != _index_wal_signature(before)
                    or (current_root.st_dev, current_root.st_ino, current_root.st_mode, current_root.st_uid, current_root.st_gid)
                    != (original_directory.st_dev, original_directory.st_ino, original_directory.st_mode,
                        original_directory.st_uid, original_directory.st_gid)):
                raise _IndexChanged("The Codex index changed while being read.")

        for attempt in range(_INDEX_COPY_ATTEMPTS):
            try:
                _check_index_sidecars(directory, wal, wal_signature)
                if time.monotonic() >= deadline:
                    raise ValueError("The private index read exceeded its cooperative time budget.")
                source = os.open("state_5.sqlite", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
                before = os.fstat(source)
                if before.st_size > _INDEX_COPY_BYTES:
                    raise _IndexCopyTooLarge("The Codex index exceeds the bounded memory snapshot size.")
                if (not stat_module.S_ISREG(before.st_mode) or before.st_nlink != 1
                        or before.st_uid != os.getuid() or not 100 <= before.st_size <= remaining):
                    raise ValueError("The Codex index is not a supported regular file.")
                header = os.pread(source, 100, 0)
                if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x02\x02":
                    raise ValueError("The Codex index is not a checkpointed WAL-mode database.")
                stable(before)
                buffer = mmap.mmap(-1, before.st_size)
                copied = 0
                while copied < before.st_size:
                    if time.monotonic() >= deadline:
                        raise ValueError("The private index read exceeded its cooperative time budget.")
                    stable(before)
                    chunk = os.read(source, min(1024 * 1024, before.st_size - copied))
                    if not chunk:
                        raise _IndexChanged("The Codex index changed while being copied.")
                    buffer[copied:copied + len(chunk)] = chunk
                    copied += len(chunk)
                    remaining -= len(chunk)
                    stable(before)
                del chunk
                stable(before)
                # sqlite3_deserialize requires rollback-format bytes. SQLite's
                # documented conversion changes only this verified memory image;
                # a nonempty WAL or any journal has already failed closed.
                buffer[18:20] = b"\x01\x01"
                connection = sqlite3.connect(":memory:", check_same_thread=False)
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA temp_store = MEMORY")
                connection.deserialize(buffer)
                buffer.close()
                buffer = None
                stable(before)
                if time.monotonic() >= deadline:
                    raise ValueError("The memory index copy exceeded its safety budget.")
                connection.execute("PRAGMA schema_version").fetchone()
                break
            except _IndexChanged:
                if connection is not None:
                    connection.close()
                    connection = None
                if buffer is not None:
                    buffer.close()
                    buffer = None
                if source is not None:
                    os.close(source)
                    source = None
                if attempt + 1 == _INDEX_COPY_ATTEMPTS:
                    raise
        stable(before)
        yield _IndexSnapshot(connection, lambda: stable(before))
        stable(before)
    finally:
        if connection is not None:
            connection.close()
        if buffer is not None:
            buffer.close()
        for descriptor in (source, wal, directory):
            if descriptor is not None:
                os.close(descriptor)


_START = {"task_started", "turn_started"}
_COMPLETE = {"task_complete", "turn_complete", "turn_completed"}
_STOPPED = {"turn_aborted", "task_aborted", "turn_cancelled", "task_cancelled", "turn_failed", "task_failed"}
_WAITING = {"approval_requested", "exec_approval_request", "apply_patch_approval_request", "tool_approval_requested", "request_user_input", "user_input_requested", "elicitation_request"}
_RESUMED = {"approval_resolved", "approval_response", "user_input_received", "elicitation_response"}


def _preview(value: Any) -> str:
    return " ".join(value.split())[:500] if isinstance(value, str) else ""


def _timestamp(value: Any, fallback: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            value = float(value)
            return (value / 1000 if value > 100_000_000_000 else value) if math.isfinite(value) else fallback
        except OverflowError:
            return fallback
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return fallback


def _is_subagent(meta: dict[str, Any]) -> bool:
    source = meta.get("source")
    if isinstance(source, str):
        try:
            source = json.loads(source)
        except (ValueError, TypeError):
            pass
    thread_source = meta.get("thread_source")
    return bool(
        meta.get("parent_thread_id") or meta.get("agent_path") or meta.get("agent_nickname")
        or meta.get("agent_role") or isinstance(thread_source, str) and thread_source in {"subagent", "guardian_review", "review"}
        or isinstance(source, dict) and ("subagent" in source or "parent_thread_id" in source)
    )


@dataclass
class _FileState:
    inode: int = 0
    offset: int = 0
    pending: bytes = b""
    skipping: bool = False
    status: str | None = None
    transition_at: float = 0.0
    started_at: float | None = None
    turn_id: str | None = None
    event_id: str = ""
    detail: str = ""
    waiting_call: str | None = None
    preview: str = ""
    preview_kind: str = ""
    preview_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    metadata_seen: bool = False
    history_checking: bool = False
    history_checking_detail: str = ""
    history_revision: dict[str, Any] | None = None
    local_rate_limits: dict[str, Any] | None = None
    local_rate_limits_at: float = 0.0

    def transition(self, kind: str, payload: dict[str, Any], timestamp: float, *, call_id: str = "") -> None:
        turn_id = payload.get("turn_id") or payload.get("root_turn_id")
        if kind in _START:
            at = _timestamp(payload.get("started_at"), timestamp)
            if at < self.transition_at:
                return
            self.status, self.started_at = "cooking", at
            self.turn_id = str(turn_id) if turn_id else None
            self.waiting_call = None
            self.detail = "Agent is working."
            self.preview, self.preview_kind, self.preview_at = "", "", 0.0
        else:
            at = _timestamp(payload.get("completed_at"), timestamp)
            # A delayed completion from an earlier turn must not finish a newer turn.
            if turn_id and self.turn_id and str(turn_id) != self.turn_id:
                return
            if at < self.transition_at:
                return
            if kind in _COMPLETE:
                if payload.get("status") in ("failed", "cancelled", "interrupted", "error") or payload.get("error"):
                    self.status = "disconnected"
                    self.detail = "Turn stopped before a verified completion."
                else:
                    self.status = "ready"
                    self.detail = "Reply is ready to review."
                    final = _preview(payload.get("last_agent_message"))
                    if final:
                        self.preview, self.preview_kind, self.preview_at = final, "reply", at
                if not self.started_at:
                    self.started_at = _timestamp(payload.get("started_at")) or None
                self.waiting_call = None
            elif kind in _STOPPED:
                self.status = "disconnected"
                self.detail = "Turn stopped before a verified completion."
                self.waiting_call = None
            elif kind in _WAITING:
                self.status = "needs_you"
                self.waiting_call = call_id or None
                self.detail = "Agent is waiting for approval or input."
                self.preview = _preview(payload.get("message") or payload.get("reason")) or self.detail
                self.preview_kind, self.preview_at = "permission", at
            elif kind in _RESUMED:
                self.status = "cooking"
                self.waiting_call = None
                self.detail = "Agent is working."
                self.preview, self.preview_kind, self.preview_at = "", "", 0.0
            else:
                return
            if turn_id:
                self.turn_id = str(turn_id)
        self.transition_at = at
        signature = f"{kind}:{self.turn_id or ''}:{at}:{call_id}"
        self.event_id = hashlib.sha256(signature.encode()).hexdigest()[:24]

    def consume(self, line: bytes) -> None:
        try:
            record = json.loads(line, object_pairs_hook=_transcript_object,
                                parse_constant=_transcript_constant, parse_float=_transcript_float)
        except (ValueError, UnicodeError, RecursionError):
            self.history_checking = True
            return
        if not self.consume_event(record):
            self.history_checking = True

    def consume_event(self, record: Any) -> bool:
        if not isinstance(record, dict):
            return False
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return False
        kind = record.get("type")
        if kind == "session_meta":
            # OpenAI's recorder treats the FIRST session_meta as canonical.
            # Later metadata can be copied from a fork's historical rollout.
            if not self.metadata_seen:
                self.metadata = {key: payload[key] for key in ("id", "cwd", "source", "thread_source", "parent_thread_id", "agent_path", "agent_nickname", "agent_role") if key in payload}
                self.metadata_seen = True
            return True
        timestamp = _timestamp(record.get("timestamp"))
        event = payload.get("type")
        if not isinstance(event, str):
            # Non-event rollout variants (turn_context, compaction, etc.)
            # have no nested event type and do not change lifecycle state.
            return kind not in ("event_msg", "response_item")
        lifecycle = (kind == "event_msg" and event in _START | _COMPLETE | _STOPPED | _WAITING | _RESUMED
                     or kind == "response_item" and event in ("function_call", "function_call_output"))
        if lifecycle and (timestamp <= 0 or any(
                key in payload and payload[key] is not None and not isinstance(payload[key], str)
                for key in ("turn_id", "root_turn_id")) or any(
                key in payload and payload[key] is not None and _timestamp(payload[key]) <= 0
                for key in ("started_at", "completed_at"))):
            return False
        if kind == "event_msg" and event == "token_count" and timestamp >= self.local_rate_limits_at:
            limits = payload.get("rate_limits")
            if isinstance(limits, dict):
                safe = {}
                if "limit_id" in limits:
                    value = limits["limit_id"]
                    safe["limit_id"] = (value if value is None or isinstance(value, str) and len(value) <= 128
                                        else "")
                for window in ("primary", "secondary"):
                    value = limits.get(window)
                    if isinstance(value, dict):
                        safe[window] = {key: value[key] for key in _LIMIT_WINDOW_FIELDS
                                        if key in value and (value[key] is None or
                                            type(value[key]) in (int, float) and _timestamp(value[key], -1) != -1)}
                credits = limits.get("credits")
                if safe.get("limit_id") in (None, "codex") and parse_codex_credits(credits, has_credits_key="has_credits") is not None:
                    safe["credits"] = {key: credits[key] for key in _LIMIT_CREDIT_FIELDS}
                self.local_rate_limits, self.local_rate_limits_at = safe, timestamp
        if kind == "event_msg" and event == "thread_rolled_back":
            self.status = "disconnected"
            self.detail = "Codex changed this chat's history. Waiting for a new turn."
            self.transition_at = timestamp
            self.event_id = hashlib.sha256(f"rollback:{timestamp}".encode()).hexdigest()[:24]
            self.preview, self.preview_kind, self.preview_at = "", "", 0.0
            self.turn_id, self.waiting_call = None, None
            return True
        if (timestamp >= (self.started_at or 0)
                and (not payload.get("turn_id") or not self.turn_id or payload["turn_id"] == self.turn_id)):
            text = ""
            if kind == "response_item" and event == "message" and payload.get("role") == "assistant" and payload.get("phase") == "final":
                content = payload.get("content")
                content = content if isinstance(content, list) else []
                text = " ".join(block.get("text", "") for block in content
                    if isinstance(block, dict) and block.get("type") in ("output_text", "text") and isinstance(block.get("text"), str))
            elif kind == "event_msg" and event == "agent_message" and payload.get("phase") == "final":
                text = payload.get("message")
            if _preview(text):
                self.preview, self.preview_kind, self.preview_at = _preview(text), "reply", timestamp
        if kind == "event_msg" and event in _START | _COMPLETE | _STOPPED | _WAITING | _RESUMED:
            self.transition(event, payload, timestamp, call_id=str(payload.get("call_id") or payload.get("id") or ""))
        elif kind == "response_item":
            # Only direct, explicit user-input tool requests are recognized. Do not
            # search code or text for tool names; they may be examples or commentary.
            if event == "function_call" and payload.get("name") in ("request_user_input", "functions.request_user_input"):
                self.transition("request_user_input", {}, timestamp, call_id=str(payload.get("call_id") or ""))
                try:
                    arguments = json.loads(payload.get("arguments", "{}"))
                    questions = arguments.get("questions", [])
                    question = " ".join(item["question"] for item in questions if isinstance(item, dict) and isinstance(item.get("question"), str))
                    if _preview(question):
                        self.preview = _preview(question)
                except (ValueError, TypeError, AttributeError, RecursionError, UnicodeError):
                    pass
            elif event == "function_call_output" and self.waiting_call and payload.get("call_id") == self.waiting_call:
                self.transition("user_input_received", {}, timestamp)
        return True

    def feed(self, data: bytes) -> None:
        parts = data.split(b"\n")
        for part in parts[:-1]:
            if self.skipping:
                self.skipping = False
            elif len(self.pending) + len(part) <= MAX_RECORD_BYTES:
                self.consume(self.pending + part)
            else:
                self.history_checking = True
            self.pending = b""
        remainder = parts[-1]
        if self.skipping:
            return
        if len(self.pending) + len(remainder) > MAX_RECORD_BYTES:
            self.pending = b""
            self.skipping = True
            self.history_checking = True
        else:
            self.pending += remainder


_LIMIT_WINDOW_FIELDS = {"used_percent": True, "window_minutes": True, "resets_at": True}
_LIMIT_CREDIT_FIELDS = {"balance": "text", "has_credits": "text", "unlimited": "text"}
_CODEX_RECORD_FIELDS = {"type": True, "timestamp": True, "payload": {
    **{key: True for key in ("type", "id", "cwd", "thread_source", "parent_thread_id", "agent_path",
                             "agent_nickname", "agent_role", "turn_id", "root_turn_id", "started_at",
                             "completed_at", "status", "error", "call_id", "role", "phase", "name")},
    "source": {"subagent": False, "parent_thread_id": False},
    "message": "text", "reason": "text", "last_agent_message": "text", "arguments": "text",
    "content": ({"type": True, "text": "text"},),
    "rate_limits": {"limit_id": True, "primary": _LIMIT_WINDOW_FIELDS, "secondary": _LIMIT_WINDOW_FIELDS,
                    "credits": _LIMIT_CREDIT_FIELDS},
}}


class _CodexTranscriptConsumer:
    """Adapt Codex turn state to the shared bounded, verified JSONL cursor."""

    def __init__(self, session_id, captions):
        self.session_id, self.captions = session_id, captions
        self.file = _FileState()
        self.unknown = False
        self.background = SimpleNamespace(identity_count=0, limit=0)

    def consume(self, event, offset):
        if not self.file.consume_event(event):
            self.unknown = True

    def snapshot(self):
        identifier = self.file.metadata.get("id")
        identity_known = (self.file.metadata_seen and isinstance(identifier, str) and 0 < len(identifier) <= 200
                          and not any(ord(char) < 32 or 0xd800 <= ord(char) <= 0xdfff for char in identifier))
        return {"background_history_unknown": self.unknown or not identity_known}


class CodexSource:
    """Poll Codex's local database and append-only rollouts without writing them."""

    def __init__(self, root: Path | None = None, confine_paths: bool = False,
                 snapshot_parent: Path | None = None):
        self.root = Path(root).expanduser() if root else Path.home() / ".codex"
        self.confine_paths = confine_paths
        self.snapshot_parent = Path(snapshot_parent) if snapshot_parent is not None else None
        self._files: dict[Path, _FileState] = {}
        self._cursors: dict[Path, _TranscriptCursor] = {}
        self._cursor_tick = 0
        self._scan_rotation = 0
        self.revision_lookup = None
        self._last_good_scan: float | None = None
        self._indexed_paths: set[Path] = set()
        self._excluded_index_ids: set[str] = set()
        self._thread_limit_reached = False
        self._fallback_limit_reached = False
        self._index_lock = threading.RLock()
        self._cached_index = None
        self._index_retry_at = 0.0
        self._index_error_detail = None
        self._closed = False

    def _drop_index_cache(self):
        cached, self._cached_index = self._cached_index, None
        if cached is not None:
            try:
                cached[1].close()
            except (sqlite3.Error, OSError, ValueError):
                # Invalidation is expected when a pinned source has changed.
                # ExitStack still closes its connection and every descriptor.
                pass

    def close(self):
        """Release memory snapshot data after shutdown or source revocation."""
        with self._index_lock:
            self._closed = True
            self._drop_index_cache()

    def _index_copy_failed(self, error):
        self._index_retry_at = time.monotonic() + _INDEX_RETRY_SECONDS
        self._index_error_detail = (
            "Codex's index exceeds the 128 MiB read-only snapshot limit. Retrying in up to 60 seconds."
            if isinstance(error, _IndexCopyTooLarge) else
            "Codex's read-only index snapshot could not be verified. Retrying in up to 60 seconds.")

    @contextmanager
    def _index_connection(self, database):
        # Both cache types are serialized with close. A normal reader keeps
        # SQLite's WAL coordination; only a verified memory image is immutable.
        with self._index_lock:
            if self._closed:
                raise ValueError("The Codex source is closed.")
            connection = None
            try:
                selected_root = require_source_directory(self.root, self.root)
                if database == self.root / "state_5.sqlite":
                    database = require_source_path(database, self.root)
                expected = selected_root / "state_5.sqlite"
                if database != expected or require_source_path(database, selected_root) != expected:
                    raise ValueError("Only the selected Codex index may be read.")
                if self._cached_index is not None:
                    try:
                        root, _, snapshot, cached_connection = self._cached_index
                        if selected_root != root:
                            raise _IndexChanged("The selected Codex folder changed.")
                        snapshot.validate()
                        connection = cached_connection
                    except (OSError, ValueError):
                        self._drop_index_cache()
                if connection is None:
                    if self._index_retry_at and time.monotonic() < self._index_retry_at:
                        raise ValueError(self._index_error_detail)
                    stack = ExitStack()
                    try:
                        try:
                            snapshot = stack.enter_context(_readonly_index_snapshot(selected_root))
                        except sqlite3.Error as error:
                            code = getattr(error, "sqlite_errorcode", None)
                            if (not self.confine_paths or self.snapshot_parent is None or not isinstance(code, int)
                                    or code & 0xff not in (sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY)):
                                raise
                            try:
                                snapshot = stack.enter_context(_private_index_snapshot(selected_root))
                            except (sqlite3.Error, OSError, ValueError, MemoryError) as copy_error:
                                self._index_copy_failed(copy_error)
                                raise
                        snapshot.validate()
                        connection = snapshot.connection
                        self._cached_index = (selected_root, stack, snapshot, connection)
                    except BaseException:
                        stack.close()
                        raise
                    self._index_retry_at = 0.0
                    self._index_error_detail = None
                yield connection
                if require_source_directory(self.root, self.root) != selected_root:
                    raise _IndexChanged("The selected Codex folder changed.")
                self._cached_index[2].validate()
            except BaseException as error:
                if self._cached_index is not None:
                    self._drop_index_cache()
                    if isinstance(error, (sqlite3.Error, OSError, ValueError, MemoryError)):
                        self._index_copy_failed(error)
                raise

    def _thread_rows(self, since: float) -> tuple[list[dict[str, Any]], str | None]:
        database = self.root / "state_5.sqlite"
        self._indexed_paths = set()
        self._excluded_index_ids = set()
        self._thread_limit_reached = False
        if not self.confine_paths and not database.exists():
            return [], "Codex thread database is not present."
        try:
            # mode=ro and query_only ensure this connection cannot mutate Codex data.
            with self._index_connection(database) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN")
                try:
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
                    if not {"id", "rollout_path", "updated_at"} <= columns:
                        return [], "Codex thread database has an unsupported schema."
                    indexed = {Path(row[0]).expanduser() for row in conn.execute(
                        "SELECT rollout_path FROM threads WHERE updated_at >= ? ORDER BY updated_at DESC LIMIT ?",
                        [int(since), MAX_THREADS * 10],
                    ) if isinstance(row[0], str) and 0 < len(row[0]) <= 4096}
                    excluded = self._excluded_ids(conn, columns)
                    conditions, params = ["updated_at >= ?"], [int(since)]
                    if "archived" in columns:
                        conditions.append("COALESCE(archived, 0) = 0")
                    if "thread_source" in columns:
                        conditions.append("COALESCE(thread_source, '') NOT IN ('subagent', 'guardian_review', 'review')")
                    if "agent_path" in columns:
                        conditions.append("agent_path IS NULL")
                    rows = [dict(row) for row in conn.execute(
                        "SELECT * FROM threads WHERE " + " AND ".join(conditions) + " ORDER BY updated_at DESC LIMIT ?",
                        params + [MAX_THREADS + 1],
                    )]
                    limit_reached = len(rows) > MAX_THREADS
                    rows = [row for row in rows[:MAX_THREADS] if isinstance(row.get("id"), str) and 0 < len(row["id"]) <= 200
                            and isinstance(row.get("rollout_path"), str) and 0 < len(row["rollout_path"]) <= 4096
                            and row["id"] not in excluded and not _is_subagent(row)]
                finally:
                    conn.rollback()
            # Publish only after post-query source validation succeeded. Archive
            # and child exclusions belong to the same index transaction.
            self._indexed_paths, self._excluded_index_ids = indexed, excluded
            self._thread_limit_reached = limit_reached
            return rows, None
        except (sqlite3.Error, OSError, ValueError, MemoryError):
            return [], self._index_error_detail or "Codex thread database could not be read."

    def _rollout_path(self, path: Path) -> Path:
        if self.confine_paths:
            # Apply the same folder boundary to scheduling metadata and reads.
            relative = path.absolute().relative_to(self.root.absolute())
            if (len(relative.parts) < 2 or relative.parts[0] not in {"sessions", "archived_sessions"}
                    or ".." in relative.parts or relative.suffix != ".jsonl"):
                raise ValueError("Unsupported rollout location")
            return require_source_path(path, self.root)
        return path

    def _read(self, path: Path, budget=None, identifier=None) -> tuple[_FileState | None, float, str | None]:
        try:
            cache_key = path
            path = self._rollout_path(path)
            stat = path.stat()
            if not stat_module.S_ISREG(stat.st_mode):
                return None, 0.0, "Codex rollout is not a regular file."
            cursor = self._cursors.get(cache_key)
            if cursor is None:
                revision = self.revision_lookup(identifier) if self.revision_lookup and identifier else None
                cursor = _TranscriptCursor(identifier or path.stem, False, revision,
                                           consumer_factory=_CodexTranscriptConsumer,
                                           record_fields=_CODEX_RECORD_FIELDS, provider="Codex")
                self._cursors[cache_key] = cursor
            self._cursor_tick += 1
            cursor.last_used = self._cursor_tick
            others = sum(item.buffer_size for item in self._cursors.values() if item is not cursor)
            reservation = min(TRANSCRIPT_RECORD_BYTES + TRANSCRIPT_READ_CHUNK, TRANSCRIPT_MAX_BUFFER_BYTES)
            while others > TRANSCRIPT_MAX_BUFFER_BYTES - reservation:
                offenders = [item for item in self._cursors.values() if item is not cursor and item.buffer_size]
                if not offenders:
                    break
                offender = max(offenders, key=lambda item: (item.buffer_size, -item.last_used))
                others -= offender.buffer_size
                offender.discard_uncommitted()
            result = cursor.advance(path, budget or _TranscriptBudget(),
                                    max(0, TRANSCRIPT_MAX_BUFFER_BYTES - others), 0)
            state = copy.deepcopy(cursor.consumer.file)
            state.inode, state.offset = stat.st_ino, cursor.indexed_size
            state.history_checking = result["history_checking"]
            state.history_checking_detail = result.get("history_checking_detail", "")
            state.history_revision = result.get("history_revision")
            self._files[cache_key] = state
            return state, stat.st_mtime, None
        except (OSError, ValueError):
            return None, 0.0, "Codex rollout could not be read."

    def _unindexed_paths(self, now: float) -> list[Path]:
        paths: list[tuple[float, Path]] = []
        self._fallback_limit_reached = False
        # Newly created rollouts can arrive before their SQLite index row. Restrict
        # discovery to two UTC dates and recently modified files, never all history.
        current = datetime.fromtimestamp(now, timezone.utc)
        for day in (current, current - timedelta(days=1)):
            folder = self.root / "sessions" / day.strftime("%Y/%m/%d")
            try:
                matches = (source_glob(self.root, "sessions/" + day.strftime("%Y/%m/%d") + "/*.jsonl")
                           if self.confine_paths else folder.glob("*.jsonl"))
                for path in matches:
                    try:
                        if self.confine_paths:
                            path = require_source_path(path, self.root)
                        modified = path.stat().st_mtime
                    except (OSError, ValueError):
                        continue
                    # Indexed paths already have an owner or an archive/child
                    # exclusion. They must not consume fallback discovery slots
                    # or make that independent coverage limit look exhausted.
                    if modified >= now - LOOKBACK_SECONDS and path not in self._indexed_paths:
                        paths.append((modified, path))
            except OSError:
                continue
        self._fallback_limit_reached = len(paths) > MAX_THREADS
        return [path for _, path in sorted(paths, key=lambda item: item[0], reverse=True)[:MAX_THREADS]]

    def scan(self, now: float, cutoff: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if self._closed:
            return [], {"id": "codex", "name": "Codex", "provider": "codex", "available": False,
                        "detail": "Codex monitoring is stopped.", "observed": 0,
                        "updated_at": now, "last_seen": self._last_good_scan}
        rows, database_error = self._thread_rows(min(cutoff, now - LOOKBACK_SECONDS))
        indexed = self._indexed_paths
        candidates: list[tuple[dict[str, Any], Path]] = [(row, Path(row["rollout_path"]).expanduser()) for row in rows if row.get("rollout_path")]
        # Avoid rediscovering archived/subagent indexed rollouts in the fallback.
        # An indexed row already proves the database works. Fallback is only for
        # files whose metadata can independently prove a user-created session.
        for path in self._unindexed_paths(now):
            if path not in indexed:
                candidates.append(({}, path))
        observations: list[dict[str, Any]] = []
        seen: set[str] = set()
        fallback_positions: dict[str, int] = {}
        indexed_ids = {row["id"] for row in rows}
        unreadable = 0
        priority = []
        background = []
        if candidates:
            # A recently appended chat must get verification work on every
            # poll. Giving a cold history the entire budget on alternate polls
            # made an actively growing chat alternate between verified and
            # checking, even when its full prefix fit within one poll.
            recent = set()
            for _, path in candidates:
                try:
                    info = self._rollout_path(path).lstat()
                    if (stat_module.S_ISREG(info.st_mode)
                            and info.st_mtime >= now - RECENT_TRANSCRIPT_SECONDS):
                        recent.add(path)
                except (OSError, ValueError):
                    continue
            if not recent:
                # The newest index row supplies a startup candidate only when
                # no recently modified history already has priority.
                recent.add(candidates[0][1])
            # Modification time only schedules work; unchanged-prefix and
            # lifecycle checks still decide status. Stable path order keeps
            # both rings fair when provider index recency changes.
            priority = sorted((item for item in candidates if item[1] in recent),
                              key=lambda item: str(item[1]))
            background = sorted((item for item in candidates if item[1] not in recent),
                                key=lambda item: str(item[1]))
            offset = self._scan_rotation % len(priority)
            priority = priority[offset:] + priority[:offset]
            if background:
                offset = self._scan_rotation % len(background)
                background = background[offset:] + background[:offset]
            candidates = priority + background
            self._scan_rotation += 1
        # Discovery and scheduling metadata must not consume the parse budget
        # before any history gets a chance to make progress.
        budget = _TranscriptBudget()
        full_deadline = budget.deadline
        reserved_bytes = 0
        if priority and background:
            reserved_bytes = int(budget.remaining * BACKGROUND_BUDGET_FRACTION)
            budget.remaining -= reserved_bytes
            duration = max(0.0, full_deadline - time.monotonic())
            budget.deadline -= duration * BACKGROUND_BUDGET_FRACTION
        for position, (row, path) in enumerate(candidates):
            if position == len(priority):
                # This is still one global budget. Unused foreground capacity
                # also flows to background work; no deadline is extended.
                budget.remaining += reserved_bytes
                budget.deadline = full_deadline
            if row.get("id") in seen:
                continue
            state, mtime, error = self._read(path, budget, row.get("id"))
            if error or state is None:
                unreadable += 1
                if row.get("id"):
                    seen.add(row["id"])
                    observations.append(self._observation(row, None, now, 0.0, error))
                continue
            metadata = state.metadata
            if _is_subagent(metadata) or _is_subagent(row):
                continue
            if row and metadata.get("id") and str(metadata["id"]) != str(row.get("id")):
                observations.append(self._observation(row, None, now, mtime,
                                    "Codex rollout identity did not match its thread index."))
                continue
            if not row:
                # Only accept explicit root/user metadata. Do not derive identity
                # from arbitrary filenames or read a prompt to invent a title.
                if metadata.get("thread_source") not in (None, "user") or not metadata.get("id"):
                    continue
                if str(metadata["id"]) in indexed_ids:
                    continue  # Only the indexed rollout owns an indexed chat.
                row = {"id": metadata["id"], "cwd": metadata.get("cwd"), "title": "Codex chat"}
                thread_id = str(row["id"])
                if thread_id in fallback_positions:
                    # A delayed or unavailable index leaves no authority for
                    # choosing one physical history over a conflicting copy.
                    # Scan rotation must never alternate a chat's completion.
                    ambiguous = observations[fallback_positions[thread_id]]
                    detail = "Multiple local histories share this chat identity. Its current state could not be verified."
                    ambiguous.update(status="disconnected", detail=detail,
                                     history_checking=True, history_checking_detail=detail,
                                     history_verified=False, event_id="ambiguous:" + thread_id,
                                     turn_id=None, started_at=None, preview="", preview_kind="", preview_at=None)
                    for field in ("history_revision", "local_rate_limits", "local_rate_limits_at"):
                        ambiguous.pop(field, None)
                    continue
                fallback_positions[thread_id] = len(observations)
            thread_id = str(row["id"])
            if thread_id in seen:
                continue
            seen.add(thread_id)
            # The same index snapshot supplies archive and child exclusions,
            # because discovering a raw file must not resurrect an archive.
            observations.append(self._observation(row, state, now, mtime))
        # Fallback must respect archives even when the indexed query excluded them.
        observations = [item for item in observations if item["id"] not in self._excluded_index_ids]
        candidate_paths = {path for _, path in candidates}
        self._files = {path: state for path, state in self._files.items() if path in candidate_paths}
        self._cursors = {path: state for path, state in self._cursors.items() if path in candidate_paths}
        available = bool(self.root.exists() and (not database_error or observations))
        if available:
            self._last_good_scan = now
        detail = "Reading local Codex turn events. Approval detection depends on events recorded by this Codex version."
        if not available:
            detail = database_error or "Codex data is not available."
        elif unreadable:
            detail = f"Reading local Codex turn events; {unreadable} rollout file(s) could not be read."
        elif database_error:
            detail = "Reading recent Codex rollouts. " + database_error
        coverage, coverage_detail = _transcript_coverage(
            observations, ((cursor.consumer.file.metadata.get("id") or cursor.consumer.session_id, cursor)
                           for cursor in self._cursors.values()),
            self._thread_limit_reached or self._fallback_limit_reached)
        if coverage_detail and available:
            detail += " " + coverage_detail
        return observations, {
            "id": "codex", "name": "Codex", "provider": "codex", "available": available,
            "detail": detail, "observed": len(observations), "updated_at": now,
            "last_seen": self._last_good_scan,
            **coverage,
        }

    def _excluded_ids(self, connection=None, columns=None) -> set[str]:
        if connection is not None:
            cols = columns if columns is not None else {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            excluded = ({row[0] for row in connection.execute("SELECT id FROM threads WHERE archived = 1")}
                        if "archived" in cols else set())
            try:
                excluded.update(row[0] for row in connection.execute("SELECT child_thread_id FROM thread_spawn_edges"))
            except sqlite3.Error:
                pass
            return excluded
        database = self.root / "state_5.sqlite"
        if not self.confine_paths and not database.exists():
            return set()
        try:
            if self.confine_paths:
                database = require_source_path(database, self.root)
            with self._index_connection(database) as conn:
                return self._excluded_ids(conn)
        except (sqlite3.Error, OSError, ValueError):
            return set()

    @staticmethod
    def _observation(row: dict[str, Any], state: _FileState | None, now: float, mtime: float, error: str | None = None) -> dict[str, Any]:
        thread_id = str(row["id"])
        last_activity = min(now, max(mtime, _timestamp(row.get("updated_at_ms") or row.get("updated_at")), state.transition_at if state else 0.0))
        status = state.status if state and state.status else "disconnected"
        detail = state.detail if state and state.status else error or "No supported turn lifecycle event was found in the recent log."
        updated_at = state.transition_at if state and state.transition_at else last_activity
        event_id = state.event_id if state and state.event_id else f"unknown:{thread_id}"
        checking = bool(state and state.history_checking)
        if checking:
            status = "disconnected"
            detail = state.history_checking_detail or "Checking local Codex history before confirming its status."
        if status == "cooking" and now - last_activity > STALE_SECONDS:
            status = "disconnected"
            detail = "No recent activity; last known state was working. The agent may still be running."
            event_id += ":stale"
            updated_at = max(updated_at, last_activity + STALE_SECONDS)
        preview_kind = "reply" if status == "ready" else "permission" if status == "needs_you" else ""
        return {
            "key": "codex:" + thread_id, "id": thread_id, "provider": "codex",
            "title": str(row.get("name") or row.get("title") or "Codex chat")[:160],
            "cwd": str(row.get("cwd") or "")[:2048], "status": status,
            "updated_at": updated_at, "last_activity_at": last_activity,
            "started_at": state.started_at if state else None,
            "detail": detail, "can_open": True, "open_kind": "chat", "open_label": "Open Codex", "event_id": event_id,
            "turn_id": state.turn_id if state else None,
            "history_checking": checking,
            "history_checking_detail": state.history_checking_detail if state else "",
            "history_verified": bool(state and state.history_revision and not checking),
            **({"history_revision": state.history_revision} if state and state.history_revision and not checking else {}),
            **({"local_rate_limits": state.local_rate_limits, "local_rate_limits_at": state.local_rate_limits_at}
               if state and state.local_rate_limits is not None and not checking else {}),
            "preview": state.preview if state and state.preview_kind == preview_kind else "",
            "preview_kind": preview_kind,
            "preview_at": state.preview_at if state and state.preview_kind == preview_kind else None,
        }
