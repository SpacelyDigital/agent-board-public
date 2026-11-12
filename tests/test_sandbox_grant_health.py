"""Unselected folders must never look connected or start source readers."""

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from server import Monitor


class SandboxGrantHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("codex", "claude", "desktop"):
            (self.root / name).mkdir()
        (self.root / "desktop/claude-code-sessions").mkdir()

    def monitor(self, grants):
        return Monitor(self.root / "data", self.root / "codex", self.root / "claude",
                       claude_desktop_root=self.root / "desktop/claude-code-sessions", sandbox=True, source_grants=grants)

    def test_unselected_existing_directories_do_not_construct_or_scan_readers(self):
        grants = dict.fromkeys(("codex", "claude", "claude_desktop"), False)
        with patch("server.CodexSource", side_effect=AssertionError("Unselected Codex reader constructed")) as codex, \
                patch("server.ClaudeSource", side_effect=AssertionError("Unselected Claude reader constructed")) as claude:
            monitor = self.monitor(grants)
            now = time.time() + 1
            monitor.store.apply([{"provider": provider, "id": "grant-fixture", "status": "cooking",
                                  "event_id": "fixture-cooking", "updated_at": now}
                                 for provider in ("codex", "claude")], now)
            monitor.scan()
            codex.assert_not_called()
            claude.assert_not_called()
            health = monitor.snapshot()["sources"]
            self.assertEqual({item["id"] for item in health}, {"codex", "claude"})
            self.assertTrue(all(item["available"] is False and item["last_seen"] is None for item in health))
            self.assertTrue(all("Choose" in item["detail"] for item in health))
            self.assertTrue(all(item["status"] == "disconnected" for item in monitor.snapshot()["sessions"]))

    def test_claude_requires_both_transcript_and_desktop_grants(self):
        for transcripts, desktop in ((False, False), (True, False), (False, True)):
            with self.subTest(transcripts=transcripts, desktop=desktop), \
                    patch("server.CodexSource", side_effect=AssertionError("Unselected Codex")), \
                    patch("server.ClaudeSource", side_effect=AssertionError("Partial Claude grant must not scan")) as claude:
                monitor = self.monitor({"codex": False, "claude": transcripts, "claude_desktop": desktop})
                monitor.scan()
                claude.assert_not_called()
                self.assertTrue(all(item["available"] is False for item in monitor.snapshot()["sources"]))

    def test_selected_codex_is_scanned_while_unselected_claude_stays_disconnected(self):
        monitor = self.monitor({"codex": True, "claude": True, "claude_desktop": False})
        with patch.object(monitor.sources[0], "scan", return_value=([], {"available": True})) as scan:
            monitor.scan()
        scan.assert_called_once()
        health = {item["id"]: item for item in monitor.snapshot()["sources"]}
        self.assertTrue(health["codex"]["available"])
        self.assertFalse(health["claude"]["available"])

    def test_both_claude_grants_allow_normal_scan_and_health(self):
        monitor = self.monitor({"codex": False, "claude": True, "claude_desktop": True})
        self.assertEqual(monitor.sources[1].desktop_root, self.root / "desktop/claude-code-sessions")
        self.assertEqual(monitor.sources[1].desktop_scope_root, monitor.sources[1].desktop_root)
        with patch.object(monitor.sources[1], "scan", return_value=([], {"available": True})) as scan:
            monitor.scan()
        scan.assert_called_once()
        health = {item["id"]: item for item in monitor.snapshot()["sources"]}
        self.assertFalse(health["codex"]["available"])
        self.assertTrue(health["claude"]["available"])

    def test_grant_map_rejects_missing_or_truthy_nonboolean_values(self):
        for grants in ({}, {"codex": True, "claude": True},
                       {"codex": "yes", "claude": True, "claude_desktop": True}):
            with self.subTest(grants=grants), self.assertRaises(ValueError):
                self.monitor(grants)

    def test_ready_reply_and_review_survive_source_revocation_and_regrant(self):
        grants = dict.fromkeys(("codex", "claude", "claude_desktop"), True)
        monitor = self.monitor(grants)
        now = time.time() + .01
        codex_id = "11111111-1111-4111-8111-111111111111"
        claude_id = "22222222-2222-4222-8222-222222222222"
        codex_log = self.root / "codex/sessions" / time.strftime("%Y/%m/%d", time.gmtime(now)) / "rollout-fixture.jsonl"
        claude_log = self.root / "claude/projects/-fixture" / (claude_id + ".jsonl")
        metadata = self.root / "desktop/claude-code-sessions/account/organization" / ("local_" + claude_id + ".json")
        for path in (codex_log, claude_log, metadata):
            path.parent.mkdir(parents=True, exist_ok=True)
        codex_log.write_text("".join(json.dumps(record) + "\n" for record in [
            {"type": "session_meta", "payload": {"id": codex_id, "thread_source": "user"}},
            {"type": "event_msg", "timestamp": now, "payload": {"type": "task_started", "turn_id": "codex-turn"}},
            {"type": "event_msg", "timestamp": now + .01, "payload": {
                "type": "task_complete", "turn_id": "codex-turn", "last_agent_message": "The synthetic reply is ready."}},
        ]))
        database = self.root / "codex/state_5.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,updated_at INTEGER,title TEXT)")
            connection.execute("INSERT INTO threads VALUES(?,?,?,?)", (codex_id, str(codex_log), int(now), "Codex fixture"))
        claude_log.write_text("".join(json.dumps(record) + "\n" for record in [
            {"type": "user", "sessionId": claude_id, "uuid": "claude-turn", "timestamp": now,
             "message": {"content": "Inspect the synthetic fixture"}},
            {"type": "assistant", "sessionId": claude_id, "uuid": "claude-reply", "timestamp": now + .01,
             "message": {"stop_reason": "end_turn", "content": "The synthetic reply is ready."}},
        ]))
        metadata.write_text(json.dumps({"sessionId": "local_" + claude_id, "cliSessionId": claude_id,
                                       "title": "Claude fixture", "createdAt": now * 1000, "isArchived": False}))
        fingerprint = lambda: {path: hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in (codex_log, claude_log, metadata, database)}
        source_hashes = fingerprint()
        monitor.scan()
        initial = {item["provider"]: item for item in monitor.snapshot()["sessions"]}
        self.assertEqual({provider: item["status"] for provider, item in initial.items()},
                         {"codex": "ready", "claude": "ready"})
        self.assertTrue(all(not item["source_unavailable"] and not item["source_unavailable_detail"]
                            for item in initial.values()))
        codex = initial["codex"]
        monitor.store.review(codex["key"], codex["event_id"])

        revoked = self.monitor(dict.fromkeys(grants, False))
        self.assertTrue(all(not hasattr(source, "scan") for source in revoked.sources))
        for _ in range(3):
            revoked.scan()
        saved = revoked.snapshot()
        self.assertEqual(saved["counts"]["ready"], 1)
        self.assertEqual(len(saved["sessions"]), 1)
        self.assertEqual(len(saved["history"]), 1)
        pending, reviewed = saved["sessions"][0], saved["history"][0]
        for item in (pending, reviewed):
            prior = initial[item["provider"]]
            self.assertEqual((item["status"], item["event_id"], item["preview"]),
                             ("ready", prior["event_id"], prior["preview"]))
            self.assertTrue(item["source_unavailable"])
            self.assertIn("Choose", item["source_unavailable_detail"])
        self.assertTrue(pending["notification_eligible"])
        self.assertEqual(reviewed["reviewed_event"], codex["event_id"])
        self.assertFalse(reviewed["notification_eligible"])

        regranted = self.monitor(grants)
        regranted.scan()
        restored = regranted.snapshot()
        self.assertEqual(restored["counts"]["ready"], 1)
        self.assertEqual(len(restored["history"]), 1)
        for item in restored["sessions"] + restored["history"]:
            prior = initial[item["provider"]]
            self.assertFalse(item["source_unavailable"])
            self.assertEqual(item["source_unavailable_detail"], "")
            self.assertEqual((item["status"], item["event_id"], item["preview"]),
                             ("ready", prior["event_id"], prior["preview"]))
        self.assertTrue(restored["sessions"][0]["notification_eligible"])
        self.assertEqual(restored["history"][0]["reviewed_event"], codex["event_id"])
        self.assertEqual(source_hashes, fingerprint())

    def test_local_and_cloud_claude_cards_use_their_own_source_health(self):
        monitor = Monitor(self.root / "data", self.root / "codex", self.root / "claude")
        now = time.time() + .01
        monitor.store.apply([
            {"provider": "claude", "id": "local-fixture", "status": "ready", "event_id": "local-reply",
             "updated_at": now, "source_kind": "local"},
            {"provider": "claude", "id": "project:fixture", "status": "ready", "event_id": "project-reply",
             "updated_at": now, "source_kind": "project"},
            {"provider": "claude", "id": "project:legacy-fixture", "status": "ready", "event_id": "legacy-reply",
             "updated_at": now},
        ], now)
        with monitor.lock:
            monitor.health = [{"id": "claude", "available": False, "detail": "Local sessions are unavailable."}]
            monitor.project_health = {"id": "claude_projects", "available": True, "detail": "Project fixture connected."}
        cards = {item["id"]: item for item in monitor.snapshot()["sessions"]}
        self.assertTrue(cards["local-fixture"]["source_unavailable"])
        self.assertFalse(cards["project:fixture"]["source_unavailable"])
        self.assertFalse(cards["project:legacy-fixture"]["source_unavailable"])
        with monitor.lock:
            monitor.health[0]["available"] = True
            monitor.project_health.update(available=False, detail="Project access is unavailable.")
        cards = {item["id"]: item for item in monitor.snapshot()["sessions"]}
        self.assertFalse(cards["local-fixture"]["source_unavailable"])
        self.assertTrue(cards["project:fixture"]["source_unavailable"])
        self.assertTrue(cards["project:legacy-fixture"]["source_unavailable"])
        self.assertEqual(cards["project:fixture"]["source_unavailable_detail"], "Project access is unavailable.")


if __name__ == "__main__":
    unittest.main()
