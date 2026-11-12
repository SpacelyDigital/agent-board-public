#!/usr/bin/env python3
"""Local-only status collector and display feed. Requires Python 3.9+, no packages."""

import argparse
import copy
import hashlib
import json
import logging
import os
import plistlib
import re
import secrets
import select
import signal
import socket
import socketserver
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from local_quota import ClaudeLocalQuota, STALE_SECONDS, parse_codex_observation

from board_store import BoardStore, Conflict, valid_desktop_chat_id
from claude_source import ClaudeSource
from codex_source import CodexSource
from chat_links import (DESKTOP_CHAT_ID, DESKTOP_CHAT_ID_PREFIX, DESKTOP_CHAT_URLS,
                        chat_url, open_command, UnsupportedChatLink)

if os.environ.get("AGENT_BOARD_SANDBOX") == "1":
    # The signed sandbox entry sets this before importing the server. Store
    # packages omit these legacy account/executable readers entirely.
    UsageSource = ClaudeProjectSource = None

    def stop_all_processes():
        pass
else:
    from claude_project_source import ClaudeProjectSource
    from chatgpt_source import ChatGPTSource
    from chatgpt_status import ChatGPTStatusReader
    from chatgpt_monitor import ChatGPTConversationSource
    from claude_chat_source import ClaudeChatAccountReader
    from claude_chat_monitor import ClaudeChatConversationSource
    from usage_source import UsageSource
    from process_reader import stop_all_processes

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("agent-board")
SNAPSHOT_LOCK_TIMEOUT = 1.0
STATUS_HEARTBEAT_SECONDS = 15.0
PROJECT_HEARTBEAT_SECONDS = 45.0


def _runtime_build_label():
    if not getattr(sys, "frozen", False):
        return "Development build"
    try:
        executable = Path(sys.executable)
        contents = executable.parent.parent
        if executable.parent.name != "MacOS" or contents.name != "Contents":
            return "Version unavailable"
        with (contents / "Info.plist").open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return "Version unavailable"
        info = plistlib.loads(raw)
        version, build = info.get("CFBundleShortVersionString"), info.get("CFBundleVersion")
        if (not isinstance(version, str) or len(version) > 32
                or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", version)
                or not isinstance(build, str) or not re.fullmatch(r"[1-9][0-9]{0,8}", build)):
            return "Version unavailable"
        return f"Version {version} (build {build})"
    except Exception:
        return "Version unavailable"


# Capture the running package's identity once, rather than reading a replacement
# bundle later and labeling an older collector with the newer build number.
BUILD_LABEL = _runtime_build_label()


class _UnselectedSource:
    def __init__(self, provider):
        self.provider = provider


