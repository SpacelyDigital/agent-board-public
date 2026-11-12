"""Exercise local quota records through real readers, board restart and HTTP."""

import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from server import BoardHTTPServer, Handler, Monitor


class LocalQuotaWorkflowTests(unittest.TestCase):
    def test_verified_local_quotas_survive_restart_and_stop_at_revocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex, claude, desktop = [root / name for name in ("codex", "claude", "desktop")]
            for directory in (codex, claude, desktop):
                directory.mkdir()
            desktop = desktop / "claude-code-sessions"
            desktop.mkdir()
            grants = dict.fromkeys(("codex", "claude", "claude_desktop"), True)

            def monitor(selected=grants):
                return Monitor(root / "board", codex, claude, claude_desktop_root=desktop,
                               sandbox=True, source_grants=selected, store_profile=False)

            with patch("server.UsageSource", side_effect=AssertionError("Account reader constructed")), \
                    patch("server.ClaudeProjectSource", side_effect=AssertionError("Account reader constructed")):
                board = monitor()
                at = time.time()
                rollout = codex / "sessions/2026/11/04/rollout-local-quota-fixture.jsonl"
                rollout.parent.mkdir(parents=True)
                records = [
                    {"type": "session_meta", "payload": {"id": "local-quota-fixture", "source": "vscode"}},
                    {"timestamp": at, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "fixture-turn"}},
                    {"timestamp": at, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                        "secondary": {"used_percent": 27, "window_minutes": 10080, "resets_at": int(at) + 86400},
                        "credits": {"balance": "240.5", "has_credits": True, "unlimited": False}}}},
                    {"timestamp": at + 0.01, "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "fixture-turn"}},
                ]
                rollout.write_text("".join(json.dumps(record) + "\n" for record in records))
                database = codex / "state_5.sqlite"
                with sqlite3.connect(database) as connection:
                    connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, updated_at INTEGER, title TEXT, cwd TEXT, archived INTEGER)")
                    connection.execute("INSERT INTO threads VALUES (?,?,?,?,?,?)",
                                       ("local-quota-fixture", str(rollout), int(at), "Local quota fixture", str(root), 0))
                quota_dir = claude / "agent-board/usage"
                quota_dir.mkdir(parents=True)
                payload = json.dumps({"provider": "claude", "observed_at_ms": int(at * 1000), "windows": [
                    {"id": "five_hour", "used_basis_points": 1800, "reset_at": int(at) - 1},
                    {"id": "weekly", "used_basis_points": 910, "reset_at": int(at) + 6 * 86400}]}, separators=(",", ":"))
                slot = quota_dir / "quota-v1-a.json"
                complete = json.dumps({"version": 1, "payload": payload,
                                       "sha256": hashlib.sha256(payload.encode()).hexdigest()}).encode()
                slot.write_bytes(complete)
                source_before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (rollout, database, slot)}

                board.scan()
                with BoardHTTPServer(("127.0.0.1", 0), Handler) as server:
                    server.monitor, server.access_token = board, "f" * 64
                    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
                    thread.start()
                    try:
                        def fetch():
                            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                            try:
                                connection.request("GET", "/api/state", headers={"X-Agent-Board-Access": server.access_token})
                                response = connection.getresponse()
                                self.assertEqual(response.status, 200)
                                return json.loads(response.read())
                            finally:
                                connection.close()

                        def assert_readings():
                            state = fetch()
                            usage = {entry["id"]: entry for entry in state["usage"]}
                            self.assertEqual(usage["codex"]["windows"][0]["remaining_percent"], 73)
                            self.assertEqual(usage["codex"]["credits"]["amount"], 240.5)
                            self.assertFalse(usage["codex"]["credits"]["stale"])
                            self.assertEqual([item["id"] for item in usage["claude"]["windows"]], ["weekly"])
                            self.assertEqual(usage["claude"]["windows"][0]["remaining_percent"], 90.9)
                            self.assertNotIn("credits", usage["claude"])
                            self.assertFalse(usage["claude"]["stale"])
                            self.assertEqual(state["counts"]["ready"], 1)
                            return usage

                        initial = assert_readings()
                        board = monitor()
                        board.scan()
                        server.monitor = board
                        self.assertEqual(assert_readings()["codex"]["updated_at"], initial["codex"]["updated_at"])
                        self.assertEqual(source_before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_before})
                        slot.write_bytes(complete[:len(complete) // 2])
                        board.scan()
                        claude_usage = next(entry for entry in fetch()["usage"] if entry["id"] == "claude")
                        self.assertTrue(claude_usage["stale"])
                        self.assertEqual(claude_usage["updated_at"], initial["claude"]["updated_at"])
                        revoked = monitor(dict.fromkeys(grants, False))
                        with patch("local_quota._read_slot", side_effect=AssertionError("Revoked export was read")):
                            revoked.scan()
                        server.monitor = revoked
                        self.assertTrue(all(not entry["available"] and not entry["windows"] for entry in fetch()["usage"]))
                        self.assertTrue(all("credits" not in entry for entry in fetch()["usage"]))
                    finally:
                        server.shutdown()
                        thread.join(2)
                    self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
