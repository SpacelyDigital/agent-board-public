"""Read Claude Desktop project coordinators and user-facing task threads.

Uses only the active Desktop account's existing Code-session OAuth grant. No
credential refresh, browser session authentication, chat writes, or model calls
are performed. The private Code API can change; unknown states stay uncertain.
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlencode

from claude_source import _literal_preview, _short, _timestamp
from usage_source import (CLAUDE_DESKTOP, UsageUnavailable, _active_claude_org,
                          _decrypt_chromium, _keychain_password)
from process_reader import OutputLimitExceeded, read_process
from privacy_network import ProviderRequestError, read_provider_json

ORIGIN = "https://api.anthropic.com"
MAX_RESPONSE = 4 * 1024 * 1024
POLL_SECONDS = 15
COMPLETION_CACHE_SECONDS = 60
FETCH_TIMEOUT = 30
_CHANNEL = re.compile(r"^chan_[A-Za-z0-9_]{6,120}$")
_SESSION = re.compile(r"^cse_[A-Za-z0-9_]{6,120}$")
_MESSAGE = re.compile(r"^cmsg_[A-Za-z0-9_]{6,120}$")
_PATH = re.compile(r"^/v1/code/(?:channels(?:/chan_[A-Za-z0-9_]{6,120}/(?:timeline|threads/cmsg_[A-Za-z0-9_]{6,120}))?|sessions/cse_[A-Za-z0-9_]{6,120})(?:\?[^\r\n]*)?$")


class ProjectUnavailable(Exception):
    pass


def _session_token(folder: Path, now: float) -> str:
    """Select the intended native grant for the active account and organization."""
    try:
        config_path = folder / "config.json"
        if config_path.stat().st_size > MAX_RESPONSE:
            raise ValueError
        config = json.loads(config_path.read_text())
        account, encoded = config.get("lastKnownAccountUuid"), config.get("oauth:tokenCacheV2")
        if not isinstance(account, str) or not account or not isinstance(encoded, str):
            raise ValueError
        password = _keychain_password("Claude Safe Storage")
        organization = _active_claude_org(folder, password)
        cache = json.loads(_decrypt_chromium(base64.b64decode(encoded, validate=True), password))
        prefix = "acct:" + account + "|"
        candidates = []
        has_session_scope = False
        for cache_key, entry in cache.items():
            if not isinstance(cache_key, str) or not cache_key.startswith(prefix) or not isinstance(entry, dict):
                continue
            parts = cache_key[len(prefix):].split(":", 2)
            if len(parts) != 3 or parts[1] != organization or not parts[2].startswith(ORIGIN + ":"):
                continue
            scopes = parts[2][len(ORIGIN) + 1:].split()
            has_session_scope = has_session_scope or "user:sessions:claude_code" in scopes
            expires = entry.get("expiresAt")
            if ("user:sessions:claude_code" in scopes and isinstance(expires, (int, float))
                    and not isinstance(expires, bool) and math.isfinite(expires) and expires > now * 1000
                    and isinstance(entry.get("token"), str) and 0 < len(entry["token"]) <= 16384
                    and all(33 <= ord(char) <= 126 for char in entry["token"])):
                candidates.append((expires, entry["token"]))
        if not candidates:
            if not has_session_scope:
                raise ProjectUnavailable("Claude's current desktop sign-in does not include Code-project access. Open a Code project in Claude, then retry.")
            raise ProjectUnavailable("Claude's existing Code-session sign-in is unavailable or expired. Open Claude to refresh its own sign-in.")
        return max(candidates, key=lambda item: item[0])[1]
    except ProjectUnavailable:
        raise
    except UsageUnavailable as error:
        raise ProjectUnavailable(str(error)) from None
    except (OSError, ValueError, TypeError, AttributeError, UnicodeError, RecursionError, OverflowError):
        raise ProjectUnavailable("Claude's existing Code-session sign-in could not be read.") from None


class _Client:
    def __init__(self, token: str):
        self.token = token

    def get(self, path: str) -> dict:
        if not _PATH.fullmatch(path):
            raise ProjectUnavailable("The Claude project request path was not recognized.")
        try:
            return read_provider_json(ORIGIN + path, self.token, purpose="projects",
                                      timeout=5, max_bytes=MAX_RESPONSE)
        except ProviderRequestError as error:
            if error.reason == "redirect":
                raise ProjectUnavailable("Claude's project endpoint unexpectedly redirected the request.") from None
            if error.reason == "oversized":
                raise ProjectUnavailable("Claude returned an oversized project response.") from None
            if error.status in (401, 403):
                raise ProjectUnavailable("Claude did not accept its existing Code-session sign-in. Open Claude to check its sign-in.") from None
            if error.status == 429:
                raise ProjectUnavailable("Claude temporarily limited project status checks.") from None
            if error.status is not None:
                raise ProjectUnavailable(f"Claude project status is unavailable (HTTP {error.status}).") from None
            raise ProjectUnavailable("The Claude project status read failed or timed out.") from None


def _rows(value: dict) -> list[dict]:
    data = value.get("data")
    if not isinstance(data, list):
        raise ProjectUnavailable("Claude returned an unsupported project response.")
    return [row for row in data if isinstance(row, dict)]


def _project_status(session: dict) -> tuple[str, str]:
    # This is the coordinator's own status, independent of its workers.
    bucket = session.get("status_bucket")
    if bucket == "blocked":
        return "needs_you", "The project coordinator needs your input."
    if bucket == "working":
        return "cooking", "The project coordinator is working."
    if bucket in ("completed", "review_ready"):
        return "ready", "The project coordinator finished responding."
    return "disconnected", "The project coordinator's current state is uncertain."


def _thread_status(root: dict) -> tuple[str, str]:
    # These fields describe the user-facing thread itself, not a child outcome.
    # A current live prompt remains actionable even if another bound worker
    # is busy. An older needs_input label alone cannot override resumed work.
    if root.get("thread_waits_on_live_prompt") is True:
        return "needs_you", "This project thread needs your input."
    if root.get("thread_status") == "working" or root.get("thread_any_session_working") is True:
        return "cooking", "Claude is working in this project thread."
    if root.get("thread_status") == "needs_input":
        return "needs_you", "This project thread needs your input."
    if root.get("thread_status") == "resolved":
        return "ready", "This project thread is resolved."
    return "disconnected", "This project thread is idle; completion is unconfirmed."


def _event_id(ident: str, status: str, values: list[Any]) -> str:
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    return "claude:" + ident + ":" + status + ":" + digest


def _ready_reply_aliases(ident: str, root: dict, reply_id: str) -> list[str]:
    """Recognize old event encodings of this independently verified reply."""
    revision = [root["id"], root.get("last_reply_at"), root.get("reply_count")]
    # The old idle path included the immutable reply ID; the old resolved
    # path instead encoded the aggregate state and optional live flags.
    aliases = [_event_id(ident, "ready", [*revision, "idle", False, waiting, reply_id])
               for waiting in (False, None)]
    aliases.extend(_event_id(ident, "ready", [*revision, "resolved", working, waiting])
                   for working in (False, None) for waiting in (False, None))
    return aliases


def _permission_identity(details: list) -> list[str]:
    """A permission request is a new event even if the status stays blocked."""
    identities = []
    for detail in details:
        # Prefer immutable native request IDs over changing presentation data.
        identity = ({key: detail[key] for key in ("id", "request_id", "permission_request_id",
                    "tool_use_id", "prompt_id", "action_id", "approval_id") if key in detail}
                    if isinstance(detail, dict) else {})
        identities.append(json.dumps(identity or detail, sort_keys=True, separators=(",", ":")))
    return sorted(identities)


def _own_coordinator_permissions(details: list, session_id: str, roots: dict,
                                 previously_bound: bool = False) -> list[dict]:
    """Require native ownership evidence before separating a project prompt."""
    bound_ids = {item.get("id") for root in roots.values()
                 for item in _bound_sessions(root) if isinstance(item.get("id"), str)}
    if previously_bound or session_id in bound_ids:
        return []
    owned = []
    for detail in details:
        if not isinstance(detail, dict):
            continue
        scope = [detail] + [detail[key] for key in ("scope", "source", "origin")
                           if isinstance(detail.get(key), dict)]
        owners = {record[key] for record in scope
                  for key in ("session_id", "author_session_id", "owner_session_id", "source_session_id")
                  if isinstance(record.get(key), str) and record[key]}
        threaded = any(record.get(key) for record in scope
                       for key in ("thread_id", "thread_root_id", "root_message_id"))
        if owners == {session_id} and not threaded:
            owned.append(detail)
    return owned


def _bound_sessions(root: dict) -> list[dict]:
    value = root.get("bound_sessions")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _preview(message: dict, status: str) -> dict:
    # Timeline entries are original user-visible messages. Native AI-generated
    # post_turn_summary/thread_preview fields are never handoff previews.
    if (status not in ("ready", "needs_you") or not message.get("author_agent_id")
            or message.get("server_notice") or not _MESSAGE.fullmatch(str(message.get("id", "")))):
        return {}
    text = _literal_preview(message.get("body"))
    stamp = _timestamp(message.get("created_at"))
    return ({"preview": text, "preview_kind": "reply", "preview_at": stamp}
            if text and stamp else {})


def _base(channel: dict, ident: str, status: str, detail: str, updated: float,
          event_id: str, title: str, thread: str = "") -> dict:
    channel_id = channel["id"]
    url = "claude://code/project/" + channel_id
    if thread:
        url += "?" + urlencode({"thread": thread})
    return {"key": "claude:" + ident, "id": ident, "provider": "claude", "source_kind": "project",
            "title": title, "title_source": "desktop_project", "project_name": _short(channel.get("name")),
            "project_channel_id": channel_id, "project_role": "thread" if thread else "overview",
            **({"project_thread_id": thread} if thread else {}), "cwd": "", "status": status,
            "updated_at": updated, "started_at": None,
            "detail": detail, "can_open": True, "open_kind": "url", "open_label": "Open Claude",
            "open_url": url, "event_id": event_id,
            "turn_id": thread or (channel["channel_session_id"] if isinstance(channel.get("channel_session_id"), str)
                                   and _SESSION.fullmatch(channel["channel_session_id"]) else None),
            "confirmed_live": status in ("cooking", "needs_you")}


def _thread_preview(client: _Client, channel_id: str, root: dict, status: str) -> dict:
    thread_id = root["id"]
    path = f"/v1/code/channels/{channel_id}/threads/{thread_id}"
    cursor = ""
    messages = []
    # Thread responses are oldest first. Follow at most four bounded pages;
    # never present an earlier reply as the latest if the end was not reached.
    for _ in range(4):
        page = client.get(path + "?" + urlencode({"limit": 100, **({"cursor": cursor} if cursor else {})}))
        messages.extend(_rows(page))
        next_cursor = page.get("next_cursor")
        if not next_cursor:
            latest = max((row for row in messages if row.get("id") != thread_id
                          and not row.get("server_notice") and isinstance(row.get("body"), str)
                          and (row.get("author_agent_id") or row.get("author_account_id"))),
                         key=lambda row: _timestamp(row.get("created_at")), default={})
            return _preview(latest, status)
        if not isinstance(next_cursor, str) or next_cursor == cursor or len(next_cursor) > 4096:
            return {}
        cursor = next_cursor
    return {}


def _idle_reply_revision(root: dict) -> list | None:
    """Only an explicitly idle thread without an outstanding prompt can finish."""
    if (root.get("thread_status") not in ("idle", "resolved") or root.get("thread_any_session_working") is not False
            or root.get("thread_waits_on_live_prompt") is True):
        return None
    decisions = root.get("open_decisions")
    if decisions not in (None, []) or (decisions is None and root.get("thread_waits_on_live_prompt") is not False):
        return None
    if any(item.get("status_bucket") in ("working", "blocked") for item in _bound_sessions(root)):
        return None
    if not _timestamp(root.get("last_reply_at")):
        return None
    return [root.get("id"), root.get("last_reply_at"), root.get("reply_count")]


def _idle_thread_completion(client: _Client, channel_id: str, root: dict) -> dict:
    """Confirm the latest same-thread reply, with a settled root before/after.

    Idle and resolved describe the same reply at different aggregate states.
    Child outcomes do not establish that the thread replied to its latest input.
    """
    revision = _idle_reply_revision(root)
    if revision is None:
        return {}
    thread_id = root["id"]
    path = f"/v1/code/channels/{channel_id}/threads/{thread_id}"
    cursor = ""
    seen_cursors = set()
    messages = []
    previous_oldest = None
    # The desktop client's thread reader supports newest_first. Each page is
    # still ascending, with the root prepended; its cursor walks older pages.
    # Server notices do not count toward reply_count, so an oldest-first scan
    # can exhaust its budget long before reaching a completed long thread.
    # Read at most eight recent pages, stopping only after the latest authored
    # timestamp is bounded by an older row or the end of available history.
    for page_number in range(8):
        page = client.get(path + "?" + urlencode({"limit": 100, "newest_first": "true",
                                                  **({"cursor": cursor} if cursor else {})}))
        rows = _rows(page)
        if len(rows) > 101:
            return {}
        page_roots = [item for item in rows if item.get("id") == thread_id]
        if (len(page_roots) > 1 or page_number == 0 and len(page_roots) != 1
                or any(_idle_reply_revision(item) != revision for item in page_roots)):
            return {}
        times = []
        for item in rows:
            if item.get("channel_id") != channel_id or item.get("thread_root_id") != thread_id:
                return {}
            if item.get("id") != thread_id:
                stamp = _timestamp(item.get("created_at"))
                if not stamp:
                    return {}
                times.append(stamp)
            if (item.get("id") != thread_id and not item.get("server_notice")
                    and (item.get("author_account_id") or item.get("author_agent_id"))):
                if not _MESSAGE.fullmatch(str(item.get("id", ""))) or not _timestamp(item.get("created_at")):
                    return {}
                messages.append(item)
        if (times != sorted(times) or previous_oldest is not None and times and max(times) > previous_oldest):
            return {}
        next_cursor = page.get("next_cursor")
        if next_cursor not in (None, "") and (not isinstance(next_cursor, str) or len(next_cursor) > 4096
                or next_cursor in seen_cursors or any(ord(char) < 32 or ord(char) == 127 for char in next_cursor)):
            return {}
        latest_at = max((_timestamp(item["created_at"]) for item in messages), default=0)
        # A boundary equal to the candidate timestamp may hide another author
        # on the next page. Read past that boundary before accepting uniqueness.
        if latest_at and (not next_cursor or times and min(times) < latest_at):
            break
        if not next_cursor or not times:
            return {}
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        previous_oldest = min(times)
    else:
        return {}
    if not messages:
        return {}
    latest_at = max(_timestamp(item["created_at"]) for item in messages)
    latest = {item["id"]: item for item in messages if _timestamp(item["created_at"]) == latest_at}
    if len(latest) != 1 or latest_at != _timestamp(root["last_reply_at"]):
        return {}
    message = next(iter(latest.values()))
    if message.get("author_account_id"):
        return {}
    preview = _preview(message, "ready")
    if not preview:
        return {}
    final_page = client.get(path + "?limit=1")
    final_root = next((item for item in _rows(final_page) if item.get("id") == thread_id), {})
    if (final_root.get("channel_id") != channel_id or final_root.get("thread_root_id") != thread_id
            or _idle_reply_revision(final_root) != revision):
        return {}
    return {**preview, "_idle_reply_revision": revision, "_completion_reply_id": message["id"]}


def _collect(now: float, cutoff: float, tracked_ids: set[str], previous: dict[str, dict],
             client: _Client | None = None) -> tuple[list[dict], dict]:
    client = client or _Client(_session_token(CLAUDE_DESKTOP, now))
    channels = _rows(client.get("/v1/code/channels?scope=all"))
    channels = [c for c in channels if _CHANNEL.fullmatch(str(c.get("id", "")))
                and not c.get("archived_at") and not c.get("disabled_at")]
    if len(channels) > 100:
        raise ProjectUnavailable("Claude returned more project channels than this reader supports.")
    observations: list[dict] = []
    errors = []

    def read_channel(channel: dict) -> tuple[list[dict], list[str]]:
        channel_id = channel["id"]
        ident = "project:" + channel_id
        title = _short(channel.get("name")) or "Claude project"
        result, failures = [], []
        coordinator = None
        coordinator_prompt = False
        coordinator_reply_at = 0.0
        coordinator_input_at = 0.0
        permissions = []
        try:
            timeline = _rows(client.get(f"/v1/code/channels/{channel_id}/timeline?limit=100"))
        except ProjectUnavailable as error:
            timeline = []
            failures.append(str(error))
        session_id = channel.get("channel_session_id")
        if isinstance(session_id, str) and _SESSION.fullmatch(session_id):
            try:
                response = client.get("/v1/code/sessions/" + session_id)
                session = response.get("response_shape", response)
                if not isinstance(session, dict) or session.get("id") != session_id:
                    raise ProjectUnavailable("Claude returned an unsupported coordinator response.")
                status, detail = _project_status(session)
                permissions = session.get("requires_action_details_list")
                permissions = permissions if isinstance(permissions, list) else []
                own_messages = [r for r in timeline if r.get("author_session_id") == session_id
                                and not r.get("server_notice")]
                latest = max(own_messages, key=lambda r: _timestamp(r.get("created_at")), default={})
                updated = max(_timestamp(session.get("last_event_at")), _timestamp(latest.get("created_at")))
                revision = [session_id, latest.get("id"), session.get("last_event_at"), session.get("status_bucket")]
                if permissions:
                    revision.append({"permissions": _permission_identity(permissions)})
                token = _event_id(ident, status, revision)
                row = _base(channel, ident, status, detail, updated, token, title)
                unread = session.get("unread") is True or channel.get("has_unread") is True
                row.update(pending_review=status == "ready" and unread,
                           discovery_eligible=status in ("cooking", "needs_you") or (status == "ready" and unread))
                row.update(_preview(latest, status))
                # A top-level coordinator reply is separate from a task thread.
                # Its actual message time can prove new input is needed; a
                # coordinator heartbeat or aggregate status alone cannot.
                own_replies = [r for r in own_messages
                               if r.get("has_thread") is not True
                               and r.get("thread_root_id") in (None, r.get("id"))
                               and _preview(r, status)]
                coordinator_reply_at = max((_timestamp(r.get("created_at")) for r in own_replies), default=0.0)
                own_inputs = [r for r in timeline if r.get("author_account_id")
                              and not r.get("server_notice") and r.get("has_thread") is not True
                              and r.get("thread_root_id") in (None, r.get("id"))
                              and _literal_preview(r.get("body"))]
                coordinator_input_at = max((_timestamp(r.get("created_at")) for r in own_inputs), default=0.0)
                coordinator = row
                result.append(row)
            except ProjectUnavailable as error:
                failures.append(str(error))
        roots = {r["id"]: r for r in timeline if r.get("has_thread") is True
                 and _MESSAGE.fullmatch(str(r.get("id", "")))}
        # Already tracked tasks remain addressable even when older than the
        # bounded timeline page. Fetch their own root instead of importing history.
        prefix = ident + ":thread:"
        for tracked in tracked_ids:
            if not tracked.startswith(prefix):
                continue
            thread_id = tracked[len(prefix):]
            if thread_id in roots or not _MESSAGE.fullmatch(thread_id):
                continue
            try:
                page = client.get(f"/v1/code/channels/{channel_id}/threads/{thread_id}?limit=1")
                root = next((r for r in _rows(page) if r.get("id") == thread_id), None)
                if root:
                    roots[thread_id] = root
            except ProjectUnavailable as error:
                failures.append(str(error))
        for thread_id, root in roots.items():
            task_id = prefix + thread_id
            status, detail = _thread_status(root)
            unread = root.get("has_unread") is True
            updated = _timestamp(root.get("last_reply_at")) or _timestamp(root.get("created_at"))
            completion = {}
            old = previous.get(task_id, {})
            revision = _idle_reply_revision(root)
            if revision is not None and (task_id in tracked_ids or unread or updated >= cutoff):
                verified_at = old.get("_completion_verified_at")
                fresh_proof = (isinstance(verified_at, (int, float)) and not isinstance(verified_at, bool)
                               and math.isfinite(verified_at) and 0 <= now - verified_at < COMPLETION_CACHE_SECONDS)
                if fresh_proof and old.get("_idle_reply_revision") == revision and old.get("_completion_reply_id"):
                    completion = {key: old[key] for key in ("preview", "preview_kind", "preview_at",
                                                           "_idle_reply_revision", "_completion_reply_id",
                                                           "_completion_verified_at") if key in old}
                else:
                    try:
                        completion = _idle_thread_completion(client, channel_id, root)
                        if completion:
                            completion["_completion_verified_at"] = now
                    except ProjectUnavailable:
                        pass
                if completion:
                    status, detail = "ready", "Claude finished responding in this project thread."
            unverified_resolved = status == "ready" and not completion
            if unverified_resolved:
                status, detail = "disconnected", "This project thread is resolved; its latest reply has not been verified."
            discovery = status in ("cooking", "needs_you") or (status == "ready" and
                        (unread or (bool(completion) and updated >= cutoff)))
            # The board begins tracking project tasks at activation. An older
            # task is still addressable in Background, but an old needs_input
            # label or unread flag alone must not populate the main queue.
            # Repeat this classification on every scan so it survives tracking
            # and service restarts. BoardStore's explicit placement preference
            # can override it; genuine new activity clears it automatically.
            older_thread = (updated > 0 and updated < cutoff
                            and status != "cooking"
                            and root.get("thread_waits_on_live_prompt") is not True)
            if not discovery and not older_thread and task_id not in tracked_ids:
                continue
            bound = root.get("bound_sessions")
            titles = [_short(item.get("title"), 140) for item in bound
                      if isinstance(item, dict) and item.get("title")] if isinstance(bound, list) else []
            # The task's native session title is the same name shown in Desktop.
            # Its bound session outcome never substitutes for the thread state.
            caption = next((value for value in titles if value), "") or _short(root.get("thread_preview"), 140)
            caption = caption or _short(_literal_preview(root.get("body")), 100) or "Project thread"
            event_values = [thread_id, root.get("last_reply_at"), root.get("reply_count"),
                            root.get("thread_status"), root.get("thread_any_session_working"),
                            root.get("thread_waits_on_live_prompt")]
            if completion:
                # The completed assistant message owns the Ready identity.
                # Aggregate flags, reply-count formatting, and later resolving
                # or reopening the same thread cannot manufacture another reply.
                event_values = ["assistant_reply", completion["_completion_reply_id"]]
            token = _event_id(task_id, status, event_values)
            row = _base(channel, task_id, status, detail, updated, token, title + " · " + caption, thread_id)
            row.update(pending_review=status == "ready" and unread,
                       discovery_eligible=discovery or older_thread)
            if unverified_resolved:
                row["metadata_only"] = True
            if older_thread:
                row["background_reason"] = "older_project_thread"
            if completion:
                row.update(completion)
                row.update(project_reply_id=completion["_completion_reply_id"],
                           project_reply_verified=True,
                           _project_reply_revision=completion["_idle_reply_revision"],
                           _project_event_aliases=_ready_reply_aliases(task_id, root, completion["_completion_reply_id"]))
                row["_project_id_event_aliases"] = row["_project_event_aliases"][:2]
            elif old.get("event_id") == token:
                row.update({k: old[k] for k in ("preview", "preview_kind", "preview_at") if k in old})
            elif status in ("ready", "needs_you"):
                try:
                    row.update(_thread_preview(client, channel_id, root, status))
                except ProjectUnavailable:
                    pass  # A missing preview cannot change authoritative state.
            result.append(row)
        # The channel overview and its current task(s) describe the same project
        # activity at two levels. Keep task-specific links on the board and the
        # overview addressable in Background instead of counting both as chats.
        # A coordinator's own live permission request or a newer top-level reply
        # is independent work and remains visible. Manual placement is applied
        # later by BoardStore, without deleting either row or acknowledgements.
        current_tasks = [row for row in result if row.get("project_role") == "thread"
                         and not row.get("background_reason")
                         and row["status"] in ("cooking", "needs_you", "ready")]
        if coordinator:
            old = previous.get(ident, {})
            bound_here = any(item.get("id") == session_id for root in roots.values()
                             for item in _bound_sessions(root))
            previously_bound = bool(failures and old.get("_coordinator_bound_to_thread"))
            coordinator["_coordinator_bound_to_thread"] = bound_here or previously_bound
            coordinator_prompt = bool(_own_coordinator_permissions(permissions, session_id, roots, previously_bound))
            latest_task_activity = (max(row["updated_at"] for row in current_tasks)
                                    if current_tasks else old.get("_overlap_activity_at", 0))
            if latest_task_activity:
                coordinator["_overlap_activity_at"] = latest_task_activity
            independent_reply = (latest_task_activity > 0 and coordinator["status"] in ("needs_you", "ready")
                                 and coordinator_reply_at > latest_task_activity)
            independent_input = (latest_task_activity > 0 and coordinator["status"] in ("cooking", "needs_you", "ready")
                                 and coordinator_input_at > latest_task_activity)
            if not (coordinator_prompt or independent_reply or independent_input):
                if current_tasks:
                    coordinator["background_reason"] = "project_overview"
                    coordinator["discovery_eligible"] = True
                elif failures:
                    # A failed task read cannot prove the prior overlap ended.
                    # Keep an existing stored route until a healthy scan can
                    # confirm fallback, including immediately after restart.
                    coordinator["background_routing_uncertain"] = True
        return result, failures

    with ThreadPoolExecutor(max_workers=4) as pool:
        for result, failures in pool.map(read_channel, channels):
            observations.extend(result)
            errors.extend(failures)
    # A thread can resume before its last_reply_at changes. Keep the receipt's
    # status observation time separate from that message timestamp so a fresh
    # active state can supersede old attention without treating cached polls as
    # new provider evidence. The source cache preserves this value unchanged.
    for observation in observations:
        observation["project_status_observed_at"] = now
    health = {"id": "claude_projects", "name": "Claude projects", "available": not errors,
              "detail": ("Reading project and task status from Claude." if not errors else
                         "Some Claude project statuses could not be read. " + errors[0]),
              "last_seen": max((r["updated_at"] for r in observations), default=None)}
    return observations, health


def _fetch(now: float, cutoff: float, tracked_ids: set[str], previous: dict[str, dict]) -> tuple[list[dict], dict]:
    payload = {"now": now, "cutoff": cutoff, "tracked_ids": sorted(tracked_ids), "previous": previous}
    try:
        process = read_process([sys.executable, *([] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve())]), "--read-projects"],
                               input=json.dumps(payload).encode(), timeout=FETCH_TIMEOUT, max_output=MAX_RESPONSE)
        if len(process.stdout) > MAX_RESPONSE:
            raise ValueError
        result = json.loads(process.stdout)
        if not isinstance(result, dict):
            raise ValueError
        if "error" in result:
            raise ProjectUnavailable(result["error"])
        if not isinstance(result.get("observations"), list) or not isinstance(result.get("source"), dict):
            raise ValueError
        return result["observations"], result["source"]
    except ProjectUnavailable:
        raise
    except subprocess.TimeoutExpired:
        raise ProjectUnavailable("Claude's noninteractive project status read timed out. No permission prompt was requested.") from None
    except (OSError, ValueError, TypeError, OutputLimitExceeded, RecursionError):
        raise ProjectUnavailable("Claude project status could not be read.") from None


class ClaudeProjectSource:
    def __init__(self):
        self.tracked_ids: set[str] = set()
        self._at = float("-inf")
        self._observations: list[dict] = []
        self._health: dict = {}

    def scan(self, now: float, cutoff: float) -> tuple[list[dict], dict]:
        elapsed = now - self._at
        # The monitor uses a monotonic start-to-start cadence. Allow clock and
        # call-site jitter at the boundary rather than skipping a whole cycle.
        if elapsed >= POLL_SECONDS or math.isclose(elapsed, POLL_SECONDS, rel_tol=0, abs_tol=0.05):
            previous = {row["id"]: {k: row[k] for k in ("event_id", "preview", "preview_kind", "preview_at",
                                                       "_overlap_activity_at", "_coordinator_bound_to_thread",
                                                       "_idle_reply_revision", "_completion_reply_id",
                                                       "_completion_verified_at") if k in row}
                        for row in self._observations}
            try:
                self._observations, self._health = _fetch(now, cutoff, self.tracked_ids, previous)
            except ProjectUnavailable as error:
                self._observations = []
                self._health = {"id": "claude_projects", "name": "Claude projects", "available": False,
                                "detail": str(error), "last_seen": None}
            self._at = now
        return copy.deepcopy(self._observations), copy.deepcopy(self._health)


if __name__ == "__main__" and sys.argv[1:] == ["--read-projects"]:
    try:
        request = json.loads(sys.stdin.buffer.read(MAX_RESPONSE + 1))
        observations, source = _collect(float(request["now"]), float(request["cutoff"]),
                                        set(request.get("tracked_ids", [])), request.get("previous", {}))
        print(json.dumps({"observations": observations, "source": source}))
    except ProjectUnavailable as error:
        print(json.dumps({"error": str(error)}))
    except Exception:
        print(json.dumps({"error": "Claude project status could not be read."}))
