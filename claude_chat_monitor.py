"""Account-scoped Claude chat lifecycle with a private metadata checkpoint.

The account reader supplies exact cloud UUIDs for both browser and desktop chats.
This module never reads a conversation body or uses a title as identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

from chat_links import DESKTOP_CHAT_ID, DESKTOP_CHAT_ID_PREFIX, DESKTOP_CHAT_URLS


MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_CHATS = 5000
ACCOUNT_SCOPE = re.compile(r"[0-9a-f]{64}\Z")


def _timestamp(value, now):
    return (float(value) if type(value) in (int, float) and 0 < value <= now + 2
            and math.isfinite(value) else None)


def _chat_id(value):
    return value if isinstance(value, str) and DESKTOP_CHAT_ID.fullmatch(value) else None


class _PrivateCheckpoint:
    """Keep only validated metadata, replacing the private file atomically."""

    def __init__(self, path):
        path = Path(path)
        self.name = path.name
        if self.name in {"", ".", ".."}:
            raise ValueError("Invalid chat checkpoint name")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(self.directory)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            self.close()
            raise PermissionError("Chat checkpoint directory is not private")

    @staticmethod
    def _valid_file(info):
        return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_nlink == 1 and not info.st_mode & 0o077)

    def load(self):
        try:
            descriptor = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=self.directory)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not self._valid_file(info) or not 0 < info.st_size <= MAX_STATE_BYTES:
                raise ValueError("Chat checkpoint is not a bounded private file")
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError("Chat checkpoint exceeds its size limit")
        return json.loads(raw)

    def save(self, value):
        try:
            info = os.stat(self.name, dir_fd=self.directory, follow_symlinks=False)
            if not self._valid_file(info):
                raise ValueError("Chat checkpoint is not a private regular file")
        except FileNotFoundError:
            pass
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        if len(encoded) > MAX_STATE_BYTES:
            raise ValueError("Chat checkpoint exceeds its size limit")
        temporary = "." + self.name + "." + uuid.uuid4().hex
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.name, src_dir_fd=self.directory, dst_dir_fd=self.directory)
            os.fsync(self.directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.directory)
            except FileNotFoundError:
                pass

    def close(self):
        if self.directory is not None:
            os.close(self.directory)
            self.directory = None


class ClaudeChatConversationSource:
    provider = "claude"
    name = "Claude"
    source_id = "claude_chats"

    def __init__(self, reader, state_path):
        self.reader = reader
        self.checkpoint = None
        self._loaded = None
        self._state = None
        self._processed_at = None
        self._checkpoint_error = False
        self._transient_error = None
        self.available = False
        self.observed_at = None
        self.cooking = {}
        self.latest_metadata = {}
        self._closed = False
        try:
            self.checkpoint = _PrivateCheckpoint(state_path)
            self._loaded = self.checkpoint.load()
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
            self._checkpoint_error = True

    @staticmethod
    def _decode_state(value, now):
        if value is None:
            return None
        if (not isinstance(value, dict) or value.get("version") != 1
                or not isinstance(value.get("account_scope"), str)
                or not ACCOUNT_SCOPE.fullmatch(value["account_scope"])
                or _timestamp(value.get("initialized_at"), now) is None
                or not isinstance(value.get("chats"), dict) or len(value["chats"]) > MAX_CHATS):
            raise ValueError("Invalid chat checkpoint")
        chats = {}
        for ident, row in value["chats"].items():
            if _chat_id(ident) is None or not isinstance(row, dict):
                raise ValueError("Invalid chat checkpoint identity")
            record = {}
            for field in ("settled_at", "pending_started_at"):
                raw = row.get(field)
                parsed = _timestamp(raw, now)
                if raw is not None and parsed is None:
                    raise ValueError("Invalid chat checkpoint timestamp")
                record[field] = parsed
            leaf = row.get("leaf")
            if leaf is not None and _chat_id(leaf) is None:
                raise ValueError("Invalid chat checkpoint leaf")
            record["leaf"] = leaf
            chats[ident] = record
        return {"version": 1, "account_scope": value["account_scope"],
                "initialized_at": value["initialized_at"], "chats": chats}

    @staticmethod
    def _metadata(ident, row, now):
        if _chat_id(ident) is None or not isinstance(row, dict):
            return None
        title = row.get("title")
        if (not isinstance(title, str) or len(title) > 4096
                or any(0xd800 <= ord(char) <= 0xdfff for char in title)
                or (row.get("needs_input") is not None and type(row["needs_input"]) is not bool)):
            return None
        return {"uuid": ident, "id": DESKTOP_CHAT_ID_PREFIX + ident,
                "title": " ".join(title.split())[:160] or "Claude chat",
                "created_at": _timestamp(row.get("created_at"), now),
                "updated_at": _timestamp(row.get("updated_at"), now) or
                              _timestamp(row.get("last_settled_at"), now) or now,
                "settled_at": _timestamp(row.get("last_settled_at"), now),
                "leaf": _chat_id(row.get("current_leaf_message_uuid")),
                "live_status": row.get("live_status", "unavailable"),
                "live_started_at": _timestamp(row.get("live_status_started_at"), now),
                "needs_input": row.get("needs_input") is True,
                "needs_input_kind": row.get("needs_input_kind") if isinstance(row.get("needs_input_kind"), str)
                                    and len(row["needs_input_kind"]) <= 128 else None}

    @staticmethod
    def _fields(item):
        return {"key": "claude:" + item["id"], "provider": "claude", "source_kind": "desktop_chat",
                "id": item["id"], "title": item["title"], "can_open": True,
                "open_label": "Open Claude", "open_kind": "url",
                "open_url": DESKTOP_CHAT_URLS["claude"] + item["uuid"]}

    @staticmethod
    def _event(kind, scope, ident, *parts):
        digest = hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()[:24]
        return f"claude-chat:{scope}:{ident}:{kind}:{digest}"

    def scan(self, store, now):
        self.available = False
        self.cooking = {}
        self._transient_error = None
        if self._closed or self._checkpoint_error:
            return self.health()
        try:
            snapshot = self.reader.scan(now)
        except Exception:
            self._transient_error = "reader"
            return self.health()
        if not isinstance(snapshot, dict):
            self._transient_error = "reader"
            return self.health()
        if snapshot.get("error_code") == "incomplete_status":
            self._transient_error = "incomplete_status"
        try:
            observed = snapshot.get("observed_at")
            scope = snapshot.get("account_scope")
            rows = snapshot.get("conversations")
            if (snapshot.get("available") is not True or snapshot.get("fresh") is not True
                    or _timestamp(observed, now) is None or not 0 <= now - observed <= 15
                    or not isinstance(scope, str) or not ACCOUNT_SCOPE.fullmatch(scope)
                    or not isinstance(rows, dict) or len(rows) > MAX_CHATS):
                return self.health()
            if self._state is None:
                try:
                    self._state = self._decode_state(self._loaded, now)
                except (ValueError, TypeError, OverflowError):
                    self._checkpoint_error = True
                    return self.health()
                self._loaded = None
            initial_snapshot = self._state is None or self._state["account_scope"] != scope
            if initial_snapshot:
                self._state = {"version": 1, "account_scope": scope, "initialized_at": observed, "chats": {}}
                self._processed_at = None
                self.latest_metadata = {}
            metadata = {item["id"]: item for ident, row in rows.items()
                        if (item := self._metadata(ident, row, now)) is not None}
            if self._processed_at is not None and observed < self._processed_at:
                return self.health()
            self.latest_metadata = metadata
            self.observed_at = observed
            current_cooking = {}
            for item in metadata.values():
                if item["live_status"] == "running" and not item["needs_input"]:
                    previous = self._state["chats"].get(item["uuid"], {})
                    started = previous.get("pending_started_at") or item["live_started_at"] or observed
                    current_cooking[item["id"]] = (started, item["title"])
            if observed == self._processed_at:
                self.cooking = current_cooking
                self.available = True
                return self.health()

            tracked = store.session_ids("claude")
            observations = [{**self._fields(item), "status": "disconnected", "metadata_only": True,
                "discovery_eligible": False, "updated_at": item["updated_at"],
                "event_id": "claude-chat-metadata:" + item["uuid"],
                "detail": "No new reply has been confirmed."}
                for item in metadata.values() if item["id"] in tracked]
            records = {ident: dict(record) for ident, record in self._state["chats"].items()}
            for item in metadata.values():
                ident = item["uuid"]
                prior = records.get(ident)
                record = dict(prior or {"leaf": None, "settled_at": None, "pending_started_at": None})
                started = record["pending_started_at"]
                status = None
                if item["needs_input"] or item["live_status"] == "running":
                    began = item["live_started_at"] or observed
                    started = max(started, began) if started is not None and item["live_started_at"] is not None else started or began
                    record["pending_started_at"] = started
                    status = "needs_you" if item["needs_input"] else "cooking"
                    turn = self._event("turn", scope, ident, started)
                    event = (self._event("input", scope, ident, started, item["leaf"], item["needs_input_kind"])
                             if item["needs_input"] else turn)
                    updated = max(started, item["updated_at"])
                elif item["live_status"] is None and item["settled_at"] is not None and item["leaf"] is not None:
                    changed = (prior is not None and prior["settled_at"] is not None
                               and item["settled_at"] > prior["settled_at"] and item["leaf"] != prior["leaf"])
                    first_pending_reply = (prior is not None and prior["settled_at"] is None
                                           and started is not None and item["settled_at"] >= math.floor(started)
                                           and item["leaf"] != prior["leaf"])
                    # Older collectors could save a completed pair before the
                    # provider cleared its running flag. A fresh idle receipt
                    # can recover that pending turn only when the settlement
                    # is strictly later than its start; same-second history
                    # does not provide that proof.
                    saved_pending_reply = (prior is not None and started is not None
                                           and item["settled_at"] > started
                                           and item["settled_at"] == prior["settled_at"]
                                           and item["leaf"] == prior["leaf"])
                    newly_settled = (not initial_snapshot and prior is None and (
                        item["settled_at"] > self._state["initialized_at"] or
                        item["created_at"] is not None and item["created_at"] > self._state["initialized_at"]
                        and item["settled_at"] >= math.floor(item["created_at"])))
                    attributable_pair = started is None or item["settled_at"] >= math.floor(started)
                    if (changed and attributable_pair) or first_pending_reply or saved_pending_reply or newly_settled:
                        status = "ready"
                        event = self._event("reply", scope, ident, item["settled_at"], item["leaf"])
                        turn = self._event("turn", scope, ident, started) if started is not None else event
                        updated = item["settled_at"]
                        record["pending_started_at"] = None
                # Keep a settled pair together. A lone leaf change (for example
                # branch selection), a lone timestamp, or an unknown live state
                # must not consume the later confirmed completion marker.
                known_state = item["live_status"] is None or item["live_status"] == "running" or item["needs_input"]
                # Settlement and live status can arrive in separate snapshots.
                # Keep a new pair unconsumed while it belongs to the active
                # turn, so the subsequent idle snapshot can confirm Ready.
                # A settlement before a later turn began is only that turn's
                # baseline. First-seen same-second history is also ambiguous.
                advanced_pending_start = (prior is not None and prior["pending_started_at"] is not None
                    and item["live_started_at"] is not None
                    and item["live_started_at"] > prior["pending_started_at"])
                same_pending_turn = (prior is not None and prior["pending_started_at"] is not None
                                     and not advanced_pending_start)
                pending_settlement = (status in {"cooking", "needs_you"} and started is not None
                    and item["settled_at"] is not None and item["settled_at"] >= math.floor(started)
                    and (same_pending_turn or item["settled_at"] > started))
                if (known_state and item["settled_at"] is not None and item["leaf"] is not None
                        and not pending_settlement
                        and (record["settled_at"] is None or
                             item["settled_at"] > record["settled_at"] and item["leaf"] != record["leaf"])):
                    record["settled_at"] = item["settled_at"]
                    record["leaf"] = item["leaf"]
                records[ident] = record
                if status is not None:
                    saved = store.get("claude:" + item["id"])
                    scope_prefix = f"claude-chat:{scope}:{ident}:"
                    if (saved and saved.get("source_kind") == "desktop_chat"
                            and isinstance(saved.get("turn_id"), str)
                            and saved["turn_id"].startswith(scope_prefix)
                            and _timestamp(saved.get("observed_at"), now) is not None
                            and observed >= saved["observed_at"]
                            and _timestamp(saved.get("updated_at"), now) is not None):
                        # Claude's settled/start times have whole-second
                        # precision, while metadata updates include fractions.
                        # A newer verified lifecycle read in the same account
                        # must not lose to that timestamp precision difference.
                        # Keep event markers unchanged and board time monotonic.
                        updated = max(updated, saved["updated_at"])
                    detail = {"cooking": "Claude is responding.", "ready": "Review the latest reply in Claude.",
                              "needs_you": "Claude needs your input. Open the conversation to continue."}[status]
                    observations.append({**self._fields(item), "status": status, "detail": detail,
                        "updated_at": updated, "started_at": started, "event_id": event, "turn_id": turn,
                        "confirmed_live": status == "cooking"})
            if len(records) > MAX_CHATS:
                records = dict(sorted(records.items(), key=lambda entry: (
                    entry[1]["pending_started_at"] is not None, entry[1]["settled_at"] or 0), reverse=True)[:MAX_CHATS])
            checkpoint = {**self._state, "chats": records}
            # Apply first. A crash before checkpointing repeats stable event IDs,
            # preserving Reviewed instead of losing a newly completed reply.
            store.apply(observations, now)
            if checkpoint != self._state or self._processed_at is None:
                try:
                    self.checkpoint.save(checkpoint)
                except (OSError, ValueError):
                    self._transient_error = "checkpoint"
                    return self.health()
            self._state = checkpoint
            self._processed_at = observed
            self.cooking = current_cooking
            self.available = True
        except Exception:
            # An unexpected transient reader/database failure must not latch
            # monitoring off permanently. Only malformed saved checkpoints do.
            self._transient_error = "reader"
        return self.health()

    def owns_saved_event(self, item):
        """Preserve saved replies without assigning them to another sign-in."""
        scope = self._state.get("account_scope") if self._state is not None else None
        ident = item.get("id")
        turn = item.get("turn_id")
        if (not isinstance(scope, str) or not ACCOUNT_SCOPE.fullmatch(scope)
                or not isinstance(ident, str) or not ident.startswith(DESKTOP_CHAT_ID_PREFIX)
                or not isinstance(turn, str)):
            return False
        uuid = ident[len(DESKTOP_CHAT_ID_PREFIX):]
        if _chat_id(uuid) is None:
            return False
        prefix = f"claude-chat:{scope}:{uuid}:"
        return turn.startswith(prefix) and len(turn) > len(prefix)

    def open_chats(self, now):
        if (not self.available or self.observed_at is None or not 0 <= now - self.observed_at <= 15):
            return {}
        return {ident: {**self._fields(item), "open_only": True, "catalog_only": True,
                       "status": "disconnected", "updated_at": item["updated_at"], "notification_eligible": False,
                       "detail": "Saved in Claude. No new reply has been confirmed."}
                for ident, item in self.latest_metadata.items()}

    def health(self):
        return {"id": self.source_id, "name": "Claude chats", "available": self.available,
                "last_seen": self.observed_at,
                "detail": ("The local Claude chat checkpoint could not be read or saved." if self._checkpoint_error or self._transient_error == "checkpoint" else
                           "Claude did not include current chat status. Using the previous reading while retrying." if self._transient_error == "incomplete_status" and self.available else
                           "Claude did not include current chat status. Retrying automatically." if self._transient_error == "incomplete_status" else
                           "Monitoring Claude chats using their account identities." if self.available else
                           "Claude chat metadata is unavailable. Check its existing sign-in.")}

    def close(self):
        self._closed = True
        self.available = False
        self.cooking = {}
        self.reader.close()
        if self.checkpoint is not None:
            self.checkpoint.close()
