#!/usr/bin/env python3
"""Local-only status collector and display feed. Requires Python 3.9+, no packages."""

import argparse
import copy
import hashlib
import json
import logging
import os
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

from board_store import BoardStore, Conflict
from claude_source import ClaudeSource
from codex_source import CodexSource
from chat_links import chat_url, open_command, UnsupportedChatLink

if os.environ.get("AGENT_BOARD_SANDBOX") == "1":
    # The signed sandbox entry sets this before importing the server. Store
    # packages omit these legacy account/executable readers entirely.
    UsageSource = ClaudeProjectSource = None

    def stop_all_processes():
        pass
else:
    from claude_project_source import ClaudeProjectSource
    from usage_source import UsageSource
    from process_reader import stop_all_processes

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("agent-board")


class _UnselectedSource:
    def __init__(self, provider):
        self.provider = provider


class Monitor:
    def __init__(self, data_dir, codex_root=None, claude_root=None, interval=2, usage_enabled=False, projects_enabled=False,
                 claude_desktop_root=None, sandbox=False, source_grants=None, store_profile=False, sample_mode=False):
        if store_profile and not sandbox:
            raise ValueError("The Store profile requires sandbox source access.")
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
                           "error": "Sample values, not your account usage.", "windows": windows}
                          for provider, name, windows in (
                              ("claude", "Claude", [{"id": "five_hour", "remaining_percent": 82, "reset_at": sample_now + 3600},
                                                    {"id": "weekly", "remaining_percent": 91, "reset_at": sample_now + 172800}]),
                              ("codex", "Codex", [{"id": "weekly", "remaining_percent": 64, "reset_at": sample_now + 259200}]))]

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

    def scan(self):
        with self.lock:
            if self.sample_mode:
                self.last_scan = time.time()
                self.health = [{"id": "samples", "name": "Sample data", "available": True, "sample": True,
                                "detail": "Sample data only. Real provider folders and accounts are not accessed.",
                                "last_seen": self.last_scan}]
                return
            health = []
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
                        source.tracked_ids = {ident for ident in self.store.session_ids("claude") if not ident.startswith("project:")}
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
                    LOG.warning("%s source scan failed; retrying without recording source contents.", provider)
                    self.store.mark_unavailable(provider, "The source could not be read. Its last working state is unconfirmed.", source_kind="local" if provider == "claude" else None)
                    health.append({"id": provider, "name": "Codex" if provider == "codex" else "Claude Code", "available": False,
                                   "detail": "Local session data could not be read. Retrying automatically.", "last_seen": self.last_scan})
            self.health = health
            self.last_scan = now
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
            self.scan()
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
                LOG.warning("Usage refresh failed; retaining explicitly stale values until retry.")
                with self.usage_lock:
                    for item in self.usage:
                        item.update(available=False, loading=False,
                                    stale=bool(item.get("updated_at")), error="Usage could not be refreshed.")
            self.stop.wait(60)

    def scan_projects(self):
        if self.project_source is None:
            return
        # Cloud account reads never hold the lock used by local status and UI
        # actions. The result is applied atomically after its bounded request.
        with self.lock:
            self.project_source.tracked_ids = {ident for ident in self.store.session_ids("claude") if ident.startswith("project:")}
        now = time.time()
        try:
            observations, health = self.project_source.scan(now, self.store.activated_at)
        except Exception:
            observations = []
            health = {"id": "claude_projects", "name": "Claude projects", "available": False,
                      "detail": "Claude project status could not be read. Retrying automatically.", "last_seen": None}
        with self.lock:
            self.store.apply(observations, now)
            present = {row["provider"] + ":" + row["id"] for row in observations if self.store.normalize(row, now) is not None}
            # A partial response can still confirm its successful rows. Missing
            # working rows become uncertain without changing completed replies.
            self.store.mark_unavailable("claude", "The project's working state could not be confirmed in the latest check.",
                                        present_keys=present, source_kind="project")
            self.project_health = health

    def run_projects(self):
        if self.project_source is None:
            return
        while not self.stop.is_set():
            self.scan_projects()
            self.stop.wait(15)

    def snapshot(self):
        with self.lock:
            snapshot = self.store.snapshot()
            sources = copy.deepcopy(self.health + ([self.project_health] if self.project_health else []))
            source_health = {source.get("id"): source for source in sources}
            for group in ("sessions", "history", "background"):
                for item in snapshot[group]:
                    if self.sample_mode:
                        item.update({"sample": True, "can_open": False, "source_unavailable": False,
                                     "source_unavailable_detail": "", "notification_eligible": False})
                        continue
                    source_id = item["provider"]
                    if source_id == "claude" and (item.get("source_kind") == "project"
                                                  or item["id"].startswith("project:")):
                        source_id = "claude_projects"
                    health = source_health.get(source_id)
                    unavailable = health is None or health.get("available") is not True
                    item["source_unavailable"] = unavailable
                    detail = health.get("detail") if health else None
                    item["source_unavailable_detail"] = (
                        " ".join(detail.split())[:240] if isinstance(detail, str) and detail.strip()
                        else "The source is unavailable. This is its last saved state."
                    ) if unavailable else ""
            snapshot.update({"sources": sources, "now": time.time(), "last_scan": self.last_scan,
                             "csrf_token": self.token, "poll_interval": self.interval, "sample_mode": self.sample_mode})
            with self.usage_lock:
                snapshot["usage"] = copy.deepcopy(self.usage)
            return snapshot


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
            return self.respond(200, {"ok": True, "app": "agent-board", "instance_id": getattr(self.server, "instance_id", None), "last_scan": self.monitor.last_scan})
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
    if args.sample_mode and (not args.sandbox or not args.store_profile or args.usage_enabled or args.projects_enabled
                             or getattr(sys, "frozen", False) and os.environ.get("AGENT_BOARD_STORE_PROFILE") != "1"):
        parser.exit(2, "Sample mode requires the Store sandbox with account helpers disabled.\n")
    os.umask(0o077)
    try:
        access_token = read_access_token(args.access_token_file) if args.access_token_file else None
    except (OSError, ValueError, UnicodeError):
        parser.exit(1, "The private display access file could not be read.\n")
    manifest_observation = {}
    source_grants = None
    if args.sandbox:
        # Do not accept a flag as authorization. Resolve the owner's private
        # transferable bookmarks in this helper before opening any provider file.
        from sandbox_sources import activate_source_bookmarks
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
            parser.exit(1, "The sandbox source grants could not be verified. Reopen Setup and Permissions.\n")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
        monitor = Monitor(args.data_dir, args.codex_root, args.claude_root, max(0.5, args.interval),
                          args.usage_enabled, args.projects_enabled, args.claude_desktop_root, args.sandbox,
                          source_grants=source_grants, store_profile=args.store_profile, sample_mode=args.sample_mode)
        if stopping.is_set():
            return
        monitor.scan()
        if stopping.is_set():
            return
        try:
            server = BoardHTTPServer(("127.0.0.1", args.port), Handler)
        except OSError as exc:
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
        LOG.info("Agent Board is listening at http://127.0.0.1:%s", server.server_port)
        server.serve_forever(poll_interval=0.5)
    finally:
        try:
            if monitor is not None:
                monitor.stop.set()
            stop_all_processes()
            if server is not None:
                server.server_close()
            for worker in workers:
                worker.join(timeout=5)
        finally:
            launch_files.cleanup()


if __name__ == "__main__":
    main()
