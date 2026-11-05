"""Real loopback HTTP attacks against disposable stores, with no provider reads."""

from concurrent.futures import ThreadPoolExecutor
import errno
import http.client
import json
import os
from pathlib import Path
import random
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from server import BoardHTTPServer, Handler, Monitor, read_access_token


class LocalHTTPStartupTests(unittest.TestCase):
    def test_authenticated_loopback_startup_never_uses_reverse_dns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monitor = Monitor(root / "data", codex_root=root / "codex", claude_root=root / "claude",
                              usage_enabled=False, projects_enabled=False)
            with patch("socket.getfqdn", side_effect=AssertionError("Startup must not resolve a hostname")), \
                    patch("socket.gethostbyaddr", side_effect=AssertionError("Startup must not reverse-resolve an address")):
                with BoardHTTPServer(("127.0.0.1", 0), Handler) as server:
                    server.monitor = monitor
                    server.access_token = "startup-fixture-secret"
                    server.instance_id = "startup-without-dns"
                    self.assertEqual(server.server_name, "localhost")
                    self.assertEqual(server.server_address, ("127.0.0.1", server.server_port))
                    self.assertGreater(server.server_port, 0)
                    thread = threading.Thread(target=server.serve_forever,
                                              kwargs={"poll_interval": 0.01}, daemon=True)
                    thread.start()
                    try:
                        for authenticated, expected in ((False, 403), (True, 200)):
                            with self.subTest(authenticated=authenticated):
                                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                                try:
                                    headers = {"X-Agent-Board-Access": server.access_token} if authenticated else {}
                                    connection.request("GET", "/api/health", headers=headers)
                                    response = connection.getresponse()
                                    payload = json.loads(response.read())
                                    self.assertEqual(response.status, expected)
                                    if authenticated:
                                        self.assertEqual(payload["app"], "agent-board")
                                        self.assertEqual(payload["instance_id"], server.instance_id)
                                        self.assertTrue(payload["ok"])
                                finally:
                                    connection.close()
                    finally:
                        server.shutdown()
                        thread.join(2)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(server.socket.fileno(), -1)

    def test_occupied_loopback_port_closes_failed_server_socket(self):
        created = []
        socket_factory = socket.socket

        def tracked_socket(*args, **kwargs):
            result = socket_factory(*args, **kwargs)
            created.append(result)
            return result

        with socket_factory(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            address = listener.getsockname()
            with patch("socket.socket", side_effect=tracked_socket), \
                    patch("socket.getfqdn", side_effect=AssertionError("Bind must not resolve a hostname")), \
                    patch("socket.gethostbyaddr", side_effect=AssertionError("Bind must not reverse-resolve an address")):
                with self.assertRaises(OSError) as failure:
                    BoardHTTPServer(address, Handler)
            self.assertEqual(failure.exception.errno, errno.EADDRINUSE)
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0].fileno(), -1)
            self.assertNotEqual(listener.fileno(), -1)
            self.assertEqual(listener.getsockname(), address)


class LocalHTTPSecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.monitor = Monitor(root / "data", codex_root=root / "codex", claude_root=root / "claude",
                               usage_enabled=False, projects_enabled=False)
        self.monitor.sources = []
        self.server = BoardHTTPServer(("127.0.0.1", 0), Handler)
        self.server.monitor = self.monitor
        self.server.access_token = "a" * 64
        self.server.instance_id = "disposable-security-fixture"
        self.server.request_timeout = 0.3
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.seed()

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)

    def seed(self, event="reply-1", status="ready", stamp=None):
        self.monitor.store.apply([dict(provider="codex", id="thread-1", title="Disposable chat",
            cwd="/tmp/fake-project", status=status, event_id=event,
            updated_at=stamp or time.time() + 1, can_open=False)])

    def request(self, method="GET", path="/api/state", body=None, headers=None, access=True, csrf=True, server=None):
        server = server or self.server
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.putrequest(method, path, skip_host=True)
            supplied = [("Host", f"127.0.0.1:{server.server_port}")]
            if access:
                supplied.append(("X-Agent-Board-Access", server.access_token))
            if method == "POST" and csrf:
                supplied.append(("X-Agent-Board-Token", server.monitor.token))
            supplied.extend(headers or [])
            if isinstance(body, dict):
                body = json.dumps(body).encode()
            if body is not None and not any(name.casefold() == "content-length" for name, value in supplied):
                supplied.append(("Content-Length", str(len(body))))
            for name, value in supplied:
                connection.putheader(name, value)
            connection.endheaders(body)
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def test_private_reads_and_actions_need_exact_single_secret(self):
        for path in ("/api/state", "/api/health", "/api/missing"):
            self.assertEqual(self.request(path=path, access=False)[0], 403)
            self.assertEqual(self.request(path=path, access=False,
                headers=[("X-Agent-Board-Access", "wrong")])[0], 403)
            self.assertEqual(self.request(path=path,
                headers=[("X-Agent-Board-Access", self.server.access_token)])[0], 403)
        self.assertEqual(self.request(method="POST", path="/api/sessions/codex:thread-1/restore",
            body={}, access=False)[0], 403)
        status, headers, data = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["sessions"][0]["title"], "Disposable chat")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn("Set-Cookie", headers)

    def test_duplicate_hosts_origins_and_csrf_are_rejected(self):
        action = "/api/sessions/codex:thread-1/rename"
        self.assertEqual(self.request(headers=[("Host", f"127.0.0.1:{self.server.server_port}")])[0], 403)
        for origin in ("null", "https://evil.example", "http://127.0.0.1:1", "http://localhost.evil.example"):
            self.assertEqual(self.request("POST", action, {"title": "Injected"}, [("Origin", origin)])[0], 403)
        allowed = f"http://127.0.0.1:{self.server.server_port}"
        self.assertEqual(self.request("POST", action, {"title": "Injected"}, [("Origin", allowed), ("Origin", allowed)])[0], 403)
        self.assertEqual(self.request("POST", action, {"title": "Injected"}, csrf=False)[0], 403)
        self.assertEqual(self.request("POST", action, {"title": "Injected"},
            [("X-Agent-Board-Token", self.monitor.token)])[0], 403)
        self.assertEqual(self.monitor.store.get("codex:thread-1")["title"], "Disposable chat")

    def test_invalid_and_ambiguous_bodies_fail_without_mutation(self):
        action = "/api/sessions/codex:thread-1/rename"
        cases = [(b"[]", [], 400), (b"null", [], 400), (b"{", [], 400),
                 (b"\xff", [], 400), (b"{}", [("Content-Length", "NaN")], 400),
                 (b"", [("Content-Length", "-1")], 413),
                 (b"", [("Content-Length", "16385")], 413),
                 (b"{}", [("Content-Length", "2"), ("Content-Length", "2")], 400),
                 (b"{}", [("Transfer-Encoding", "chunked")], 400),
                 (b"[" * 1100 + b"]" * 1100, [], 400)]
        for body, headers, expected in cases:
            with self.subTest(body=body[:30], headers=headers):
                self.assertEqual(self.request("POST", action, body, headers)[0], expected)
        for title in ([], {}, True, 7, "", "x" * 161):
            self.assertEqual(self.request("POST", action, {"title": title})[0], 400)
        self.assertEqual(self.monitor.store.get("codex:thread-1")["title"], "Disposable chat")

    def test_paths_never_escape_the_static_allowlist(self):
        for path in ("/../server.py", "/%2e%2e/server.py", "/static/../../board.sqlite3",
                     "/about/../../../.local/board.sqlite3", "/api%2fstate", "/server.py",
                     "/about/%2e%2e/%2e%2e/server.py"):
            self.assertEqual(self.request(path=path)[0], 404)
        self.assertEqual(self.request(path="http://evil.example/api/state")[0], 400)
        self.assertEqual(self.request(path="/about/")[0], 200)

    def test_stale_acknowledgement_and_snooze_cannot_hide_a_new_turn(self):
        old = self.monitor.store.get("codex:thread-1")
        self.seed("new-turn", "cooking", old["updated_at"] + 1)
        for action in ("review", "snooze"):
            status, _, _ = self.request("POST", f"/api/sessions/codex:thread-1/{action}", {"event_id": old["event_id"]})
            self.assertEqual(status, 409)
        self.assertEqual(self.monitor.store.get(old["key"])["status"], "cooking")
        self.assertEqual(self.monitor.store.snapshot()["counts"]["cooking"], 1)

    def test_invalid_source_numbers_return_valid_recoverable_json(self):
        self.monitor.health = [{"id": "fixture", "last_seen": float("inf")}]
        status, _, data = self.request()
        self.assertEqual(status, 503)
        self.assertIn("local board state", json.loads(data)["error"])
        self.monitor.health = []
        self.assertEqual(self.request()[0], 200)

    def test_incomplete_and_trickling_clients_release_their_slots(self):
        clients = []
        try:
            for index in range(self.server.max_connections + 5):
                client = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=1)
                client.sendall(b"GET /api/state HTTP/1.1\r\nX-Slow: ")
                clients.append(client)
            time.sleep(self.server.request_timeout + 0.15)
            self.assertEqual(self.request()[0], 200)
            # A declared body that never arrives is also bounded.
            self.assertEqual(self.request("POST", "/api/sessions/codex:thread-1/rename", b"",
                [("Content-Length", "50")])[0], 408)
            self.assertEqual(self.request()[0], 200)
        finally:
            for client in clients:
                client.close()

    def test_two_hundred_fixture_users_make_one_thousand_mixed_actions(self):
        # Each user has a deterministic independent seed and uses the real HTTP
        # parser, authentication, monitor, SQLite store and lifecycle actions.
        def user(index):
            rng = random.Random(20261030 + index)
            statuses = []
            for number in range(5):
                action = rng.choice(("rename", "restore", "move", "review", "snooze", "read"))
                current = self.monitor.store.get("codex:thread-1")
                body = {"rename": {"title": f"User {index} edit {number}"},
                        "restore": {}, "move": {"placement": rng.choice(("board", "background", "auto"))},
                        "review": {"event_id": current["event_id"]},
                        "snooze": {"event_id": current["event_id"], "minutes": 10}}.get(action)
                status = self.request() [0] if action == "read" else self.request("POST", f"/api/sessions/codex:thread-1/{action}", body)[0]
                statuses.append(status)
            return statuses
        self.server.request_timeout = 5
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = [status for group in pool.map(user, range(200)) for status in group]
        self.assertEqual(len(results), 1000)
        self.assertEqual(set(results), {200})
        self.assertEqual(self.request()[0], 200)

    def test_two_separate_installations_never_share_tokens_canaries_or_persistence(self):
        root = Path(self.directory.name)
        other_monitor = Monitor(root / "other-installation", codex_root=root / "other-codex",
                                claude_root=root / "other-claude", usage_enabled=False, projects_enabled=False)
        other_monitor.sources = []
        other_server = BoardHTTPServer(("127.0.0.1", 0), Handler)
        other_server.monitor = other_monitor
        other_server.access_token = "b" * 64
        other_server.instance_id = "other-disposable-installation"
        other_thread = threading.Thread(target=other_server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        other_thread.start()
        try:
            self.monitor.store.rename("codex:thread-1", "INSTALLATION_A_PRIVATE_CANARY")
            other_monitor.store.apply([dict(provider="codex", id="thread-1", title="INSTALLATION_B_PRIVATE_CANARY",
                cwd="/tmp/other-fixture", status="ready", event_id="other-reply", updated_at=time.time() + 1)])
            old_access = self.server.access_token
            status, _, data = self.request(server=other_server, access=False,
                headers=[("X-Agent-Board-Access", old_access)])
            self.assertEqual(status, 403)
            self.assertNotIn(b"INSTALLATION_", data)
            status, _, data = self.request(server=other_server)
            self.assertEqual(status, 200)
            self.assertIn(b"INSTALLATION_B_PRIVATE_CANARY", data)
            self.assertNotIn(b"INSTALLATION_A_PRIVATE_CANARY", data)
            restarted = Monitor(root / "data", codex_root=root / "codex", claude_root=root / "claude",
                                usage_enabled=False, projects_enabled=False)
            restarted.sources = []
            self.monitor = self.server.monitor = restarted
            self.server.access_token = "c" * 64
            self.assertEqual(self.request(access=False, headers=[("X-Agent-Board-Access", old_access)])[0], 403)
            status, _, data = self.request()
            self.assertEqual(status, 200)
            self.assertIn(b"INSTALLATION_A_PRIVATE_CANARY", data)
            self.assertNotIn(b"INSTALLATION_B_PRIVATE_CANARY", data)
            self.assertEqual(other_monitor.store.get("codex:thread-1")["title"], "INSTALLATION_B_PRIVATE_CANARY")
        finally:
            other_server.shutdown()
            other_server.server_close()
            other_thread.join(1)


class SecretSpecialFileTests(unittest.TestCase):
    def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "display-access"
            os.mkfifo(path, 0o600)
            started = time.monotonic()
            with self.assertRaises(ValueError):
                read_access_token(path)
            self.assertLess(time.monotonic() - started, 0.1)


if __name__ == "__main__":
    unittest.main()
