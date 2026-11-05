"""Lifecycle and read-only regression checks using disposable Codex fixtures."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_source import CodexSource, STALE_SECONDS


class CodexSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = time.time()
        self.cutoff = self.now - 60
        self.db = self.root / "state_5.sqlite"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER, title TEXT, name TEXT, cwd TEXT, archived INTEGER, thread_source TEXT, source TEXT, agent_path TEXT)")
            conn.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)")
        self.source = CodexSource(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def thread(self, ident="chat", *, archived=0, thread_source="user", source="vscode", name=None, timestamp=None):
        path = self.root / "sessions" / time.strftime("%Y/%m/%d", time.gmtime(self.now)) / f"rollout-{ident}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.write(path, {"type": "session_meta", "payload": {"id": ident, "cwd": "/example", "thread_source": thread_source, "source": source}})
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)", (ident, str(path), int(timestamp or self.now), "Source title", name, "/example", archived, thread_source, source))
        return path

    @staticmethod
    def write(path, record):
        with path.open("ab") as handle:
            handle.write(json.dumps(record).encode() + b"\n")

    def event(self, path, kind, *, at=None, turn="turn-1", **extra):
        at = self.now if at is None else at
        payload = {"type": kind, "turn_id": turn, **extra}
        if kind == "task_started":
            payload["started_at"] = at
        if kind in {"task_complete", "turn_aborted", "task_failed", "turn_complete"}:
            payload["completed_at"] = at
        self.write(path, {"timestamp": at, "type": "event_msg", "payload": payload})

    def scan(self, now=None):
        return self.source.scan(now or self.now, self.cutoff)[0]

    def test_start_complete_resume_and_stable_transition(self):
        path = self.thread(name="Custom source name")
        self.event(path, "task_started", at=self.now - 5)
        first = self.scan()[0]
        self.assertEqual(first["status"], "cooking")
        self.assertEqual(first["title"], "Custom source name")
        self.assertEqual(first["started_at"], self.now - 5)
        self.event(path, "task_complete", at=self.now - 3)
        ready = self.scan()[0]
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["updated_at"], self.now - 3)
        self.assertEqual(ready["event_id"], self.scan()[0]["event_id"])
        self.event(path, "task_started", at=self.now - 1, turn="turn-2")
        self.event(path, "task_complete", at=self.now, turn="turn-1")
        resumed = self.scan()[0]
        self.assertEqual(resumed["status"], "cooking")
        self.assertEqual(resumed["turn_id"], "turn-2")
        self.assertNotEqual(ready["event_id"], resumed["event_id"])

    def test_commentary_and_tool_results_do_not_complete_turn(self):
        path = self.thread()
        self.event(path, "task_started")
        self.write(path, {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": "Text is not lifecycle evidence"}})
        self.write(path, {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "phase": "final"}}})
        self.assertEqual(self.scan()[0]["status"], "cooking")

    def test_final_reply_preview_is_literal_bounded_and_cleared_for_next_turn(self):
        path = self.thread()
        self.event(path, "task_started", at=self.now - 5)
        for phase, text in (("commentary", "Still working"), ("final", "The requested draft is ready. " + "x" * 700)):
            self.write(path, {"timestamp": self.now - 2, "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "phase": phase,
                "content": [{"type": "output_text", "text": text}]}})
        self.assertEqual(self.scan()[0]["preview"], "")
        self.event(path, "task_complete", at=self.now - 1)
        ready = self.scan()[0]
        self.assertTrue(ready["preview"].startswith("The requested draft is ready."))
        self.assertEqual(len(ready["preview"]), 500)
        self.assertEqual(ready["preview_kind"], "reply")
        self.event(path, "task_started", at=self.now, turn="turn-2")
        self.assertEqual(self.scan()[0]["preview"], "")

    def test_question_preview_does_not_export_tool_results(self):
        path = self.thread()
        self.event(path, "task_started", at=self.now - 5)
        self.write(path, {"timestamp": self.now - 2, "type": "response_item", "payload": {
            "type": "function_call", "name": "request_user_input", "call_id": "question",
            "arguments": json.dumps({"questions": [{"question": "Which draft should I use?"}]})}})
        waiting = self.scan()[0]
        self.assertEqual(waiting["preview"], "Which draft should I use?")
        self.assertEqual(waiting["preview_kind"], "permission")
        self.write(path, {"timestamp": self.now - 1, "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "question", "output": "Private tool result"}})
        self.assertEqual(self.scan()[0]["preview"], "")

    def test_aborts_failure_and_unsuccessful_complete_are_never_ready(self):
        for ident, kind, extra in (("abort", "turn_aborted", {}), ("failed", "task_failed", {}), ("bad-complete", "task_complete", {"status": "failed"})):
            path = self.thread(ident)
            self.event(path, "task_started", at=self.now - 5)
            self.event(path, kind, at=self.now, **extra)
        self.assertEqual({row["status"] for row in self.scan()}, {"disconnected"})

    def test_filters_archives_subagents_guardians_and_spawn_edges(self):
        paths = [self.thread("archived", archived=1), self.thread("child", thread_source="subagent"), self.thread("guardian", thread_source="guardian_review"), self.thread("json-child", source=json.dumps({"subagent": {"other": "guardian"}})), self.thread("edge-child")]
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO thread_spawn_edges VALUES ('parent', 'edge-child')")
        for path in paths:
            self.event(path, "task_started")
        self.assertEqual(self.scan(), [])

    def test_existing_recent_ready_is_returned_for_store_admission(self):
        path = self.thread()
        self.event(path, "task_started", at=self.cutoff - 20)
        self.event(path, "task_complete", at=self.cutoff - 10)
        self.assertEqual(self.scan()[0]["status"], "ready")

    def test_partial_line_waits_for_newline_then_detects_completion(self):
        path = self.thread()
        self.event(path, "task_started", at=self.now - 5)
        self.assertEqual(self.scan()[0]["status"], "cooking")
        raw = json.dumps({"timestamp": self.now, "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1", "completed_at": self.now}}).encode()
        with path.open("ab") as handle:
            handle.write(raw[:50])
        partial = self.scan()[0]
        self.assertEqual(partial["status"], "disconnected")
        self.assertTrue(partial["history_checking"])
        with path.open("ab") as handle:
            handle.write(raw[50:] + b"\n")
        self.assertEqual(self.scan()[0]["status"], "ready")

    def test_truncation_resets_old_completion(self):
        path = self.thread()
        self.event(path, "task_started", at=self.now - 5)
        self.event(path, "task_complete", at=self.now - 3)
        self.assertEqual(self.scan()[0]["status"], "ready")
        path.write_bytes(json.dumps({"type": "session_meta", "payload": {"id": "chat"}}).encode() + b"\n")
        self.assertEqual(self.scan()[0]["status"], "disconnected")
        self.event(path, "task_started", turn="new-turn")
        self.assertEqual(self.scan()[0]["status"], "cooking")

    def test_stale_unmatched_start_is_uncertain_not_ready(self):
        old = self.now - STALE_SECONDS - 20
        path = self.thread(timestamp=old)
        self.event(path, "task_started", at=old)
        os.utime(path, (old, old))
        row = self.scan()[0]
        self.assertEqual(row["status"], "disconnected")
        self.assertIn("may still be running", row["detail"])

    def test_explicit_input_wait_and_response(self):
        path = self.thread()
        self.event(path, "task_started", at=self.now - 5)
        self.write(path, {"timestamp": self.now - 3, "type": "response_item", "payload": {"type": "function_call", "name": "functions.request_user_input", "call_id": "ask-1"}})
        self.assertEqual(self.scan()[0]["status"], "needs_you")
        self.write(path, {"timestamp": self.now - 2, "type": "response_item", "payload": {"type": "function_call_output", "call_id": "unrelated"}})
        self.assertEqual(self.scan()[0]["status"], "needs_you")
        self.write(path, {"timestamp": self.now - 1, "type": "response_item", "payload": {"type": "function_call_output", "call_id": "ask-1"}})
        self.assertEqual(self.scan()[0]["status"], "cooking")

    def test_missing_index_fallback_and_database_bytes_unchanged(self):
        path = self.thread()
        self.event(path, "task_started")
        before = self.db.read_bytes()
        self.assertEqual(self.scan()[0]["status"], "cooking")
        self.assertEqual(before, self.db.read_bytes())
        with sqlite3.connect(self.db) as conn:
            conn.execute("DELETE FROM threads")
        self.assertEqual(self.scan()[0]["id"], "chat")
        self.db.unlink()
        rows, health = self.source.scan(self.now, self.cutoff)
        self.assertEqual(rows[0]["status"], "cooking")
        self.assertTrue(health["available"])

    def test_large_record_does_not_block_later_lifecycle(self):
        path = self.thread()
        self.event(path, "task_started", at=self.now - 5)
        self.assertEqual(self.scan()[0]["status"], "cooking")
        raw = json.dumps({"type": "response_item", "timestamp": self.now - 1, "payload": {
            "type": "function_call_output", "call_id": "unrelated", "output": "x" * 2000000}}).encode()
        with path.open("ab") as handle:
            handle.write(raw[:1100000])
        self.assertTrue(self.scan()[0]["history_checking"])
        with path.open("ab") as handle:
            handle.write(raw[1100000:] + b"\n")
        self.event(path, "task_complete", at=self.now)
        for _ in range(50):
            item = self.scan()[0]
            if not item["history_checking"]:
                break
        self.assertEqual(item["status"], "ready")

    def test_missing_rollout_is_disconnected(self):
        path = self.thread()
        self.event(path, "task_complete")
        self.assertEqual(self.scan()[0]["status"], "ready")
        path.unlink()
        self.assertEqual(self.scan()[0]["status"], "disconnected")


if __name__ == "__main__":
    unittest.main()
