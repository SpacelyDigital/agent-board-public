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

    def backlogged_histories(self, active_count=1, active_padding=0):
        active = []
        for index in range(active_count):
            path = self.thread(f"active-{index}", timestamp=self.now - index)
            self.event(path, "task_started", at=self.now - 1, turn="first")
            if active_padding:
                self.write(path, {"type": "trace", "payload": {"ignored": "x" * active_padding}})
            active.append(path)
        old = []
        for index in range(37):
            path = self.thread(f"older-{index}", timestamp=self.now - 200 - index)
            self.event(path, "task_started", at=self.now - 300 - index)
            for _ in range(96):
                self.write(path, {"type": "trace", "payload": {"ignored": "x" * 1024}})
            os.utime(path, (self.now - 200, self.now - 200))
            old.append(path)
        return active, old

    @patch("claude_source.TRANSCRIPT_POLL_SECONDS", 10)
    @patch("claude_source.TRANSCRIPT_POLL_BYTES", 8192)
    def test_newest_cold_history_finishes_before_old_history_rotation(self):
        active, _ = self.backlogged_histories(active_padding=12000)
        # The index still supplies a startup candidate when no file is recent.
        os.utime(active[0], (self.now - 200, self.now - 200))
        first = next(row for row in self.scan() if row["id"] == "active-0")
        self.assertTrue(first["history_checking"])
        for poll in range(1, 5):
            row = next(row for row in self.scan(self.now + poll * 2) if row["id"] == "active-0")
            if not row["history_checking"]:
                break
        self.assertFalse(row["history_checking"])
        self.assertEqual(row["status"], "cooking")

    @patch("claude_source.TRANSCRIPT_POLL_SECONDS", 10)
    @patch("claude_source.TRANSCRIPT_POLL_BYTES", 8192)
    def test_repeated_active_appends_do_not_wait_for_old_histories(self):
        active, _ = self.backlogged_histories()
        first = next(row for row in self.scan() if row["id"] == "active-0")
        self.assertFalse(first["history_checking"])
        generation = first["history_revision"]["generation"]
        for turn in range(1, 5):
            at = self.now + turn * 10
            self.event(active[0], "task_started", at=at, turn=f"next-{turn}")
            os.utime(active[0], (at, at))
            for poll in range(4):
                row = next(row for row in self.scan(at + poll * 2) if row["id"] == "active-0")
                if not row["history_checking"]:
                    break
            self.assertFalse(row["history_checking"])
            self.assertEqual(row["turn_id"], f"next-{turn}")
            self.assertEqual(row["history_revision"]["generation"], generation)

    @patch("claude_source.TRANSCRIPT_POLL_SECONDS", 10)
    @patch("claude_source.TRANSCRIPT_POLL_BYTES", 8192)
    def test_warm_active_history_stays_verified_on_every_poll_amid_cold_backlog(self):
        active, old = self.backlogged_histories(active_padding=3000)
        first = next(row for row in self.scan() if row["id"] == "active-0")
        self.assertEqual(first["status"], "cooking")
        for poll in range(12):
            at = self.now + (poll + 1) * 2
            # Real tool/commentary appends do not create another lifecycle.
            # Every poll must verify them, including while old histories index.
            self.write(active[0], {"type": "trace", "payload": {"ignored": "progress"}})
            os.utime(active[0], (at, at))
            row = next(row for row in self.scan(at) if row["id"] == "active-0")
            self.assertFalse(row["history_checking"], f"poll {poll}")
            self.assertEqual(row["status"], "cooking")
            self.assertEqual(row["event_id"], first["event_id"])
            self.assertEqual(row["history_revision"]["size"], active[0].stat().st_size)
            self.assertEqual(row["history_revision"]["generation"], first["history_revision"]["generation"])
        self.assertTrue(any(self.source._cursors[path].offset > 0 for path in old))

    @patch("claude_source.TRANSCRIPT_POLL_SECONDS", 10)
    @patch("claude_source.TRANSCRIPT_POLL_BYTES", 8192)
    def test_multiple_recent_histories_share_priority_after_appending(self):
        active, _ = self.backlogged_histories(active_count=3, active_padding=12000)
        for poll in range(12):
            rows = {row["id"]: row for row in self.scan(self.now + poll * 2)}
            if all(not rows[f"active-{index}"]["history_checking"] for index in range(3)):
                break
        self.assertTrue(all(not rows[f"active-{index}"]["history_checking"] for index in range(3)))
        for index, path in enumerate(active):
            self.event(path, "task_started", at=self.now + 30, turn=f"next-{index}")
            os.utime(path, (self.now + 30, self.now + 30))
        for poll in range(12):
            rows = {row["id"]: row for row in self.scan(self.now + 30 + poll * 2)}
            if all(not rows[f"active-{index}"]["history_checking"] for index in range(3)):
                break
        for index in range(3):
            self.assertFalse(rows[f"active-{index}"]["history_checking"])
            self.assertEqual(rows[f"active-{index}"]["turn_id"], f"next-{index}")

    @patch("claude_source.TRANSCRIPT_POLL_SECONDS", 10)
    @patch("claude_source.TRANSCRIPT_POLL_BYTES", 8192)
    def test_recent_history_rotation_survives_changing_index_order(self):
        active, _ = self.backlogged_histories(active_count=2, active_padding=200000)
        original_rows = self.source._thread_rows
        original_read = self.source._read
        calls = [0]
        first_read = []
        first_in_poll = [True]

        def changing_recency(since):
            rows, error = original_rows(since)
            # Both chats stay active, but their relative index recency changes
            # before the next pass.
            if calls[0] % 2:
                rows[0], rows[1] = rows[1], rows[0]
            calls[0] += 1
            return rows, error

        def read(path, budget, ident):
            if first_in_poll[0]:
                first_read.append(ident)
                first_in_poll[0] = False
            return original_read(path, budget, ident)

        with patch.object(self.source, "_thread_rows", side_effect=changing_recency), patch.object(self.source, "_read", side_effect=read):
            for poll in range(6):
                first_in_poll[0] = True
                self.scan(self.now + poll * 2)
        self.assertEqual(first_read, ["active-0", "active-1"] * 3)
        self.assertTrue(all(self.source._cursors[path].offset > 0 for path in active))

    @patch("claude_source.TRANSCRIPT_POLL_SECONDS", 10)
    @patch("claude_source.TRANSCRIPT_POLL_BYTES", 8192)
    def test_two_growing_histories_stay_verified_when_their_prefixes_fit(self):
        active, _ = self.backlogged_histories(active_count=2, active_padding=1000)
        self.scan()
        for poll in range(12):
            at = self.now + (poll + 1) * 2
            for path in active:
                self.write(path, {"type": "trace", "payload": {"ignored": "progress"}})
                os.utime(path, (at, at))
            with sqlite3.connect(self.db) as conn:
                conn.execute("UPDATE threads SET updated_at=? WHERE id=?",
                             (int(at), f"active-{poll % 2}"))
            rows = {row["id"]: row for row in self.scan(at)}
            for index, path in enumerate(active):
                self.assertEqual(rows[f"active-{index}"]["status"], "cooking")
                self.assertEqual(rows[f"active-{index}"]["history_revision"]["size"], path.stat().st_size)

    @patch("claude_source.TRANSCRIPT_POLL_SECONDS", 10)
    @patch("claude_source.TRANSCRIPT_POLL_BYTES", 8192)
    def test_stale_newest_index_row_cannot_take_recent_history_budget(self):
        active, old = self.backlogged_histories(active_padding=3000)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE threads SET updated_at=? WHERE id='older-0'", (int(self.now + 50),))
        for poll in range(4):
            at = self.now + poll * 2
            self.write(active[0], {"type": "trace", "payload": {"ignored": "progress"}})
            os.utime(active[0], (at, at))
            row = next(row for row in self.scan(at) if row["id"] == "active-0")
            self.assertEqual(row["status"], "cooking")
        self.assertTrue(any(self.source._cursors[path].offset > 0 for path in old))

    @patch("claude_source.TRANSCRIPT_POLL_BYTES", 8192)
    def test_foreground_cannot_consume_reserved_background_time_or_bytes(self):
        active, old = self.backlogged_histories(active_padding=20000)
        clock = [100.0]
        original = self.source._read
        background_started = []

        def read(path, budget, ident):
            before = (clock[0], budget.deadline, budget.remaining)
            result = original(path, budget, ident)
            if path in active:
                clock[0] = budget.deadline
            elif not background_started:
                background_started.append(before)
            return result

        with patch("claude_source.time.monotonic", side_effect=lambda: clock[0]), patch.object(self.source, "_read", side_effect=read):
            self.scan()
        started, deadline, remaining = background_started[0]
        self.assertGreater(deadline, started)
        self.assertAlmostEqual(deadline, 100.075)
        self.assertGreaterEqual(remaining, 1024)
        self.assertTrue(any(self.source._cursors[path].offset > 0 for path in old))

    @patch("claude_source.TRANSCRIPT_POLL_SECONDS", 10)
    @patch("claude_source.TRANSCRIPT_POLL_BYTES", 8192)
    def test_over_budget_continuous_change_never_retains_unverified_cooking(self):
        active, _ = self.backlogged_histories(active_padding=20000)
        for poll in range(6):
            row = next(row for row in self.scan() if row["id"] == "active-0")
            if not row["history_checking"]:
                break
        self.assertEqual(row["status"], "cooking")
        for poll in range(5):
            at = self.now + poll * 2
            self.write(active[0], {"type": "trace", "payload": {"ignored": "progress"}})
            os.utime(active[0], (at, at))
            row = next(row for row in self.scan(at) if row["id"] == "active-0")
            self.assertEqual(row["status"], "disconnected")
            self.assertTrue(row["history_checking"])
            self.assertNotIn("history_revision", row)
        # Once a stable prefix can be checked across bounded polls, it recovers.
        for poll in range(6):
            row = next(row for row in self.scan() if row["id"] == "active-0")
            if not row["history_checking"]:
                break
        self.assertEqual(row["status"], "cooking")

    @patch("claude_source.TRANSCRIPT_POLL_SECONDS", 10)
    @patch("claude_source.TRANSCRIPT_POLL_BYTES", 8192)
    def test_priority_keeps_global_budget_and_old_history_progress(self):
        active, old = self.backlogged_histories()
        original = self.source._read
        used = []
        budgets = []

        def read(path, budget, ident):
            before = budget.remaining
            result = original(path, budget, ident)
            used.append(before - budget.remaining)
            budgets.append(id(budget))
            return result

        with patch.object(self.source, "_read", side_effect=read):
            for poll in range(2 * (len(old) + 1)):
                used.clear()
                budgets.clear()
                at = self.now + poll * 2
                self.event(active[0], "task_started", at=at, turn=f"ongoing-{poll}")
                os.utime(active[0], (at, at))
                self.scan(at)
                self.assertLessEqual(sum(used), 8192)
                self.assertEqual(len(set(budgets)), 1)
        self.assertTrue(all(self.source._cursors[path].offset > 0 for path in old))

    @patch("claude_source.TRANSCRIPT_POLL_BYTES", 8192)
    def test_slow_scheduling_metadata_does_not_exhaust_history_budget(self):
        self.backlogged_histories()
        original = Path.lstat
        elapsed = [0.0]
        calls = [0]

        def delayed_stat(path):
            calls[0] += 1
            if calls[0] <= 38:
                elapsed[0] += .01
            return original(path)

        with patch.object(Path, "lstat", delayed_stat), patch("claude_source.time.monotonic", side_effect=lambda: 100 + elapsed[0]):
            row = next(row for row in self.scan() if row["id"] == "active-0")
        self.assertFalse(row["history_checking"])
        self.assertEqual(row["status"], "cooking")

    @patch("claude_source.TRANSCRIPT_POLL_SECONDS", 10)
    @patch("claude_source.TRANSCRIPT_POLL_BYTES", 8192)
    def test_recent_replacement_still_requires_new_verified_content(self):
        active, _ = self.backlogged_histories()
        self.event(active[0], "task_complete", at=self.now, turn="first")
        first = next(row for row in self.scan() if row["id"] == "active-0")
        self.assertEqual(first["status"], "ready")
        replacement = active[0].with_suffix(".replacement")
        self.write(replacement, {"type": "session_meta", "payload": {"id": "active-0", "thread_source": "user"}})
        self.event(replacement, "task_started", at=self.now + 1, turn="replacement")
        replacement.replace(active[0])
        for poll in range(4):
            row = next(row for row in self.scan(self.now + 2 + poll * 2) if row["id"] == "active-0")
            if not row["history_checking"]:
                break
            self.assertNotEqual(row["status"], "ready")
        self.assertFalse(row["history_checking"])
        self.assertEqual(row["status"], "cooking")
        self.assertEqual(row["turn_id"], "replacement")
        self.assertNotEqual(row["history_revision"]["generation"], first["history_revision"]["generation"])

    def test_missing_rollout_is_disconnected(self):
        path = self.thread()
        self.event(path, "task_complete")
        self.assertEqual(self.scan()[0]["status"], "ready")
        path.unlink()
        self.assertEqual(self.scan()[0]["status"], "disconnected")


if __name__ == "__main__":
    unittest.main()
