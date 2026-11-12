#!/usr/bin/env python3
"""Create and advance synthetic Agent Board review samples in a clean home.

Requires Python 3.11+ with sqlite3 serialization support. The CLI always uses
the current user's home. Tests may pass a disposable home to the Python API.
Existing provider folders are never adopted, merged, overwritten or removed.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time
import uuid


ROOTS = (".codex", ".claude", "Library/Application Support/Claude")
GRANT_ROOTS = (".codex", ".claude", "Library/Application Support/Claude/claude-code-sessions")
CONTROL = ".agent-board-review-samples.jsonl"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_CONTROL_BYTES = 256 * 1024
PROVIDERS = ("codex", "claude")
FORMAT = "agent-board-synthetic-review-v1"


class SampleError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SampleError("A sample-control field was duplicated.")
            result[key] = value
        return result

    def invalid(_value):
        raise SampleError("A sample-control number is invalid.")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def _write_all(descriptor, raw):
    while raw:
        written = os.write(descriptor, raw)
        if written <= 0:
            raise OSError("The synthetic sample write did not finish.")
        raw = raw[written:]


class _Home:
    """Anchor filesystem operations to an owned home and reject child links."""

    def __init__(self, home):
        if os.geteuid() == 0:
            raise SampleError("Do not run review samples as root or with sudo.")
        self.path = Path(home).resolve(strict=True)
        if self.path == Path("/"):
            raise SampleError("A regular user home is required.")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        self.descriptor = os.open(self.path, flags)
        if os.fstat(self.descriptor).st_uid != os.getuid():
            os.close(self.descriptor)
            raise SampleError("The sample home must belong to the current user.")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        os.close(self.descriptor)

    @contextmanager
    def parent(self, relative, create=False):
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(part in (".", "..") for part in parts):
            raise SampleError("A sample path is invalid.")
        opened = []
        descriptor = self.descriptor
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    dir_fd=descriptor)
                opened.append(child)
                descriptor = child
            yield descriptor, parts[-1]
        finally:
            for child in reversed(opened):
                os.close(child)

    def probe(self, relative):
        try:
            with self.parent(relative) as (parent, name):
                return os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def mkdir(self, relative):
        with self.parent(relative, create=True) as (parent, name):
            os.mkdir(name, mode=0o700, dir_fd=parent)

    @staticmethod
    def metadata(descriptor, limit=MAX_FILE_BYTES):
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                or before.st_size > limit):
            raise SampleError("A sample file was replaced, enlarged or redirected. No files were overwritten.")
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if len(raw) > limit or signature(before) != signature(after):
            raise SampleError("A sample file changed during verification. Retry after other sample commands stop.")
        return {"device": before.st_dev, "inode": before.st_ino, "bytes": len(raw),
                "sha256": digest.hexdigest()}, bytes(raw)

    def read(self, relative, limit=MAX_FILE_BYTES):
        with self.parent(relative) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=parent)
            try:
                return self.metadata(descriptor, limit)
            finally:
                os.close(descriptor)

    def create(self, relative, raw):
        with self.parent(relative, create=True) as (parent, name):
            descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 0o600, dir_fd=parent)
            try:
                _write_all(descriptor, raw)
                os.fsync(descriptor)
                return self.metadata(descriptor)[0]
            finally:
                os.close(descriptor)

    def append(self, relative, raw, expected, limit=MAX_FILE_BYTES):
        with self.parent(relative) as (parent, name):
            descriptor = os.open(name, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=parent)
            try:
                metadata, _ = self.metadata(descriptor, limit)
                if metadata != expected or metadata["bytes"] + len(raw) > limit:
                    raise SampleError("The sample pack changed outside this tool or reached its size limit. Nothing was appended.")
                _write_all(descriptor, raw)
                os.fsync(descriptor)
                return self.metadata(descriptor, limit)[0]
            finally:
                os.close(descriptor)

    @contextmanager
    def locked(self):
        with self.parent(CONTROL) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=parent)
            try:
                self.metadata(descriptor, MAX_CONTROL_BYTES)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise SampleError("Another review-sample command is running. Wait for it to finish.") from None
                yield
            finally:
                os.close(descriptor)

    def check_tree(self, expected_files, root_ids):
        allowed_directories = set()
        for relative in expected_files:
            allowed_directories.update(str(parent) for parent in Path(relative).parents if str(parent) != ".")

        def visit(relative):
            with self.parent(relative) as (parent, name):
                descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                     dir_fd=parent)
                try:
                    info = os.fstat(descriptor)
                    if relative in root_ids and [info.st_dev, info.st_ino] != root_ids[relative]:
                        raise SampleError("A generated provider folder was replaced. No sample events were appended.")
                    for child in os.listdir(descriptor):
                        path = relative + "/" + child
                        info = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode) and path in allowed_directories:
                            visit(path)
                        elif not stat.S_ISREG(info.st_mode) or path not in expected_files:
                            raise SampleError("A provider folder contains files not created by this sample pack. No merge is allowed.")
                finally:
                    os.close(descriptor)

        for relative in ROOTS:
            visit(relative)


def _paths(codex_id, claude_id, created):
    day = datetime.fromtimestamp(created, timezone.utc).strftime("%Y/%m/%d")
    return {
        "codex_log": ".codex/sessions/" + day + "/rollout-" + codex_id + ".jsonl",
        "database": ".codex/state_5.sqlite",
        "claude_log": ".claude/projects/-agent-board-review/" + claude_id + ".jsonl",
        "registry": ".claude/sessions/agent-board-review.json",
        "desktop": "Library/Application Support/Claude/claude-code-sessions/agent-board-review/agent-board-review/local_" + claude_id + ".json",
        "quota": ".claude/agent-board/usage/quota-v1-a.json",
    }


def _stamp(value=None):
    value = time.time() if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise SampleError("The synthetic event time is invalid.")
    return float(value)


def _start(provider, identifier, turn, stamp):
    if provider == "codex":
        return {"type": "event_msg", "timestamp": stamp, "payload": {
            "type": "task_started", "turn_id": turn, "started_at": stamp}}
    return {"type": "user", "sessionId": identifier, "uuid": turn, "timestamp": stamp,
            "cwd": "/synthetic/agent-board-review", "message": {"content": "Inspect the synthetic review sample"}}


def _event(provider, identifier, turn, action, stamp):
    if action == "cooking":
        return _start(provider, identifier, turn, stamp)
    if provider == "codex":
        if action == "needs-you":
            return {"type": "response_item", "timestamp": stamp, "payload": {
                "type": "function_call", "name": "request_user_input", "call_id": turn + "-question",
                "arguments": _json({"questions": [{"question": "Which synthetic sample option should we use?"}]}).decode()}}
        return {"type": "event_msg", "timestamp": stamp, "payload": {
            "type": "task_complete", "turn_id": turn, "completed_at": stamp,
            "last_agent_message": "The synthetic review sample is ready."}}
    content = [{"type": "tool_use", "name": "AskUserQuestion", "id": turn + "-question",
                "input": {"questions": [{"question": "Which synthetic sample option should we use?"}]}}] if action == "needs-you" else "The synthetic review sample is ready."
    return {"type": "assistant", "sessionId": identifier, "uuid": turn + "-" + action,
            "timestamp": stamp, "cwd": "/synthetic/agent-board-review", "message": {
                "stop_reason": "tool_use" if action == "needs-you" else "end_turn", "content": content}}


def initialize(home, *, now=None):
    """Create fresh sample roots. Explicit homes are for isolated tests only."""
    if sys.version_info < (3, 11) or not hasattr(sqlite3.Connection, "serialize"):
        raise SampleError("Python 3.11 or newer with sqlite3 serialization support is required.")
    stamp = _stamp(now)
    codex_id, claude_id = str(uuid.uuid4()), str(uuid.uuid4())
    paths = _paths(codex_id, claude_id, stamp)
    with _Home(home) as access:
        if any(access.probe(relative) is not None for relative in (*ROOTS, CONTROL)):
            raise SampleError("A provider folder or previous sample marker already exists. Use a clean local test user; nothing was changed.")
        # Build the database in memory. Its one absolute rollout path belongs
        # to this user's generated tree, never to the developer's fixture home.
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,updated_at INTEGER,title TEXT,cwd TEXT,archived INTEGER,thread_source TEXT)")
            connection.execute("INSERT INTO threads VALUES(?,?,?,?,?,0,'user')", (
                codex_id, str(access.path / paths["codex_log"]), int(stamp),
                "Codex review sample", "/synthetic/agent-board-review"))
            database = connection.serialize()
        state = {provider: {"turn": 1, "turn_id": "review-" + provider + "-1", "status": "cooking"}
                 for provider in PROVIDERS}
        header = {"format": FORMAT, "phase": "initializing", "home": str(access.path),
                  "created_at": stamp, "ids": {"codex": codex_id, "claude": claude_id}}
        marker = access.create(CONTROL, _json(header) + b"\n")
        for relative in ROOTS:
            access.mkdir(relative)
        quota_payload = _json({"provider": "claude", "observed_at_ms": int(stamp * 1000), "windows": [
            {"id": "five_hour", "used_basis_points": 1800, "reset_at": int(stamp) + 3600},
            {"id": "weekly", "used_basis_points": 900, "reset_at": int(stamp) + 6 * 86400}]})
        contents = {
            paths["database"]: database,
            paths["codex_log"]: b"\n".join(_json(record) for record in [
                {"type": "session_meta", "payload": {"id": codex_id, "source": "vscode", "thread_source": "user",
                                                     "cwd": "/synthetic/agent-board-review"}},
                _start("codex", codex_id, state["codex"]["turn_id"], stamp),
                {"type": "event_msg", "timestamp": stamp, "payload": {"type": "token_count", "rate_limits": {
                    "limit_id": "codex", "secondary": {"used_percent": 27, "window_minutes": 10080,
                                                          "resets_at": int(stamp) + 6 * 86400}}}},
            ]) + b"\n",
            paths["claude_log"]: _json(_start("claude", claude_id, state["claude"]["turn_id"], stamp)) + b"\n",
            paths["registry"]: _json({"sessionId": claude_id, "updatedAt": stamp * 1000,
                                       "statusUpdatedAt": stamp * 1000, "cwd": "/synthetic/agent-board-review"}),
            paths["desktop"]: _json({"sessionId": "local_" + claude_id, "cliSessionId": claude_id,
                                      "title": "Claude review sample", "createdAt": stamp * 1000,
                                      "cwd": "/synthetic/agent-board-review", "isArchived": False}),
            paths["quota"]: _json({"version": 1, "payload": quota_payload.decode(),
                                    "sha256": hashlib.sha256(quota_payload).hexdigest()}),
        }
        files = {relative: access.create(relative, raw) for relative, raw in contents.items()}
        roots = {relative: [access.probe(relative).st_dev, access.probe(relative).st_ino] for relative in ROOTS}
        record = {"phase": "initialized", "roots": roots, "files": files, "state": state, "last_stamp": stamp}
        access.append(CONTROL, _json(record) + b"\n", marker, MAX_CONTROL_BYTES)
        return {"synthetic": True, "home": str(access.path), "folders": [str(access.path / root) for root in GRANT_ROOTS],
                "state": state, "provider_requests": 0, "note": "Only synthetic samples were created. No real chat links exist."}


def _load(access):
    marker, raw = access.read(CONTROL, MAX_CONTROL_BYTES)
    if not raw.endswith(b"\n"):
        raise SampleError("The sample journal has an unfinished write. It will not be repaired or overwritten.")
    records = [_decode(line) for line in raw.splitlines()]
    if len(records) < 2:
        raise SampleError("Sample initialization did not finish. Its files are preserved; use a clean local test user.")
    header, current = records[0], records[-1]
    if (header.get("format") != FORMAT or header.get("home") != str(access.path)
            or records[1].get("phase") != "initialized" or current.get("phase") not in ("initialized", "update")):
        raise SampleError("The sample journal does not describe this home.")
    ids = header["ids"]
    if set(ids) != set(PROVIDERS) or any(str(uuid.UUID(ids[provider])) != ids[provider] for provider in PROVIDERS):
        raise SampleError("The synthetic conversation identities are invalid.")
    paths = _paths(ids["codex"], ids["claude"], _stamp(header["created_at"]))
    files, roots = current["files"], current["roots"]
    if set(files) != set(paths.values()) or set(roots) != set(ROOTS):
        raise SampleError("The sample journal contains an unsupported source path.")
    access.check_tree(files, roots)
    for relative, expected in files.items():
        if access.read(relative)[0] != expected:
            raise SampleError("The sample pack changed outside this tool. No merge or overwrite is allowed.")
    return marker, header, current, paths


def advance(home, action, *, provider="both", now=None):
    """Append lifecycle records only to a verified pack generated by this tool."""
    if action not in ("cooking", "needs-you", "ready") or provider not in (*PROVIDERS, "both"):
        raise SampleError("Choose cooking, needs-you or ready for codex, claude or both.")
    with _Home(home) as access, access.locked():
        marker, header, current, paths = _load(access)
        if marker["bytes"] > MAX_CONTROL_BYTES - 16384:
            raise SampleError("The sample journal reached its limit. Its existing versions are preserved.")
        selected = PROVIDERS if provider == "both" else (provider,)
        stamp = max(_stamp(now), current["last_stamp"] + .01)
        for name in selected:
            if action != "cooking" and current["state"][name]["status"] != "cooking":
                raise SampleError("Run cooking before adding a new question or completed reply.")
        for name in selected:
            state = current["state"][name]
            if action == "cooking":
                state["turn"] += 1
                state["turn_id"] = "review-" + name + "-" + str(state["turn"])
            record = _event(name, header["ids"][name], state["turn_id"], action, stamp)
            relative = paths[name + "_log"]
            current["files"][relative] = access.append(relative, _json(record) + b"\n", current["files"][relative])
            state["status"] = {"needs-you": "needs_you", "ready": "ready", "cooking": "cooking"}[action]
        current.update(phase="update", action=action, last_stamp=stamp)
        access.append(CONTROL, _json(current) + b"\n", marker, MAX_CONTROL_BYTES)
        return {"synthetic": True, "state": current["state"], "provider_requests": 0}


def status(home):
    with _Home(home) as access, access.locked():
        _, _, current, _ = _load(access)
        return {"synthetic": True, "home": str(access.path), "state": current["state"], "provider_requests": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "status", "cooking", "needs-you", "ready"))
    parser.add_argument("--provider", choices=(*PROVIDERS, "both"), default="both",
                        help="Choose the sample to advance; init always creates both.")
    args = parser.parse_args(argv)
    try:
        home = Path.home()
        result = initialize(home) if args.action == "init" else status(home) if args.action == "status" else advance(home, args.action, provider=args.provider)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError,
            RecursionError, sqlite3.Error) as error:
        parser.exit(1, "Review samples stopped: " + str(error) + "\nNo existing data is overwritten or removed. Incomplete sample files are preserved.\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