class Monitor:
    def __init__(self, data_dir, codex_root=None, claude_root=None, interval=2, usage_enabled=False, projects_enabled=False,
                 claude_desktop_root=None, sandbox=False, source_grants=None, store_profile=False, sample_mode=False,
                 claude_chat_source=None, chatgpt_chat_source=None):
        if store_profile and not sandbox:
            raise ValueError("The Store profile requires sandbox source access.")
        desktop_chat_sources = {provider: reader for provider, reader in
                                (("claude", claude_chat_source), ("chatgpt", chatgpt_chat_source))
                                if reader is not None}
        if desktop_chat_sources and (sandbox or store_profile or sample_mode
                                     or any(root is not None for root in
                                            (codex_root, claude_root, claude_desktop_root))):
            raise ValueError("Desktop chat status requires a direct native launch.")
        if source_grants is None:
            # Explicit-root model fixtures predate the native grant bridge.
            # The packaged entry always supplies its validated three grants.
            source_grants = dict.fromkeys(("codex", "claude", "claude_desktop"), True)
        if (not isinstance(source_grants, dict) or set(source_grants) != {"codex", "claude", "claude_desktop"}
                or any(type(value) is not bool for value in source_grants.values())):
            raise ValueError("Source grants must be the validated folder-access choices.")
        if type(sample_mode) is not bool:
            raise ValueError("Sample mode must be an explicit choice.")
        if sample_mode and (not sandbox or not store_profile or any(source_grants.values())
                            or usage_enabled or projects_enabled):
            raise ValueError("Sample mode requires the Store sandbox with every provider source disabled.")
        if sandbox and any(value is None for value in (codex_root, claude_root, claude_desktop_root)):
            raise ValueError("Sandbox monitoring requires explicit authorized source folders.")
        source_allowed = {"codex": not sandbox or source_grants["codex"],
                          "claude": not sandbox or source_grants["claude"] and source_grants["claude_desktop"]}
        board_directory = Path(data_dir)
        if sample_mode:
            # A separate directory also separates SQLite's initialization lock,
            # recovery files and preferences from the live board.
            if not board_directory.is_absolute() or ".." in board_directory.parts:
                raise ValueError("Sample data requires a regular private directory.")
            system_aliases = {Path("/var"): Path("/private/var"), Path("/tmp"): Path("/private/tmp")}
            for ancestor in (board_directory, *board_directory.parents):
                if ancestor.is_symlink() and (ancestor not in system_aliases
                                               or ancestor.resolve() != system_aliases[ancestor]
                                               or ancestor.lstat().st_uid != 0):
                    raise ValueError("Sample data requires a regular private directory.")
            board_directory = board_directory.resolve()
            board_directory = board_directory / "sample-board"
            board_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = board_directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError("Sample data requires a regular private directory.")
        self.store = BoardStore(board_directory / "board.sqlite3")
        self.sandbox = sandbox
        self.store_profile = store_profile
        self.sample_mode = sample_mode
        self.desktop_chat_sources = desktop_chat_sources
        self.hidden_desktop_providers = DESKTOP_CHAT_URLS.keys() - desktop_chat_sources.keys()
        self.sources = [] if sample_mode else [
            _UnselectedSource("codex") if not source_allowed["codex"] else
            CodexSource(codex_root, confine_paths=True, snapshot_parent=board_directory) if sandbox else CodexSource(codex_root),
            _UnselectedSource("claude") if not source_allowed["claude"] else
            ClaudeSource(claude_root, desktop_root=Path(claude_desktop_root) / "claude-code-sessions",
                         desktop_scope_root=Path(claude_desktop_root),
                         allow_commands=False, confine_paths=True) if sandbox else ClaudeSource(claude_root)]
        self.interval = interval
        self.health = []
        self.lock = threading.RLock()
        # This lock never covers storage or provider reads, so a stalled scan
        # cannot also prevent the HTTP endpoint from reporting stale health.
        self.refresh_lock = threading.Lock()
        self.refresh_state = {name: {"completed": time.monotonic(), "failed": False}
                              for name in ("status", "projects")}
        self.stop = threading.Event()
        self.last_scan = None
        self.token = secrets.token_urlsafe(32)
        self.project_source = ClaudeProjectSource() if not sandbox and projects_enabled and codex_root is None and claude_root is None else None
        self.project_health = ({"id": "claude_projects", "name": "Claude projects", "available": False,
                                "detail": "Claude project status is connecting.", "last_seen": None}
                               if self.project_source else None)
        # Usage has its own worker and lock. Network or Keychain delays cannot
        # prevent the local conversation board from refreshing or being reviewed.
        self.usage_source = UsageSource() if not sandbox and usage_enabled and codex_root is None and claude_root is None else None
        self.usage_lock = threading.Lock()
        self.local_claude_quota = ClaudeLocalQuota(claude_root) if sandbox and source_grants["claude"] else None
        self.local_codex_usage = None
        self.local_codex_granted = sandbox and source_grants["codex"]
        self.usage = [{"id": provider, "name": name, "available": False, "stale": False,
                       "loading": self.usage_source is not None, "updated_at": None, "windows": [],
                       "error": None if self.usage_source is not None else (
                           "Account usage is unavailable in this sandbox preview while provider integration is being verified."
                           if sandbox else "Account usage is turned off or unavailable for these sources.")}
                      for provider, name in (("claude", "Claude"), ("codex", "Codex"))]
        if self.sample_mode:
            with self.store.lock, self.store.connect() as db:
                keys = {row[0] for row in db.execute("SELECT key FROM sessions")}
            if keys != {"codex:sample-codex", "claude:sample-claude"}:
                self.advance_samples("reset")
            self.scan()
            sample_now = time.time()
            self.usage = [{"id": provider, "name": name, "source": "sample", "sample": True,
                           "available": True, "updated_at": None, "stale": False, "loading": False,
                           "error": "Sample values, not your account usage.",
                           "windows": [{"id": "weekly", "remaining_percent": percent, "reset_at": sample_now + reset}],
                           "credits": {"amount": balance, "unit": unit}}
                          for provider, name, percent, reset, balance, unit in (
                              ("claude", "Claude", 91, 172800, 25, "USD"),
                              ("codex", "Codex", 64, 259200, 240, "credits"))]

    def advance_samples(self, action):
        """Change only the two built-in examples in the isolated sample store."""
        if not self.sample_mode:
            raise ValueError("Sample controls are unavailable on the live board.")
        if not isinstance(action, str) or action not in {"cooking", "needs_you", "ready", "reset"}:
            raise ValueError("Choose Cooking, Needs you, Ready, or Reset samples.")
        with self.lock:
            now = time.time()
            with self.store.lock, self.store.connect() as db:
                row = db.execute("SELECT value FROM metadata WHERE key='sample_sequence'").fetchone()
                sequence = (int(row[0]) + 1 if row and isinstance(row[0], str) and len(row[0]) <= 12
                            and row[0].isascii() and row[0].isdigit() else 1)
                if action == "reset":
                    for table in ("sessions", "session_preferences", "background_sessions"):
                        db.execute("DELETE FROM " + table)
                latest = db.execute("SELECT max(updated_at) FROM sessions").fetchone()[0]
                if BoardStore._finite_number(latest):
                    now = max(now, latest + .000001)
                now = max(now, self.store.activated_at)
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('sample_sequence', ?)", (str(sequence),))
            status = "cooking" if action == "reset" else action
            details = {"cooking": "This sample agent is working.",
                       "needs_you": "This sample chat is waiting for your input.",
                       "ready": "Your sample reply is ready to review."}
            preview = ("This is a sample reply. You can review, rename or snooze this chat without changing your real board."
                       if status == "ready" else "")
            self.store.apply([{"provider": provider, "id": "sample-" + provider,
                               "title": "Sample " + name + " chat", "cwd": "Sample project",
                               "status": status, "detail": details[status], "source_kind": "sample",
                               "event_id": "sample:" + str(sequence), "updated_at": now, "started_at": now,
                               "can_open": False, "preview": preview, "preview_kind": "assistant"}
                              for provider, name in (("codex", "Codex"), ("claude", "Claude"))], now)
            self.scan()

    def _refresh_result(self, name, success):
        with self.refresh_lock:
            self.refresh_state[name]["failed"] = not success
            if success:
                self.refresh_state[name]["completed"] = time.monotonic()

    @staticmethod
    def _warn(message, *args):
        # The same storage outage can also make the collector log unwritable.
        # Logging must never become a second failure in a recovery handler.
        try:
            LOG.warning(message, *args)
        except Exception:
            pass

    def refresh_health(self):
        """In-memory collector health, independent of conversation timestamps."""
        now = time.monotonic()
        with self.refresh_lock:
            result = {name: not state["failed"] and 0 <= now - state["completed"] <= limit
                      for name, state in self.refresh_state.items()
                      for limit in (max(STATUS_HEARTBEAT_SECONDS, self.interval * 3)
                                    if name == "status" else PROJECT_HEARTBEAT_SECONDS,)}
        return result

    def _health_snapshot(self):
        fresh = self.refresh_health()
        local = copy.deepcopy(self.health)
        if not fresh["status"]:
            if not local:
                local = [{"id": provider, "name": name, "last_seen": None}
                         for provider, name in (("codex", "Codex"), ("claude", "Claude Code"))]
                local.extend({"id": source.source_id, "name": source.name + " chats", "last_seen": None}
                             for source in self.desktop_chat_sources.values())
            for item in local:
                item.update(available=False, detail="Conversation status is not refreshing. Its last saved state is unconfirmed.")
        project = copy.deepcopy(self.project_health)
        if project and not fresh["projects"]:
            project.update(available=False, detail="Claude project status is not refreshing. Its last saved state is unconfirmed.")
        return local + ([project] if project else [])

    def scan(self):
        # Also protects the initial scan, which runs before the HTTP listener
        # and retry workers exist. Recovery must not require another DB write.
        try:
            self._scan()
        except Exception:
            self._refresh_result("status", False)
            self._warn("Conversation refresh failed; retrying without recording source contents.")
        else:
            self._refresh_result("status", True)

    def _scan(self):
        with self.lock:
            if self.sample_mode:
                self.last_scan = time.time()
                self.health = [{"id": "samples", "name": "Sample data", "available": True, "sample": True,
                                "detail": "Sample data only. Real provider folders and accounts are not accessed.",
                                "last_seen": self.last_scan}]
                return
            health = []
            scan_failed = False
            local_codex_readings = []
            now = time.time()
            for source in self.sources:
                if isinstance(source, _UnselectedSource):
                    provider = source.provider
                    detail = ("Choose the Codex folder in Setup and Permissions to connect this source."
                              if provider == "codex" else
                              "Choose both Claude folders in Setup and Permissions to connect this source.")
                    self.store.mark_unavailable(provider, detail, source_kind="local" if provider == "claude" else None)
                    health.append({"id": provider, "name": "Codex" if provider == "codex" else "Claude Code",
                                   "available": False, "detail": detail, "last_seen": None})
                    continue
                provider = "codex" if isinstance(source, CodexSource) else "claude"
                try:
                    source.revision_lookup = lambda ident, provider=provider: self.store.transcript_revision(ident, provider)
                    if provider == "claude":
                        source.tracked_ids = {ident for ident in self.store.session_ids("claude")
                                              if not ident.startswith(("project:", DESKTOP_CHAT_ID_PREFIX))}
                    observations, status = source.scan(now, self.store.activated_at)
                    if self.sandbox and provider == "codex":
                        for observation in observations:
                            if observation.get("history_checking") is True:
                                continue
                            reading = parse_codex_observation(observation.get("local_rate_limits"),
                                                              observation.get("local_rate_limits_at"), now=now)
                            if reading is not None:
                                local_codex_readings.append(reading)
                    self.store.set_background_sessions(provider, getattr(source, "background_reasons", {}), now)
                    self.store.apply(observations, now)
                    status = dict(status)
                    status.setdefault("id", provider)
                    status.setdefault("name", "Codex" if provider == "codex" else "Claude Code")
                    status.setdefault("last_seen", now)
                    if not status.get("available"):
                        self.store.mark_unavailable(provider, "The source is unavailable. Its last working state is unconfirmed.", source_kind="local" if provider == "claude" else None)
                    else:
                        present_keys = set()
                        for observation in observations:
                            checking = observation.get("history_checking") is True
                            if observation.get("metadata_only") is True and not checking:
                                continue
                            normalized = self.store.normalize(observation, now)
                            if normalized is not None:
                                stored = self.store.get(normalized["key"])
                                if (stored and normalized["updated_at"] < stored["updated_at"] and not checking
                                        and not (observation.get("confirmed_live") is True
                                                 and normalized["status"] == stored["status"])):
                                    continue
                                present_keys.add(normalized["key"])
                        self.store.mark_unavailable(provider,
                            "This conversation was not found in the latest source scan. Its last working state is unconfirmed.",
                            present_keys=present_keys, source_kind="local" if provider == "claude" else None)
                    health.append(status)
                except Exception:
                    # Provider exceptions can contain paths, chat text or token
                    # values. Keep diagnostics local and free of their contents.
                    scan_failed = True
                    self._warn("%s source scan failed; retrying without recording source contents.", provider)
                    health.append({"id": provider, "name": "Codex" if provider == "codex" else "Claude Code", "available": False,
                                   "detail": "Local session data could not be read. Retrying automatically.", "last_seen": self.last_scan})
            for source in self.desktop_chat_sources.values():
                try:
                    health.append(source.scan(self.store, time.time()))
                except Exception:
                    scan_failed = True
                    self._warn("Regular chat refresh failed; retrying without recording source contents.")
                    health.append({"id": source.source_id, "name": source.name + " chats", "available": False,
                                   "detail": "Chat status could not be refreshed. Retrying automatically.", "last_seen": self.last_scan})
            self.health = health
            if not scan_failed:
                self.last_scan = time.time()
            if self.sandbox:
                self.refresh_local_usage(local_codex_readings, now)

    def refresh_local_usage(self, readings, now):
        """Expose provider observations already on this Mac, without account access."""
        if readings and self.local_codex_granted:
            newest = max(readings, key=lambda reading: reading["updated_at"])
            if self.local_codex_usage is None or newest["updated_at"] >= self.local_codex_usage["updated_at"]:
                self.local_codex_usage = copy.deepcopy(newest)
        if self.local_codex_usage is not None and self.local_codex_granted:
            codex = copy.deepcopy(self.local_codex_usage)
            expired = any(window["reset_at"] <= now for window in codex["windows"])
            codex["stale"] = now - codex["updated_at"] > STALE_SECONDS or expired or not readings
            codex["error"] = ("Waiting for another verified local usage reading."
                              if codex["stale"] else None)
            if "credits" in codex:
                codex["credits"]["stale"] = now - codex["credits"]["observed_at"] > STALE_SECONDS or not readings
        else:
            codex = {"id": "codex", "name": "Codex", "source": "local_record", "available": False,
                     "updated_at": None, "stale": False, "loading": False, "windows": [],
                     "error": ("Waiting for a local Codex session to report weekly usage."
                               if self.local_codex_granted else "Choose the Codex folder to show local usage.")}
        claude = (self.local_claude_quota.scan(now) if self.local_claude_quota else
                  {"id": "claude", "name": "Claude", "source": "local_export", "available": False,
                   "updated_at": None, "stale": False, "loading": False, "windows": [],
                   "error": "Choose the Claude Sessions folder to read its local usage export."})
        with self.usage_lock:
            self.usage = [claude, codex]

    def run(self):
        while not self.stop.is_set():
            try:
                self.scan()
            except Exception:
                # Keep the retry loop alive even if a future scan wrapper has
                # a bug. Error reporting here remains entirely in memory.
                self._refresh_result("status", False)
                self._warn("Conversation collector interrupted; retrying without recording source contents.")
            self.stop.wait(self.interval)

    def run_usage(self):
        if self.usage_source is None:
            return
        while not self.stop.is_set():
            try:
                result = self.usage_source.scan(time.time())
                if not isinstance(result, list):
                    raise ValueError("Invalid usage update")
                with self.usage_lock:
                    self.usage = copy.deepcopy(result)
            except Exception:
                self._warn("Usage refresh failed; retaining explicitly stale values until retry.")
                with self.usage_lock:
                    for item in self.usage:
                        item.update(available=False, loading=False,
                                    stale=bool(item.get("updated_at")), error="Usage could not be refreshed.")
                        if "credits" in item:
                            item["credits"]["stale"] = True
            self.stop.wait(60)

    def scan_projects(self):
        if self.project_source is None:
            return
        try:
            self._scan_projects()
        except Exception:
            self._refresh_result("projects", False)
            self._warn("Project refresh failed; retrying without recording source contents.")
        else:
            self._refresh_result("projects", True)

    def _scan_projects(self):
        # Cloud account reads never hold the lock used by local status and UI
        # actions. The result is applied atomically after its bounded request.
        with self.lock:
            self.project_source.tracked_ids = {ident for ident in self.store.session_ids("claude") if ident.startswith("project:")}
        now = time.time()
        observations, health = self.project_source.scan(now, self.store.activated_at)
        with self.lock:
            received_at = time.time()
            self.store.apply(observations, received_at)
            present = {row["provider"] + ":" + row["id"] for row in observations if self.store.normalize(row, received_at) is not None}
            # A partial response can still confirm its successful rows. Missing
            # working rows become uncertain without changing completed replies.
            self.store.mark_unavailable("claude", "The project's working state could not be confirmed in the latest check.",
                                        present_keys=present, source_kind="project")
            self.project_health = health

    def run_projects(self):
        if self.project_source is None:
            return
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.scan_projects()
            except Exception:
                self._refresh_result("projects", False)
                self._warn("Project collector interrupted; retrying without recording source contents.")
            # Keep a start-to-start cadence. Slow history verification must
            # not add another full polling interval to the visible state lag.
            self.stop.wait(max(0, 15 - (time.monotonic() - started)))

    def snapshot(self):
        if not self.lock.acquire(timeout=SNAPSHOT_LOCK_TIMEOUT):
            raise OSError("The local collector is still refreshing.")
        try:
            snapshot = self.store.snapshot(hidden_desktop_providers=self.hidden_desktop_providers)
            now = time.time()
            for provider, source in self.desktop_chat_sources.items():
                idle = source.open_chats(now)
                saved = self.store.session_ids(provider, candidates=idle)
                snapshot["background"].extend(item for ident, item in idle.items() if ident not in saved)
            sources = self._health_snapshot()
            source_health = {source.get("id"): source for source in sources}
            for group in ("sessions", "history", "background"):
                for item in snapshot[group]:
                    if self.sample_mode:
                        item.update({"sample": True, "can_open": False, "source_unavailable": False,
                                     "source_unavailable_detail": "", "notification_eligible": False})
                        continue
                    source_id = item["provider"]
                    desktop_chat = (self.desktop_chat_sources.get(source_id)
                                    if item.get("source_kind") == "desktop_chat" else None)
                    if desktop_chat is not None:
                        source_id = desktop_chat.source_id
                    elif source_id == "claude" and (item.get("source_kind") == "project"
                                                   or item["id"].startswith("project:")):
                        source_id = "claude_projects"
                    health = source_health.get(source_id)
                    unavailable = health is None or health.get("available") is not True
                    detail = health.get("detail") if health else None
                    owns_saved_event = getattr(desktop_chat, "owns_saved_event", None)
                    if (callable(owns_saved_event) and not item.get("catalog_only")
                            and not owns_saved_event(item)):
                        unavailable = True
                        detail = "This saved chat belongs to a different or unconfirmed sign-in."
                        item["notification_eligible"] = False
                    if (desktop_chat is not None and not unavailable and item["status"] == "cooking"
                            and item["id"] not in desktop_chat.cooking):
                        unavailable = True
                        detail = f"This chat was not observed in the latest {desktop_chat.name} status check."
                    item["source_unavailable"] = unavailable
                    item["source_unavailable_detail"] = (
                        " ".join(detail.split())[:240] if isinstance(detail, str) and detail.strip()
                        else "The source is unavailable. This is its last saved state."
                    ) if unavailable else ""
            snapshot.update({"sources": sources, "now": now, "last_scan": self.last_scan,
                             "csrf_token": self.token, "poll_interval": self.interval, "sample_mode": self.sample_mode})
            with self.usage_lock:
                snapshot["usage"] = copy.deepcopy(self.usage)
            return snapshot
        except (sqlite3.DatabaseError, OSError):
            # A failed read cannot leave the last successful footer claiming
            # connected while the store is unavailable. No DB fallback here.
            self._refresh_result("status", False)
            self._refresh_result("projects", False)
            raise
        finally:
            self.lock.release()

    def open_desktop_chat(self, key):
        with self.lock:
            provider, _, ident = key.partition(":")
            source = self.desktop_chat_sources.get(provider)
            return source.open_chats(time.time()).get(ident) if source is not None else None


class BoardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32
    max_connections = 32
    request_timeout = 5

    def server_bind(self):
        self._connections = threading.BoundedSemaphore(self.max_connections)
        self._warning_lock = threading.Lock()
        self._next_request_warning = 0
        # HTTPServer resolves its own address after binding. This loopback-only
        # service must start even when the Mac's reverse-DNS resolver is stalled.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]

    def process_request(self, request, client_address):
        # A stalled local client must not create an unbounded number of threads.
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentBoard/1"

    def handle(self):
        self.connection.settimeout(self.server.request_timeout)
        # Socket inactivity timeouts alone still permit an endless trickle of
        # header bytes. The deadline includes the initial request line.
        deadline = threading.Timer(self.server.request_timeout, self.expire_request)
        deadline.daemon = True
        self._header_deadline = deadline
        deadline.start()
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            deadline.cancel()

    def expire_request(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def parse_request(self):
        try:
            return super().parse_request()
        finally:
            self._header_deadline.cancel()

    def log_message(self, fmt, *args):
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            # Rejected traffic can contain private chat IDs or terminal control
            # bytes. Neither its contents nor unlimited log lines are useful.
            with self.server._warning_lock:
                now = time.monotonic()
                if now >= self.server._next_request_warning:
                    LOG.warning("A local dashboard request was rejected (HTTP %s).", args[1])
                    self.server._next_request_warning = now + 5

    @property
    def monitor(self):
        return self.server.monitor

    def valid_host(self):
        port = self.server.server_port
        return self.headers.get_all("Host", []) in ([f"127.0.0.1:{port}"], [f"localhost:{port}"])

    def authorized_display(self):
        token = getattr(self.server, "access_token", None)
        return token is None or self.matches_header("X-Agent-Board-Access", token)

    def matches_header(self, name, token):
        if len(self.headers.get_all(name, [])) != 1:
            return False
        try:
            supplied = self.headers.get(name, "").encode("ascii")
        except UnicodeEncodeError:
            return False
        return secrets.compare_digest(supplied, token.encode("ascii"))

    def request_path(self):
        # This server accepts only origin-form local paths, never proxy URLs.
        if not self.path.startswith("/") or self.path.startswith("//"):
            raise ValueError("Use a local dashboard path.")
        return urlsplit(self.path).path

    def respond(self, code, body, kind="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            try:
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                code = 503
                body = b'{"error":"The local board state could not be read. Quit and reopen Agent Board, or restore a local backup."}'
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def do_GET(self):
        if not self.valid_host():
            return self.respond(403, {"error": "Use the local dashboard address."})
        try:
            path = self.request_path()
        except ValueError:
            return self.respond(400, {"error": "Use a local dashboard path."})
        if path.startswith("/api/") and not self.authorized_display():
            return self.respond(403, {"error": "Open this board in the Agent Board app."})
        if path == "/api/health":
            return self.respond(200, {"ok": True, "app": "agent-board", "instance_id": getattr(self.server, "instance_id", None),
                                      "last_scan": self.monitor.last_scan, "collectors": self.monitor.refresh_health()})
        if path == "/api/state":
            try:
                return self.respond(200, self.monitor.snapshot())
            except (sqlite3.DatabaseError, OSError):
                return self.respond(503, {"error": "Local board storage is unavailable. Retry, or quit and reopen Agent Board."})
        if self.monitor.store_profile and path in ("/icons/claude.svg", "/icons/codex.svg"):
            return self.respond(404, {"error": "Page not found."})
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/index.html": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8"),
                  "/palettes.css": ("palettes.css", "text/css; charset=utf-8"),
                  "/themes.js": ("themes.js", "text/javascript; charset=utf-8"),
                  "/effects.js": ("effects.js", "text/javascript; charset=utf-8"),
                  "/effects.css": ("effects.css", "text/css; charset=utf-8"),
                  "/icons/claude.svg": ("icons/claude.svg", "image/svg+xml"),
                  "/icons/codex.svg": ("icons/codex.svg", "image/svg+xml"),
                  "/designs/": ("designs/index.html", "text/html; charset=utf-8"),
                  "/designs/designs.css": ("designs/designs.css", "text/css; charset=utf-8"),
                  "/designs/designs.js": ("designs/designs.js", "text/javascript; charset=utf-8"),
                  "/colors/": ("colors/index.html", "text/html; charset=utf-8"),
                  "/colors/colors.css": ("colors/colors.css", "text/css; charset=utf-8"),
                  "/colors/colors.js": ("colors/colors.js", "text/javascript; charset=utf-8"),
                  "/about/": ("about/index.html", "text/html; charset=utf-8"),
                  "/about/about.css": ("about/about.css", "text/css; charset=utf-8"),
                  "/about/about.js": ("about/about.js", "text/javascript; charset=utf-8"),
                  "/about/ko-fi.svg": ("about/ko-fi.svg", "image/svg+xml"),
                  "/about/ko-fi.png": ("about/ko-fi.png", "image/png")}
        if self.monitor.store_profile:
            assets["/about/privacy/"] = ("about/privacy.html", "text/html; charset=utf-8")
        for sound in ("soft-ding", "chime", "bell", "glass", "marimba", "arcade", "radar", "space-signal", "pop", "fart"):
            assets["/sounds/" + sound + ".wav"] = ("sounds/" + sound + ".wav", "audio/wav")
        if path not in assets:
            return self.respond(404, {"error": "Page not found."})
        name, kind = assets[path]
        try:
            content = (ROOT / "static" / name).read_bytes()
            if self.monitor.store_profile and kind.startswith("text/html"):
                # A fixed presentation marker covers the board and generated
                # theme examples without introducing a user-controlled value.
                content = content.replace(b"<html ", b'<html data-provider-marks="off" ', 1)
            if name == "about/index.html":
                # About describes the running build, including its storage and
                # credential boundaries, without exposing a user's file path.
                content = content.replace(b"<!-- runtime-build -->", BUILD_LABEL.encode("ascii"))
                content = content.replace(b" data-direct-only", b" hidden" if self.monitor.sandbox else b"")
                content = content.replace(b" data-sandbox-only hidden", b"" if self.monitor.sandbox else b" hidden")
                content = content.replace(b" data-store-excluded", b" hidden" if self.monitor.store_profile else b"")
            return self.respond(200, content, kind)
        except FileNotFoundError:
            return self.respond(503, {"error": "The dashboard files are unavailable."})

    def do_POST(self):
        if not self.valid_host():
            return self.respond(403, {"error": "Use the local dashboard address."})
        if not self.authorized_display():
            return self.respond(403, {"error": "Open this board in the Agent Board app."})
        origins = self.headers.get_all("Origin", [])
        origin = self.headers.get("Origin")
        allowed = (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}")
        if len(origins) > 1 or (origin and origin not in allowed) or not self.matches_header("X-Agent-Board-Token", self.monitor.token):
            return self.respond(403, {"error": "Refresh the dashboard before trying again."})
        try:
            if self.headers.get_all("Transfer-Encoding", []) or len(self.headers.get_all("Content-Length", [])) > 1:
                raise ValueError()
            length = int(self.headers.get("Content-Length", 0))
            if length < 0 or length > 16384:
                return self.respond(413, {"error": "Request is too large."})
            remaining = length
            chunks = []
            deadline = time.monotonic() + self.server.request_timeout
            while remaining:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise TimeoutError()
                self.connection.settimeout(timeout)
                chunk = self.rfile.read1(remaining)
                if not chunk:
                    raise ValueError()
                chunks.append(chunk)
                remaining -= len(chunk)
            def unique_fields(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            body = json.loads(b"".join(chunks) or b"{}",
                              object_pairs_hook=unique_fields if self.monitor.sample_mode else None)
            if not isinstance(body, dict):
                raise ValueError()
        except TimeoutError:
            return self.respond(408, {"error": "The request timed out. Retry the action."})
        except (ValueError, UnicodeDecodeError, RecursionError):
            return self.respond(400, {"error": "Request must contain a JSON object."})
        try:
            pieces = self.request_path().split("/")
        except ValueError:
            return self.respond(400, {"error": "Use a local dashboard path."})
        if pieces == ["", "api", "samples", "advance"]:
            if not self.monitor.sample_mode:
                return self.respond(404, {"error": "Sample controls are unavailable on the live board."})
            if set(body) != {"action"}:
                return self.respond(400, {"error": "Choose one sample action."})
            try:
                self.monitor.advance_samples(body["action"])
            except ValueError as exc:
                return self.respond(400, {"error": str(exc)})
            except (OSError, sqlite3.DatabaseError):
                return self.respond(503, {"error": "Sample board storage is unavailable. Retry or reset the sample board."})
            return self.respond(200, {"ok": True})
        if len(pieces) != 5 or pieces[1:3] != ["api", "sessions"]:
            return self.respond(404, {"error": "Action not found."})
        key, action = unquote(pieces[3]), pieces[4]
        try:
            provider, _, ident = key.partition(":")
            if provider in self.monitor.hidden_desktop_providers and ident.startswith(DESKTOP_CHAT_ID_PREFIX):
                raise KeyError(key)
            # Scan before acknowledging to prevent a stale card clearing a newly started turn.
            if action == "review":
                with self.monitor.lock:
                    self.monitor.scan()
                    self.monitor.store.review(key, body.get("event_id"))
            elif action == "restore":
                self.monitor.store.restore(key)
            elif action == "rename":
                self.monitor.store.rename(key, body.get("title"))
            elif action == "move":
                self.monitor.store.move(key, body.get("placement"))
            elif action == "snooze":
                with self.monitor.lock:
                    self.monitor.scan()
                    self.monitor.store.snooze(key, body.get("event_id"), body.get("minutes", 10))
            elif action == "open":
                if self.monitor.sample_mode:
                    return self.respond(409, {"error": "Sample chats cannot open a provider app."})
                session = self.monitor.store.get(key)
                if session is None:
                    session = self.monitor.open_desktop_chat(key)
                if session is None:
                    raise KeyError(key)
                if not session["can_open"]:
                    return self.respond(409, {"error": "Opening this conversation is not supported yet."})
                if self.monitor.sandbox:
                    return self.respond(200, {"ok": True, "native_open_url": chat_url(session),
                                              "provider": session["provider"]})
                if sys.platform != "darwin":
                    return self.respond(409, {"error": "Opening conversations requires the Mac app."})
                command = open_command(session)
                subprocess.run(command, check=True, timeout=8, capture_output=True)
            else:
                return self.respond(404, {"error": "Action not found."})
        except Conflict as exc:
            return self.respond(409, {"error": str(exc)})
        except UnsupportedChatLink as exc:
            return self.respond(409, {"error": str(exc)})
        except ValueError as exc:
            return self.respond(400, {"error": str(exc)})
        except KeyError:
            return self.respond(404, {"error": "This conversation is no longer on the board."})
        except (OSError, subprocess.SubprocessError):
            return self.respond(502, {"error": "The app could not be opened. You can retry or open it from the Dock."})
        except sqlite3.DatabaseError:
            return self.respond(503, {"error": "Local board storage is unavailable. Retry, or quit and reopen Agent Board."})
        return self.respond(200, {"ok": True})


def read_access_token(path):
    """Read the native launcher secret without following a substituted symlink."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size > 128):
            raise ValueError("The display access file must be owned by you and private (mode 0600).")
        token = os.read(descriptor, 129).decode("ascii").strip()
        if len(token) != 64 or any(char not in "0123456789abcdef" for char in token):
            raise ValueError("The display access file is invalid.")
        return token
    finally:
        os.close(descriptor)


class NativeLaunchFiles:
    """Remove only this validated native launch's unchanged private files.

    The open private-directory descriptor and file fingerprints prevent a
    subsequent launch, shared file, symlink, modified file or moved checkpoint
    from being mistaken for this collector's disposable launch material.
    No directory is created, and failed validation confers no cleanup rights.
    """

    def __init__(self, data_dir, instance_id, token_path, token, manifest_path=None, *, parent_lifetime=False, manifest_fingerprint=None):
        self.directory = None
        self.entries = {}
        self.path = None
        self.identity = None
        if not parent_lifetime or not isinstance(instance_id, str) or not isinstance(token, str):
            return
        try:
            if str(uuid.UUID(instance_id)).lower() != instance_id.lower():
                return
            path = Path(data_dir)
            if not path.is_absolute() or ".." in path.parts or path.is_symlink():
                return
            path = path.resolve(strict=True)
            token_path = Path(token_path)
            token_name = ".collector-access-" + instance_id
            if token_path != path / token_name:
                return
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            self.directory, self.path = descriptor, path
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
                self.cleanup()
                return
            self.identity = (info.st_dev, info.st_ino)
            captured = self._read(token_name, 128)
            if captured is None or captured[0].decode("ascii").strip() != token:
                self.cleanup()
                return
            self.entries[token_name] = captured[1]
            manifest_name = ".source-bookmarks-" + instance_id + ".json"
            if manifest_path is not None and Path(manifest_path) == path / manifest_name:
                captured = self._read(manifest_name, 262_144)
                if captured is not None and captured[1] == manifest_fingerprint:
                    self.entries[manifest_name] = captured[1]
        except (OSError, ValueError, TypeError, UnicodeError):
            # In particular, an invalid access-token read must not authorize
            # deletion of a similarly named source file or checkpoint.
            self.entries.clear()
            self.cleanup()

    def _read(self, name, limit):
        descriptor = None
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=self.directory)
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600
                    or before.st_uid != os.getuid() or before.st_nlink != 1 or before.st_size > limit):
                return None
            raw = bytearray()
            while len(raw) <= limit:
                chunk = os.read(descriptor, min(65_536, limit + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(descriptor)
            signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            if len(raw) > limit or signature(before) != signature(after):
                return None
            return bytes(raw), signature(after) + (hashlib.sha256(raw).digest(),)
        except OSError:
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def cleanup(self):
        removed = []
        if self.directory is None:
            return removed
        try:
            current = self.path.lstat()
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != self.identity:
                return removed
            for name, expected in self.entries.items():
                captured = self._read(name, 262_144)
                if captured is None or captured[1] != expected:
                    continue
                latest = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
                if (latest.st_dev, latest.st_ino) != expected[:2]:
                    continue
                os.unlink(name, dir_fd=self.directory)
                removed.append(name)
        except OSError:
            pass
        finally:
            os.close(self.directory)
            self.directory = None
            self.entries.clear()
        return removed


def validate_native_chat_launch(launch_files, instance_id):
    if (not isinstance(instance_id, str) or str(uuid.UUID(instance_id)).upper() != instance_id
            or launch_files.directory is None or ".collector-access-" + instance_id not in launch_files.entries):
        raise ValueError("Chat monitoring requires the native launch identity.")


class DesktopChatSource:
    """Confirm only Cooking followed by Idle in consecutive observed snapshots."""

    MAX_AGE = 12
    MAX_BYTES = 65_536
    MAX_CHATS = 64
    DIAGNOSTIC_FLAGS = {"deadline_reached", "request_limit_reached"}
    DIAGNOSTIC_COUNTS = {
        "apps", "windows", "visible_windows", "web_areas", "page_urls", "shell_urls",
        "accepted_shells", "navigation_landmarks", "history_landmarks", "history_links",
        "current_links", "current_link_urls", "walk_limits", "role_failures", "searched_history_links",
        "recognized_pages", "text_inputs", "composers", "stop_controls", "idle_controls", "classifications",
        "complete_idle_scopes", "incomplete_control_scopes", "child_count_unavailable",
        "incomplete_child_batches", "empty_child_values",
        "child_truncations", "child_count_mismatches", "child_copy_failures", "depth_truncations",
        "max_child_count", "control_read_failures", "control_child_failures", "control_depth_truncations",
        "max_control_child_count",
    }

    def __init__(self, launch_files, path, instance_id):
        validate_native_chat_launch(launch_files, instance_id)
        name = self.provider + "-chat-status-" + instance_id + ".json"
        token_name = ".collector-access-" + instance_id
        if (launch_files.directory is None or token_name not in launch_files.entries
                or path != launch_files.path / name):
            raise ValueError("Desktop chat status requires this launch's private file.")
        self.launch_files = launch_files
        self.filename = name
        self.source_id = self.provider + "_chats"
        self.cooking = {}
        self.idle = {}
        self.observed_at = None
        self.content_hash = None
        self.available = False

    @staticmethod
    def unique_object(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("Duplicate status fields.")
        return value

    def valid_snapshot_shape(self, value):
        if isinstance(value, dict) and set(value) == {"observed_at", "chats"}:
            return True
        if not isinstance(value, dict) or set(value) != {"observed_at", "chats", "diagnostics"}:
            return False
        diagnostics = value["diagnostics"]
        return (isinstance(diagnostics, dict) and len(diagnostics) <= 40
                and diagnostics.keys() <= self.DIAGNOSTIC_COUNTS | self.DIAGNOSTIC_FLAGS
                and all(type(number) is int and 0 <= number <= (1 if key in self.DIAGNOSTIC_FLAGS else 10_000)
                        for key, number in diagnostics.items()))

    def read_snapshot(self, now):
        directory = self.launch_files.path.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or stat.S_IMODE(directory.st_mode) != 0o700
                or directory.st_uid != os.getuid()
                or (directory.st_dev, directory.st_ino) != self.launch_files.identity):
            raise ValueError("Desktop chat status directory changed.")
        captured = self.launch_files._read(self.filename, self.MAX_BYTES)
        if captured is None:
            raise ValueError("Desktop chat status is unavailable.")
        raw, fingerprint = captured
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=self.unique_object)
        if not self.valid_snapshot_shape(value):
            raise ValueError("Invalid desktop chat status.")
        observed = value["observed_at"]
        rows = value["chats"]
        if (not BoardStore._finite_number(observed) or not now - self.MAX_AGE <= observed <= now + 2
                or not isinstance(rows, list) or len(rows) > self.MAX_CHATS):
            raise ValueError("Invalid or expired desktop chat status.")
        chats = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"id", "title", "status"}:
                raise ValueError("Invalid desktop chat.")
            ident, title, status = row["id"], row["title"], row["status"]
            if (not valid_desktop_chat_id(self.provider, ident) or ident in chats
                    or not isinstance(title, str) or len(title) > 500
                    or any(0xd800 <= ord(char) <= 0xdfff for char in title)
                    or status not in ("cooking", "idle")):
                raise ValueError("Invalid desktop chat.")
            title = " ".join(title.split())
            if not title and not DESKTOP_CHAT_ID.fullmatch(ident):
                raise ValueError("An unlinked desktop chat requires an observed title.")
            chats[ident] = (title or f"{self.name} chat", status)
        return observed, fingerprint[-1], chats

    def scan(self, store, now):
        try:
            observed, content_hash, chats = self.read_snapshot(now)
            if self.observed_at is not None:
                if observed < self.observed_at or (observed == self.observed_at and content_hash != self.content_hash):
                    raise ValueError("Desktop chat status is out of order.")
                if observed == self.observed_at:
                    return self.health()
            prior = (self.cooking if self.observed_at is not None
                     and observed - self.observed_at <= self.MAX_AGE else {})
            observations, cooking, idle = [], {}, {}
            for ident, (title, state) in chats.items():
                session_id = DESKTOP_CHAT_ID_PREFIX + ident
                previous = prior.get(session_id)
                if previous and not DESKTOP_CHAT_ID.fullmatch(ident) and previous[1] != title:
                    previous = None
                if state == "cooking":
                    started = previous[0] if previous else observed
                    cooking[session_id] = (started, title)
                    status, updated, detail = "cooking", started, f"{self.name} Desktop is responding."
                else:
                    idle[session_id] = title
                    if previous is None:
                        continue
                    started = previous[0]
                    status, updated, detail = "ready", observed, f"Review the latest activity in {self.name} Desktop."
                observations.append({"provider": self.provider, "source_kind": "desktop_chat", "id": session_id,
                                     "title": title, "status": status, "detail": detail, "started_at": started,
                                     "updated_at": updated, "event_id": f"desktop-chat:{ident}:{started}",
                                     "confirmed_live": state == "cooking", **self.link_fields(ident)})
            if observations:
                store.apply(observations, now)
            self.cooking, self.idle = cooking, idle
            self.observed_at, self.content_hash = observed, content_hash
            self.available = True
        except (OSError, ValueError, RecursionError, sqlite3.DatabaseError):
            # Loss of observation breaks the lifecycle without rewriting a
            # saved reply or its Review acknowledgement.
            self.cooking.clear()
            self.idle.clear()
            self.available = False
        return self.health()

    def open_chats(self, now):
        if not self.available or self.observed_at is None or not now - self.MAX_AGE <= self.observed_at <= now + 2:
            return {}
        return {ident: {"key": self.provider + ":" + ident, "provider": self.provider, "id": ident,
                        "title": title, "source_kind": "desktop_chat", "open_only": True, "status": "disconnected",
                        "detail": f"This chat is open in {self.name}. No reply status was observed.",
                        "updated_at": self.observed_at, "notification_eligible": False,
                        **self.link_fields(ident[len(DESKTOP_CHAT_ID_PREFIX):])}
                for ident, title in self.idle.items()}

    def link_fields(self, ident):
        if DESKTOP_CHAT_ID.fullmatch(ident):
            return {"can_open": True, "open_url": DESKTOP_CHAT_URLS[self.provider] + ident,
                    "open_label": self.open_label}
        return {"can_open": False, "open_label": self.open_label,
                "open_error": "ChatGPT did not expose a verified link for this window."}

    def health(self):
        return {"id": self.source_id, "name": f"{self.name} chats", "available": self.available,
                "detail": (f"Monitoring observed {self.name} Desktop chats." if self.available
                           else f"{self.name} Desktop chat status could not be confirmed."),
                "last_seen": self.observed_at if self.available else None}


class ClaudeDesktopChatSource(DesktopChatSource):
    provider = "claude"
    name = "Claude"
    open_label = "Open Claude chat"


class ChatGPTDesktopChatSource(DesktopChatSource):
    provider = "chatgpt"
    name = "ChatGPT"
    open_label = "Open ChatGPT"
    allow_window_ids = True
    def read_snapshot(self, now):
        observed, fingerprint, chats = super().read_snapshot(now)
        if not self.allow_window_ids:
            chats = {ident: value for ident, value in chats.items() if DESKTOP_CHAT_ID.fullmatch(ident)}
        return observed, fingerprint, chats

    def health(self):
        health = super().health()
        if self.available and not (self.cooking or self.idle):
            health.update(waiting_for_chat=True, detail="No regular ChatGPT chat was recognized in the latest check.")
        return health


def parent_stdin_ended():
    """The native-only lifetime pipe carries no data; bytes or EOF mean stop."""
    try:
        readable, _, _ = select.select([0], [], [], 0)
        if not readable:
            return False
        os.read(0, 1)
        return True
    except (OSError, ValueError):
        return True


def watch_native_parent():
    try:
        # Use the raw descriptor. A daemon blocked on buffered sys.stdin can
        # hold its lock during interpreter finalization.
        os.read(0, 1)
    except OSError:
        pass
    os.kill(os.getpid(), signal.SIGTERM)


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".local")
    parser.add_argument("--codex-root", type=Path)
    parser.add_argument("--claude-root", type=Path)
    parser.add_argument("--claude-desktop-root", type=Path, help="Explicit authorized Claude Desktop support folder.")
    parser.add_argument("--claude-chat-status-file", type=Path, help="Private status file for an opted-in direct native launch.")
    parser.add_argument("--chatgpt-chat-status-file", type=Path, help="Private status file for opted-in ChatGPT chat monitoring.")
    parser.add_argument("--chatgpt-chats", action="store_true", help="Monitor exact ChatGPT conversation IDs using the existing sign-in.")
    parser.add_argument("--claude-chats", action="store_true", help="Monitor Claude web and desktop conversations using the existing sign-in.")
    parser.add_argument("--sandbox", action="store_true", help="Require sandbox source grants and disable unsupported account helpers.")
    parser.add_argument("--store-profile", action="store_true", help="Use the sandboxed Mac App Store presentation.")
    parser.add_argument("--sample-mode", action="store_true", help="Show isolated built-in samples without accessing providers.")
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--instance-id", default=None, help="Native launch identity used to verify the owned collector.")
    parser.add_argument("--access-token-file", type=Path, help="Private native display access file; required for packaged launches.")
    parser.add_argument("--parent-lifetime-stdin", action="store_true", help="Stop when the native owner's private stdin lifetime pipe closes.")
    usage = parser.add_mutually_exclusive_group()
    usage.add_argument("--usage", dest="usage_enabled", action="store_true", help="Explicitly enable direct provider account-usage reads.")
    usage.add_argument("--no-usage", dest="usage_enabled", action="store_false", help="Keep provider account-usage reads disabled.")
    projects = parser.add_mutually_exclusive_group()
    projects.add_argument("--projects", dest="projects_enabled", action="store_true", help="Explicitly enable direct Anthropic project reads.")
    projects.add_argument("--no-projects", dest="projects_enabled", action="store_false", help="Keep authenticated Claude project reads disabled.")
    parser.set_defaults(usage_enabled=False, projects_enabled=False)
    return parser, parser.parse_args(argv)


def main():
    parser, args = parse_arguments()
    args.store_profile = args.store_profile or os.environ.get("AGENT_BOARD_STORE_PROFILE") == "1"
    args.sandbox = args.sandbox or args.store_profile or os.environ.get("AGENT_BOARD_SANDBOX") == "1"
    if (args.claude_chat_status_file is not None or args.chatgpt_chat_status_file is not None or args.chatgpt_chats or args.claude_chats) and (
            args.sandbox or args.store_profile or args.sample_mode
            or not args.parent_lifetime_stdin or not args.instance_id or args.access_token_file is None
            or any(root is not None for root in (args.codex_root, args.claude_root, args.claude_desktop_root))):
        parser.exit(2, "Desktop chat status requires a direct native launch.\n")
    if args.chatgpt_chats and args.chatgpt_chat_status_file is not None:
        parser.exit(2, "Choose one ChatGPT conversation source.\n")
    if args.claude_chats and args.claude_chat_status_file is not None:
        parser.exit(2, "Choose one Claude conversation source.\n")
    if args.sample_mode and (not args.sandbox or not args.store_profile or args.usage_enabled or args.projects_enabled
                             or getattr(sys, "frozen", False) and os.environ.get("AGENT_BOARD_STORE_PROFILE") != "1"):
        parser.exit(2, "Sample mode requires the Store sandbox with account helpers disabled.\n")
    os.umask(0o077)
    # Start fixed, local-only diagnostics before bookmark validation or the
    # first protected read can wait for a macOS permission decision.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOG.info("startup phase=prepare")
    try:
        access_token = read_access_token(args.access_token_file) if args.access_token_file else None
    except (OSError, ValueError, UnicodeError):
        LOG.info("startup phase=access_file_failed")
        parser.exit(1, "The private display access file could not be read.\n")
    manifest_observation = {}
    source_grants = None
    if args.sandbox:
        # Do not accept a flag as authorization. Resolve the owner's private
        # transferable bookmarks in this helper before opening any provider file.
        from sandbox_sources import activate_source_bookmarks
        LOG.info("startup phase=verify_source_grants")
        try:
            if not access_token or not args.parent_lifetime_stdin:
                raise ValueError
            roots = {"codex": args.codex_root, "claude": args.claude_root,
                     "claude_desktop": args.claude_desktop_root}
            if any(root is None or not root.is_absolute() for root in roots.values()):
                raise ValueError
            source_grants = activate_source_bookmarks(os.environ.get("AGENT_BOARD_SOURCE_BOOKMARKS"),
                                                     expected_roots=roots, support=args.data_dir.parent, observation=manifest_observation,
                                                     require_unselected=args.sample_mode)
        except (OSError, ValueError, TypeError):
            LOG.info("startup phase=source_grants_failed")
            parser.exit(1, "The sandbox source grants could not be verified. Reopen Setup and Permissions.\n")
        LOG.info("startup phase=source_grants_verified")
    monitor = None
    server = None
    workers = []
    stopping = threading.Event()
    def stop_server(signum, frame):
        if stopping.is_set():
            return
        stopping.set()
        if monitor is not None:
            monitor.stop.set()
        stop_all_processes()
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    launch_files = NativeLaunchFiles(args.data_dir, args.instance_id, args.access_token_file, access_token,
                                    os.environ.get("AGENT_BOARD_SOURCE_BOOKMARKS") if args.sandbox else None,
                                    parent_lifetime=args.parent_lifetime_stdin,
                                    manifest_fingerprint=manifest_observation.get("fingerprint"))
    try:
        if args.parent_lifetime_stdin:
            # Detect an already-dead owner before constructing source readers.
            # The finally block still clears this validated launch's secrets.
            if parent_stdin_ended():
                return
            threading.Thread(target=watch_native_parent, daemon=True, name="native-parent-lifetime").start()
        if stopping.is_set():
            return
        try:
            claude_chat_source = (ClaudeDesktopChatSource(launch_files, args.claude_chat_status_file, args.instance_id)
                                  if args.claude_chat_status_file is not None else None)
            chatgpt_chat_source = (ChatGPTDesktopChatSource(launch_files, args.chatgpt_chat_status_file, args.instance_id)
                                   if args.chatgpt_chat_status_file is not None else None)
            if chatgpt_chat_source is not None:
                chatgpt_chat_source.allow_window_ids = False
            if args.chatgpt_chats:
                validate_native_chat_launch(launch_files, args.instance_id)
                chatgpt_chat_source = ChatGPTConversationSource(ChatGPTSource(), ChatGPTStatusReader(Path.home() / ".codex"),
                    state_path=args.data_dir / "chatgpt-chat-baseline.json")
            if args.claude_chats:
                validate_native_chat_launch(launch_files, args.instance_id)
                claude_chat_source = ClaudeChatConversationSource(ClaudeChatAccountReader(),
                    state_path=args.data_dir / "claude-chat-baseline.json")
        except ValueError:
            parser.exit(2, "Desktop chat status requires this launch's private file.\n")
        LOG.info("startup phase=construct_readers")
        monitor = Monitor(args.data_dir, args.codex_root, args.claude_root, max(0.5, args.interval),
                          args.usage_enabled, args.projects_enabled, args.claude_desktop_root, args.sandbox,
                          source_grants=source_grants, store_profile=args.store_profile, sample_mode=args.sample_mode,
                          claude_chat_source=claude_chat_source, chatgpt_chat_source=chatgpt_chat_source)
        if stopping.is_set():
            return
        LOG.info("startup phase=initial_scan")
        monitor.scan()
        LOG.info("startup phase=initial_scan_complete")
        if stopping.is_set():
            return
        try:
            LOG.info("startup phase=bind_listener")
            server = BoardHTTPServer(("127.0.0.1", args.port), Handler)
        except OSError as exc:
            LOG.info("startup phase=listener_failed")
            parser.exit(1, f"Could not start Agent Board on port {args.port}: {exc}\n")
        server.monitor = monitor
        server.access_token = access_token
        server.instance_id = args.instance_id
        for name, target in (("status-collector", monitor.run), ("usage-collector", monitor.run_usage),
                             ("claude-project-collector", monitor.run_projects)):
            if stopping.is_set():
                return
            worker = threading.Thread(target=target, daemon=True, name=name)
            worker.start()
            workers.append(worker)
        if stopping.is_set():
            return
        LOG.info("startup phase=ready")
        LOG.info("Agent Board is listening at http://127.0.0.1:%s", server.server_port)
        server.serve_forever(poll_interval=0.5)
    finally:
        try:
            if monitor is not None:
                monitor.stop.set()
                for source in monitor.desktop_chat_sources.values():
                    close = getattr(source, "close", None)
                    if close is not None:
                        close()
            stop_all_processes()
            if server is not None:
                server.server_close()
            for worker in workers:
                worker.join(timeout=5)
        finally:
            launch_files.cleanup()


if __name__ == "__main__":
    main()
