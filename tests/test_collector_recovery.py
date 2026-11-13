"""Storage outages must not kill collectors or leave stale connected labels."""
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_source import CodexSource
from server import BoardHTTPServer, Handler, Monitor


class FixtureCodex(CodexSource):
    def __init__(self, row):
        self.row = row

    def scan(self, now, cutoff):
        return [dict(self.row)], {"id": "codex", "name": "Codex", "available": True, "last_seen": now}


class FixtureProjects:
    def __init__(self, row):
        self.row = row

    def scan(self, now, cutoff):
        return [dict(self.row)], {"id": "claude_projects", "name": "Claude projects", "available": True,
                                  "last_seen": now - 86400 * 14}


class FastStop(threading.Event):
    """Shorten only fixture waits while retaining the requested retry cadence."""
    def __init__(self):
        super().__init__()
        self.delays = []

    def wait(self, timeout=None):
        self.delays.append(timeout)
        return super().wait(min(timeout, .01) if timeout is not None else None)


class CollectorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.monitor = Monitor(root / "data", codex_root=root / "codex", claude_root=root / "claude", interval=.01)
        self.at = self.monitor.store.activated_at + .01
        self.local = dict(provider="codex", id="local-fixture", title="Local fixture", status="ready",
                          event_id="complete", updated_at=self.at)
        self.project = dict(provider="claude", id="project:fixture", title="Project fixture", status="ready",
                            event_id="project-complete", updated_at=self.at, source_kind="project")
        self.monitor.sources = [FixtureCodex(self.local)]
        self.monitor.project_source = FixtureProjects(self.project)
        self.monitor.project_health = {"id": "claude_projects", "name": "Claude projects", "available": False}
        self.monitor.scan()
        self.monitor.scan_projects()
        self.saved = self.monitor.store.get("codex:local-fixture")
        self.monitor.store.review(self.saved["key"], self.saved["event_id"])
        self.addCleanup(self.monitor.stop.set)

    def health(self):
        return {row["id"]: row["available"] for row in self.monitor.snapshot()["sources"]}

    def test_limited_history_health_does_not_disconnect_verified_sibling_or_realert_review(self):
        checking = dict(self.local, history_checking=True, history_verified=False,
                        status="disconnected", detail="This history keeps changing before verification finishes.")
        sibling = dict(self.local, id="verified-sibling", event_id="sibling-complete")
        health = {"id": "codex", "name": "Codex", "available": True,
                  "coverage_limited": True, "history_checking": 1, "history_changing": 1,
                  "history_limit_reached": False, "history_oldest_check_seconds": 12,
                  "detail": "A local history remains unconfirmed."}
        source = self.monitor.sources[0]
        with patch.object(source, "scan", return_value=([checking, sibling], health)):
            self.monitor.scan()
        snapshot = self.monitor.snapshot()
        projected = next(item for item in snapshot["sources"] if item["id"] == "codex")
        self.assertTrue(projected["available"])
        self.assertTrue(projected["coverage_limited"])
        items = {item["id"]: item for group in ("sessions", "history", "background") for item in snapshot[group]}
        self.assertTrue(items["local-fixture"]["history_checking"])
        self.assertFalse(items["local-fixture"]["notification_eligible"])
        self.assertEqual(items["local-fixture"]["reviewed_event"], self.saved["event_id"])
        self.assertEqual(items["verified-sibling"]["status"], "ready")
        self.assertFalse(items["verified-sibling"]["source_unavailable"])
        self.monitor.scan()
        recovered = self.monitor.store.get(self.saved["key"])
        self.assertFalse(recovered["history_checking"])
        self.assertEqual(recovered["reviewed_event"], self.saved["event_id"])
        self.assertFalse(recovered["notification_eligible"])

    def test_local_commit_failure_has_no_database_fallback_and_recovers_review(self):
        last_scan = self.monitor.last_scan
        with patch.object(self.monitor.store, "apply", side_effect=sqlite3.OperationalError("PRIVATE disk path")), \
                patch.object(self.monitor.store, "mark_unavailable", side_effect=sqlite3.OperationalError("PRIVATE fallback")) as fallback, \
                self.assertLogs("agent-board", level="WARNING") as logs:
            self.monitor.scan()
        fallback.assert_not_called()
        self.assertEqual(self.monitor.last_scan, last_scan)
        self.assertFalse(self.health()["codex"])
        self.assertTrue(self.health()["claude_projects"])
        self.assertNotIn("PRIVATE", " ".join(logs.output))
        self.monitor.scan()
        self.assertTrue(self.health()["codex"])
        current = self.monitor.store.get(self.saved["key"])
        self.assertEqual(current["reviewed_event"], self.saved["event_id"])
        self.assertEqual(current["event_id"], self.saved["event_id"])

    def test_unwritable_logger_cannot_break_failure_reporting_or_retry(self):
        last_scan = self.monitor.last_scan
        with patch.object(self.monitor.store, "apply", side_effect=sqlite3.OperationalError("PRIVATE")), \
                patch("server.LOG.warning", side_effect=OSError("PRIVATE disk full log")):
            self.monitor.scan()
            self.monitor.scan_projects()
        self.assertEqual(self.monitor.last_scan, last_scan)
        self.assertEqual(self.health(), {"codex": False, "claude_projects": False})
        self.monitor.scan(); self.monitor.scan_projects()
        self.assertEqual(self.health(), {"codex": True, "claude_projects": True})

    def test_failed_unavailable_update_does_not_escape_initial_scan_wrapper(self):
        with patch.object(self.monitor.store, "mark_unavailable", side_effect=sqlite3.OperationalError("PRIVATE")), \
                self.assertLogs("agent-board", level="WARNING"):
            self.monitor.scan()
        self.assertFalse(self.health()["codex"])
        self.monitor.scan()
        self.assertTrue(self.health()["codex"])

    def test_project_lookup_and_commit_failures_retry_without_fallback_writes(self):
        for method in ("session_ids", "apply"):
            with self.subTest(method=method), \
                    patch.object(self.monitor.store, method, side_effect=sqlite3.OperationalError("PRIVATE")), \
                    patch.object(self.monitor.store, "mark_unavailable") as fallback, \
                    self.assertLogs("agent-board", level="WARNING") as logs:
                self.monitor.scan_projects()
            fallback.assert_not_called()
            self.assertFalse(self.health()["claude_projects"])
            self.assertTrue(self.health()["codex"])
            self.assertNotIn("PRIVATE", " ".join(logs.output))
            self.monitor.scan_projects()
            self.assertTrue(self.health()["claude_projects"])

    def test_outer_local_and_project_workers_survive_unexpected_scan_exception(self):
        for method, runner, interval in (("scan", "run", .01), ("scan_projects", "run_projects", 15)):
            with self.subTest(method=method):
                self.monitor.stop = FastStop()
                original = getattr(self.monitor, method)
                calls = []
                def attempt():
                    calls.append(True)
                    if len(calls) == 1:
                        raise sqlite3.OperationalError("PRIVATE worker failure")
                    original()
                    self.monitor.stop.set()
                with patch.object(self.monitor, method, side_effect=attempt), \
                        self.assertLogs("agent-board", level="WARNING") as logs:
                    worker = threading.Thread(target=getattr(self.monitor, runner), daemon=True)
                    worker.start(); worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(calls), 2)
                self.assertGreater(self.monitor.stop.delays[0], max(0, interval - .5))
                self.assertNotIn("PRIVATE", " ".join(logs.output))
                self.assertTrue(self.monitor.refresh_health()["status" if method == "scan" else "projects"])

    def test_monotonic_heartbeats_keep_local_and_project_health_independent(self):
        with self.monitor.refresh_lock:
            self.monitor.refresh_state["status"].update(completed=100, failed=False)
            self.monitor.refresh_state["projects"].update(completed=149, failed=False)
        with patch("server.time.monotonic", return_value=150):
            state = self.monitor.snapshot()
        health = {row["id"]: row["available"] for row in state["sources"]}
        self.assertEqual(health, {"codex": False, "claude_projects": True})
        self.assertTrue(state["history"][0]["source_unavailable"])
        self.assertFalse(state["sessions"][0]["source_unavailable"])
        with self.monitor.refresh_lock:
            self.monitor.refresh_state["status"]["completed"] = 149
            self.monitor.refresh_state["projects"]["completed"] = 100
        with patch("server.time.monotonic", return_value=150):
            self.assertEqual(self.health(), {"codex": True, "claude_projects": False})

    def serve(self):
        server = BoardHTTPServer(("127.0.0.1", 0), Handler)
        server.monitor = self.monitor
        server.instance_id = "fixture"
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        def finish():
            server.shutdown(); server.server_close(); worker.join(2)
        self.addCleanup(finish)
        return server

    def get(self, server, route):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", route)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_http_storage_failure_is_sanitized_and_recovers_without_restart(self):
        server = self.serve()
        with patch.object(self.monitor.store, "snapshot", side_effect=sqlite3.OperationalError("PRIVATE disk path")):
            code, result = self.get(server, "/api/state")
        self.assertEqual(code, 503)
        self.assertNotIn("PRIVATE", json.dumps(result))
        code, result = self.get(server, "/api/health")
        self.assertEqual(code, 200)
        self.assertEqual(result["collectors"], {"status": False, "projects": False})
        self.monitor.scan(); self.monitor.scan_projects()
        code, result = self.get(server, "/api/state")
        self.assertEqual(code, 200)
        self.assertTrue(all(row["available"] for row in result["sources"]))
        self.assertEqual(result["history"][0]["reviewed_event"], self.saved["event_id"])

    def test_stalled_scan_cannot_hold_http_state_or_health_indefinitely(self):
        server = self.serve()
        entered, release = threading.Event(), threading.Event()
        def hold():
            with self.monitor.lock:
                entered.set(); release.wait(2)
        holder = threading.Thread(target=hold, daemon=True)
        holder.start(); self.assertTrue(entered.wait(1))
        try:
            with self.monitor.refresh_lock:
                self.monitor.refresh_state["status"]["completed"] = time.monotonic() - 60
            with patch("server.SNAPSHOT_LOCK_TIMEOUT", .02):
                started = time.monotonic()
                code, result = self.get(server, "/api/state")
                self.assertEqual(code, 503)
                self.assertLess(time.monotonic() - started, .5)
            code, result = self.get(server, "/api/health")
            self.assertEqual(code, 200)
            self.assertFalse(result["collectors"]["status"])
            self.assertTrue(result["collectors"]["projects"])
        finally:
            release.set(); holder.join(2)
        self.monitor.scan()
        self.assertEqual(self.get(server, "/api/state")[0], 200)


if __name__ == "__main__":
    unittest.main()
