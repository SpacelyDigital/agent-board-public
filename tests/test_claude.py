import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import ANY, patch
from datetime import datetime
import claude_source

from claude_source import ClaudeSource, TAIL_BYTES, _process_alive, _prompt_caption, _task_notification


class ClaudeSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "projects" / "-project" / "session-1.jsonl"
        self.path.parent.mkdir(parents=True)
        self.source = ClaudeSource(self.root)
        self.now = 1793307000.0

    def tearDown(self):
        self.temp.cleanup()

    def test_explicit_root_never_discovers_desktop_even_when_it_equals_default(self):
        with patch("claude_source.Path.home", return_value=self.root), patch("claude_source._find_claude") as finder:
            source = ClaudeSource(self.root / ".claude")
        finder.assert_not_called()
        self.assertIsNone(source.command)
        self.assertIsNone(source.desktop_root)
        self.assertFalse(source.app_available)
        self.assertFalse(source.desktop_only)

    def test_command_disabled_reader_imports_without_process_helper_module(self):
        code = """
import sys
from pathlib import Path
sys.modules['process_reader'] = None
from claude_source import ClaudeSource
source = ClaudeSource(Path(sys.argv[1]), desktop_root=Path(sys.argv[1]) / 'desktop',
                      allow_commands=False, confine_paths=True)
assert source.command is None
assert source._live_sessions(1000) == ({}, False)
assert sys.modules['process_reader'] is None
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.root)],
                                cwd=Path(claude_source.__file__).parent, capture_output=True,
                                timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_enabled_command_output_limit_falls_back_without_leaking_output(self):
        from process_reader import OutputLimitExceeded
        self.source.command = "/fixture/claude"
        with patch("process_reader.read_process", side_effect=OutputLimitExceeded("fixture private output")) as reader:
            self.assertEqual(self.source._live_sessions(self.now), ({}, False))
        reader.assert_called_once()

    def write(self, *events):
        with self.path.open("a") as stream:
            for event in events:
                stream.write(json.dumps({"sessionId": "session-1", "cwd": "/work/project", **event}) + "\n")

    def prompt(self, identifier="prompt-1", timestamp=None):
        return {"type": "user", "uuid": identifier, "promptId": identifier,
                "timestamp": timestamp or self.now - 30,
                "message": {"role": "user", "content": "Disposable test request"}}

    def assistant(self, stop_reason="end_turn", timestamp=None):
        return {"type": "assistant", "uuid": "reply-" + str(timestamp or self.now - 10),
                "timestamp": timestamp or self.now - 10,
                "message": {"role": "assistant", "stop_reason": stop_reason,
                            "content": [{"type": "text", "text": "Disposable test reply"}]}}

    def scan(self):
        observations, source = self.source.scan(self.now, self.now - 3600)
        self.assertTrue(source["available"])
        return observations[0]

    def drain(self, source=None, limit=100):
        source = source or self.source
        for _ in range(limit):
            observations, health = source.scan(self.now, self.now - 3600)
            if observations and all(cursor.phase == "idle" for cursor in source._tails.values()):
                return observations[0]
        self.fail("The bounded index did not finish")

    def registry(self, **fields):
        path = self.root / "sessions" / "123.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"sessionId": "session-1", "pid": 123,
                                    "statusUpdatedAt": (self.now - 5) * 1000, **fields}))

    def desktop_metadata(self, **fields):
        desktop = self.root / "desktop"
        path = desktop / "account" / "org" / "local_123.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sessionId": "local_123", "cliSessionId": "session-1",
                                    "cwd": "/work/project", "createdAt": (self.now - 1000) * 1000,
                                    "isArchived": False, "title": "Remote Control session (from a project)",
                                    **fields}))
        self.source.desktop_root = desktop
        self.source.desktop_only = True
        self.source.app_available = True
        return path

    def test_completion_and_same_session_new_turn(self):
        self.write(self.prompt(), self.assistant())
        first = self.scan()
        self.assertEqual(first["status"], "ready")
        self.assertFalse(first["confirmed_live"])
        self.assertEqual(first["turn_id"], "prompt-1")
        self.assertEqual(first["started_at"], self.now - 30)
        self.assertEqual(first["event_id"], self.scan()["event_id"])
        self.write(self.prompt("prompt-2", self.now))
        second = self.scan()
        self.assertEqual(second["status"], "cooking")
        self.assertEqual(second["turn_id"], "prompt-2")
        self.assertNotEqual(first["event_id"], second["event_id"])

    def test_tool_results_are_not_user_prompts(self):
        tool = self.assistant("tool_use")
        tool["message"]["content"] = [{"type": "tool_use", "name": "Bash", "id": "tool-1"}]
        self.write(self.prompt(), tool, {"type": "user", "uuid": "result-1", "promptId": "prompt-1",
                   "timestamp": self.now - 2, "message": {"role": "user", "content": [
                       {"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}]}})
        item = self.scan()
        self.assertEqual(item["status"], "cooking")
        self.assertEqual(item["turn_id"], "prompt-1")

    def background_launch(self, *, failed=False, requested=True):
        tool = self.assistant("tool_use", self.now - 20)
        tool["message"]["content"] = [{"type": "tool_use", "name": "Bash", "id": "tool-background",
                                      "input": {"command": "sleep 90", "run_in_background": requested}}]
        result = {"type": "user", "uuid": "background-result", "timestamp": self.now - 19,
                  "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-background",
                                             "is_error": failed, "content": "Fixture-only tool output"}]}}
        if not failed:
            result["toolUseResult"] = {"backgroundTaskId": "background-1"}
        self.write(self.prompt(), tool, result, self.assistant(timestamp=self.now - 18))

    def notification(self, identifier="background-1", status="completed", **fields):
        return {"type": "user", "isMeta": True, "uuid": "background-notification",
                "timestamp": self.now - 1, "message": {"content":
                    "<task-notification><task-id>" + identifier + "</task-id><status>" + status
                    + "</status><summary>Private fixture task output</summary></task-notification>"}, **fields}

    def test_background_tool_result_prevents_premature_end_turn_ready(self):
        # These are synthetic lifecycle fixtures. Native schema parity is a
        # separate release check, not established by this test.
        for requested in (True, False):
            self.path.unlink(missing_ok=True)
            self.background_launch(requested=requested)
            item = self.scan()
            self.assertEqual(item["status"], "cooking")
            self.assertEqual(item["turn_id"], "prompt-1")
            self.assertNotIn("preview", item)
            self.assertNotIn("Fixture-only tool output", json.dumps(item))

    def test_background_completion_requires_matching_task_before_final_reply(self):
        self.background_launch()
        self.write(self.notification("other-task"), self.assistant(timestamp=self.now))
        self.assertEqual(self.scan()["status"], "cooking")
        self.write(self.notification())
        self.assertEqual(self.scan()["status"], "cooking")
        self.write(self.assistant(timestamp=self.now + 1))
        item = self.scan()
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["turn_id"], "prompt-1")
        self.assertEqual(item["started_at"], self.now - 30)
        self.assertNotIn("Private fixture", json.dumps(item))

    def test_background_stop_and_main_turn_interruption_are_not_ready(self):
        for status in ("failed", "killed", "stopped"):
            self.path.unlink(missing_ok=True)
            self.background_launch()
            self.write(self.notification(status=status))
            self.assertEqual(self.scan()["status"], "disconnected")
            self.write(self.assistant(timestamp=self.now))
            with patch.object(self.source, "_live_sessions", return_value=({"session-1": {"state": "done"}}, True)):
                self.assertEqual(self.scan()["status"], "disconnected")
            self.write(self.prompt("prompt-recovery", self.now + 1), self.assistant(timestamp=self.now + 2))
            self.assertEqual(self.scan()["status"], "ready")
        self.path.unlink()
        self.background_launch()
        self.write({"type": "user", "timestamp": self.now, "message": {
            "content": "[Request interrupted by user]"}})
        self.assertEqual(self.scan()["status"], "disconnected")

    def test_failed_background_launch_does_not_leave_pending_task(self):
        self.background_launch(failed=True)
        self.assertEqual(self.scan()["status"], "ready")

    def test_native_desktop_queued_killed_notification_settles_task_before_model_receives_it(self):
        # Lifecycle shape observed in the disposable native Claude Code test;
        # IDs, timestamps, output path, and task summary are fixture-only.
        self.background_launch()
        self.write({"type": "queue-operation", "operation": "enqueue", "timestamp": self.now,
                    "content": "<task-notification>\n<task-id>background-1</task-id>\n"
                    "<tool-use-id>tool-background</tool-use-id>\n"
                    "<output-file>/private/tmp/disposable/tasks/background-1.output</output-file>\n"
                    "<status>killed</status>\n<summary>Private fixture output</summary>\n</task-notification>"})
        item = self.scan()
        self.assertEqual(item["status"], "disconnected")
        self.assertEqual(item["turn_id"], "prompt-1")
        self.assertEqual(item["updated_at"], self.now)
        self.assertNotIn("preview", item)
        self.assertNotIn("Private fixture", json.dumps(item))
        self.assertNotIn("/private/tmp", json.dumps(item))

    def test_queued_completion_can_correlate_background_request_by_tool_id(self):
        tool = self.assistant("tool_use")
        tool["message"]["content"] = [{"type": "tool_use", "name": "Bash", "id": "tool-background",
                                      "input": {"command": "sleep 90", "run_in_background": True}}]
        self.write(self.prompt(), tool, self.assistant())
        self.write({"type": "queue-operation", "operation": "enqueue", "timestamp": self.now,
                    "content": "<task-notification><task-id>background-1</task-id>"
                    "<tool-use-id>tool-background</tool-use-id><status>completed</status></task-notification>"})
        self.assertEqual(self.scan()["status"], "cooking")
        self.write(self.assistant(timestamp=self.now + 1))
        self.assertEqual(self.scan()["status"], "ready")

    def test_foreground_idle_or_done_does_not_complete_background_work(self):
        self.background_launch()
        for row in ({"status": "idle"}, {"state": "done"}):
            with patch.object(self.source, "_live_sessions", return_value=({"session-1": row}, True)):
                self.assertEqual(self.scan()["status"], "cooking")
        self.registry(status="idle")
        with patch("claude_source._process_alive", return_value=True):
            self.assertEqual(self.scan()["status"], "cooking")
            self.now += 181
            item = self.scan()
            self.assertEqual(item["status"], "disconnected")
            self.assertIn("background work could not be confirmed", item["detail"])

    def test_notification_cannot_supply_prompt_title_or_change_turn_identity(self):
        self.write(self.notification())
        item = self.source._transcript(self.path)
        self.assertEqual(item["status"], "disconnected")
        self.assertIsNone(item["turn_id"])
        self.assertEqual(item["prompt_title"], "")
        self.assertNotIn("Private fixture", json.dumps(item))

    def test_assistant_prose_alone_is_not_background_lifecycle_evidence(self):
        reply = self.assistant()
        reply["message"]["content"] = "Example documentation: a command is running in the background."
        self.write(self.prompt(), reply)
        self.assertEqual(self.scan()["status"], "ready")

    def test_malformed_notification_envelope_is_bounded_and_requires_direct_children(self):
        started = time.monotonic()
        self.assertEqual(_task_notification("<task-notification>" + " " * 2000 + "X"), ("", "", ""))
        self.assertLess(time.monotonic() - started, 1)
        invalid = (
            "<task-notification><summary><task-id>background-1</task-id><status>completed</status></summary></task-notification>",
            "<task-notification><task-id>background-1</task-id><status>completed</status><status>killed</status></task-notification>",
            "<task-notification><task-id>background-1</task-id><status><summary>completed</summary></status></task-notification>",
            "<!DOCTYPE task-notification [<!ENTITY id 'background-1'>]><task-notification><task-id>&id;</task-id><status>completed</status></task-notification>",
        )
        for content in invalid:
            self.assertEqual(_task_notification(content), ("", "", ""))
        self.background_launch()
        self.write({"type": "queue-operation", "operation": "enqueue", "timestamp": self.now,
                    "content": invalid[0]}, self.assistant(timestamp=self.now + 1))
        self.assertEqual(self.scan()["status"], "cooking")

    def test_oversized_progress_retains_unsettled_background_work_warm_or_cold(self):
        self.background_launch()
        self.assertEqual(self.scan()["status"], "cooking")
        self.write({"type": "progress", "timestamp": self.now, "content": "x" * (TAIL_BYTES + 100)},
                   self.assistant(timestamp=self.now + 1))
        for source in (self.source, ClaudeSource(self.root)):
            with patch.object(source, "_live_sessions", return_value=({"session-1": {"state": "done"}}, True)):
                item = self.drain(source)
            self.assertEqual(item["status"], "cooking")
            self.assertFalse(item["history_checking"])
            self.assertTrue(source._transcript(self.path)["background_pending"])
            self.assertNotIn("preview", item)

    def test_notification_mixed_task_and_tool_ids_cannot_settle_two_launches(self):
        self.background_launch()
        tool = self.assistant("tool_use", self.now - 5)
        tool["message"]["content"] = [{"type": "tool_use", "name": "Bash", "id": "other-tool",
                                      "input": {"command": "sleep 90", "run_in_background": True}}]
        self.write(tool, {"type": "queue-operation", "operation": "enqueue", "timestamp": self.now,
                          "content": "<task-notification><task-id>background-1</task-id>"
                          "<tool-use-id>other-tool</tool-use-id><status>completed</status></task-notification>"},
                   self.assistant(timestamp=self.now + 1))
        self.assertEqual(self.scan()["status"], "cooking")
        self.write(self.notification(), self.assistant(timestamp=self.now + 2))
        # Correctly settling the first task must still leave the second request.
        self.assertEqual(self.scan()["status"], "cooking")

    def test_older_background_notification_and_queue_dequeue_cannot_settle_launch(self):
        self.background_launch()
        self.write(self.notification(timestamp=self.now - 100), self.assistant(timestamp=self.now))
        self.assertEqual(self.scan()["status"], "cooking")
        self.write({"type": "queue-operation", "operation": "dequeue", "timestamp": self.now,
                    "content": self.notification()["message"]["content"]}, self.assistant(timestamp=self.now + 1))
        self.assertEqual(self.scan()["status"], "cooking")

    def test_ordinary_user_notification_text_is_a_new_prompt_and_cannot_settle_work(self):
        self.background_launch()
        self.write(self.notification(isMeta=False, promptSource="user", promptId="prompt-2"),
                   self.assistant(timestamp=self.now))
        item = self.scan()
        self.assertEqual(item["status"], "cooking")
        self.assertEqual(item["turn_id"], "prompt-2")

    def test_completed_notification_before_delayed_result_does_not_resurrect_work(self):
        tool = self.assistant("tool_use", self.now - 20)
        tool["message"]["content"] = [{"type": "tool_use", "name": "Bash", "id": "tool-background",
                                      "input": {"command": "sleep 90", "run_in_background": True}}]
        self.write(self.prompt(), tool,
                   {"type": "queue-operation", "operation": "enqueue", "timestamp": self.now - 1,
                    "content": "<task-notification><task-id>background-1</task-id>"
                    "<tool-use-id>tool-background</tool-use-id><status>completed</status></task-notification>"},
                   {"type": "user", "timestamp": self.now - 19, "toolUseResult": {"backgroundTaskId": "background-1"},
                    "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-background", "content": "Fixture output"}]}},
                   self.assistant(timestamp=self.now))
        self.assertEqual(self.scan()["status"], "ready")
        # Reusing a settled identity with a newer launch timestamp is ambiguous.
        tool["timestamp"] = self.now + 1
        self.write(tool, self.assistant(timestamp=self.now + 2))
        self.assertEqual(self.scan()["status"], "disconnected")

    def test_malformed_background_identity_preserves_uncertainty(self):
        self.write(self.prompt(), {"type": "user", "timestamp": self.now - 1,
                                  "toolUseResult": {"backgroundTaskId": ["malformed"]},
                                  "message": {"content": [{"type": "tool_result", "content": "Fixture output"}]}},
                   self.assistant(timestamp=self.now))
        self.assertEqual(self.scan()["status"], "disconnected")

    def test_reused_live_tool_identity_cannot_be_certified_by_an_older_launch(self):
        self.background_launch()
        tool = self.assistant("tool_use", self.now - 5)
        tool["message"]["content"] = [{"type": "tool_use", "name": "Bash", "id": "tool-background",
                                      "input": {"command": "sleep 90", "run_in_background": True}}]
        self.write(tool, self.notification(), self.assistant(timestamp=self.now))
        self.assertEqual(self.scan()["status"], "disconnected")

    def long_progress(self):
        # Many ordinary bounded records, rather than a single oversized record.
        for _ in range(65):
            self.write({"type": "progress", "timestamp": self.now - 2, "content": "x" * 65536})

    def test_long_history_indexes_pending_then_completion_warm_and_cold(self):
        budget = patch("claude_source.TRANSCRIPT_POLL_BYTES", 4 * 1024 * 1024)
        budget.start()
        self.addCleanup(budget.stop)
        self.background_launch()
        self.long_progress()
        self.write(self.assistant(timestamp=self.now))
        self.source.tracked_ids = {"session-1"}
        first = self.scan()
        self.assertTrue(first["history_checking"])
        self.assertNotEqual(first["status"], "ready")
        pending = self.drain()
        self.assertEqual(pending["status"], "cooking")
        self.assertFalse(pending["history_checking"])
        self.write(self.notification(), self.assistant(timestamp=self.now + 1))
        checking = self.scan()
        self.assertTrue(checking["history_checking"])
        completed = self.drain()
        cold = self.drain(ClaudeSource(self.root))
        self.assertEqual(completed["status"], "ready")
        self.assertEqual(cold["event_id"], completed["event_id"])
        self.assertEqual(cold["turn_id"], "prompt-1")

    def test_same_inode_historical_rewrite_and_growth_invalidates_cached_ready(self):
        budget = patch("claude_source.TRANSCRIPT_POLL_BYTES", 4 * 1024 * 1024)
        budget.start()
        self.addCleanup(budget.stop)
        disguised = {"type": "progress", "timestamp": self.now - 5,
                     "toolUseResult": {"backgroundTaskId": "auto-task"}, "message": {
                         "content": [{"type": "tool_result", "tool_use_id": "auto-tool"}]}}
        self.write(self.prompt(), disguised, self.assistant())
        self.long_progress()
        self.write(self.assistant(timestamp=self.now))
        self.assertEqual(self.drain()["status"], "ready")
        inode = self.path.stat().st_ino
        raw = self.path.read_bytes()
        needle = b'"type": "progress"'
        replacement = b'"type": "user"    '
        self.assertEqual(len(needle), len(replacement))
        with self.path.open("r+b") as stream:
            stream.write(raw.replace(needle, replacement, 1))
        self.write(self.assistant(timestamp=self.now + 1))
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertTrue(self.scan()["history_checking"])
        self.assertEqual(self.drain()["status"], "cooking")

    def test_saved_revision_is_verified_during_the_budgeted_read_and_never_from_records(self):
        self.write(self.prompt(), self.assistant())
        initial = self.scan()
        revision = initial["history_revision"]
        self.assertTrue(initial["history_verified"])
        self.write({"type": "progress", "content": "x" * 65536})
        restarted = ClaudeSource(self.root)
        restarted.tracked_ids = {"session-1"}
        restarted.revision_lookup = lambda ident: dict(revision)
        with patch("claude_source.TRANSCRIPT_POLL_BYTES", 1024):
            first = restarted.scan(self.now, self.now - 3600)[0][0]
            self.assertTrue(first["history_checking"])
            self.assertFalse(first["history_verified"])
            self.assertNotIn("history_revision", first)
        complete = self.drain(restarted)
        self.assertEqual(complete["history_revision"]["generation"], revision["generation"])
        self.assertGreater(complete["history_revision"]["size"], revision["size"])
        self.assertNotEqual(complete["history_revision"]["sha256"], revision["sha256"])
        forged = dict(revision, generation="0" * 64)
        self.write({"type": "progress", "history_verified": True, "history_revision": forged})
        after_forgery = self.drain(restarted)
        self.assertEqual(after_forgery["history_revision"]["generation"], revision["generation"])
        self.path.write_text(json.dumps({"sessionId": "session-1", **self.prompt("replacement", self.now - 40)}) + "\n")
        rewritten = self.drain(restarted)
        self.assertNotEqual(rewritten["history_revision"]["generation"], revision["generation"])

    def test_unknown_history_cannot_emit_stored_revision_authority(self):
        self.write(self.prompt(), self.assistant())
        ready = self.scan()
        self.assertTrue(ready["history_verified"])
        with self.path.open("a") as stream:
            stream.write("not-json\n")
        unsupported = self.drain()
        self.assertTrue(unsupported["history_checking"])
        self.assertFalse(unsupported["history_verified"])
        self.assertNotIn("history_revision", unsupported)

    def test_rotation_truncation_and_partial_record_never_reuse_a_stale_completion(self):
        self.write(self.prompt(), self.assistant())
        ready = self.scan()
        self.path.write_text("")
        self.write(self.prompt("new-turn", self.now))
        self.assertEqual(self.scan()["status"], "cooking")
        replacement = self.path.with_suffix(".replacement")
        replacement.write_text(json.dumps({"sessionId": "session-1", **self.assistant(timestamp=self.now + 1)}) + "\n")
        replacement.replace(self.path)
        self.assertEqual(self.scan()["status"], "ready")
        with self.path.open("ab") as stream:
            stream.write(b'{"type":"user","message":')
        self.assertTrue(self.scan()["history_checking"])
        with self.path.open("ab") as stream:
            stream.write(b'{"content":"A new disposable request"},"timestamp":1793307002,"uuid":"partial-turn"}\n')
        self.assertEqual(self.scan()["status"], "cooking")

    def test_incremental_read_budget_and_scheduler_fairness_are_global(self):
        self.path.unlink(missing_ok=True)
        for identifier in ("a", "b"):
            path = self.path.with_name(identifier + ".jsonl")
            path.write_text(json.dumps({"sessionId": identifier, "type": "user", "timestamp": self.now,
                                        "message": {"content": "x" * 1000}}) + "\n")
        with patch("claude_source.TRANSCRIPT_POLL_BYTES", 200):
            self.source.scan(self.now, self.now - 3600)
            self.assertEqual(sum(cursor.offset for cursor in self.source._tails.values()), 200)
            self.source.scan(self.now, self.now - 3600)
            offsets = [cursor.offset for cursor in self.source._tails.values()]
            self.assertTrue(all(offset > 0 for offset in offsets))
            self.assertEqual(sum(offsets), 400)

    def test_expired_time_budget_cannot_reuse_ready_as_confirmed_after_append(self):
        self.write(self.prompt(), self.assistant())
        self.assertEqual(self.scan()["status"], "ready")
        self.write(self.prompt("next-turn", self.now))
        budget = claude_source._TranscriptBudget()
        budget.deadline = time.monotonic() - 1
        snapshot = self.source._transcript(self.path, budget=budget)
        self.assertTrue(snapshot["history_checking"])
        self.assertEqual(budget.remaining, claude_source.TRANSCRIPT_POLL_BYTES)
        self.assertEqual(self.drain()["status"], "cooking")

    def test_full_warm_cache_admits_a_new_current_tracked_chat(self):
        old_paths = []
        for identifier in ("old-a", "old-b"):
            path = self.path.with_name(identifier + ".jsonl")
            path.write_text(json.dumps({"sessionId": identifier, **self.assistant()}) + "\n")
            os.utime(path, (self.now - 100, self.now - 100))
            old_paths.append(path)
        self.source.tracked_ids = {"old-a", "old-b", "session-1"}
        with patch("claude_source.TRANSCRIPT_MAX_FILES", 2):
            self.source.scan(self.now, self.now - 3600)
            self.assertEqual(set(self.source._tails), set(old_paths))
            self.write(self.prompt(), self.assistant(timestamp=self.now))
            os.utime(self.path, (self.now, self.now))
            items, _ = self.source.scan(self.now, self.now - 3600)
            new = next(item for item in items if item["id"] == "session-1")
            self.assertFalse(new["history_checking"])
            self.assertEqual(new["status"], "ready")
            self.assertIn(self.path, self.source._tails)
            self.assertEqual(len(self.source._tails), 2)
            excluded = [item for item in items if item["id"].startswith("old-") and item["history_checking"]]
            self.assertEqual(len(excluded), 1)

    def test_sixteen_incomplete_maximum_records_cannot_starve_a_healthy_chat(self):
        for index in range(16):
            identifier = f"incomplete-{index}"
            path = self.path.with_name(identifier + ".jsonl")
            prefix = ('{"sessionId":"' + identifier + '","type":"user","message":{"content":"').encode()
            path.write_bytes(prefix + b"x" * (claude_source.TRANSCRIPT_RECORD_BYTES - len(prefix)))
            self.assertTrue(self.source._transcript(path)["history_checking"])
        self.write(self.prompt(), self.assistant())
        healthy = self.source._transcript(self.path)
        self.assertFalse(healthy["history_checking"])
        self.assertEqual(healthy["status"], "ready")
        self.assertFalse(healthy["background_history_unknown"])
        self.assertLessEqual(sum(cursor.buffer_size for cursor in self.source._tails.values()),
                             claude_source.TRANSCRIPT_MAX_BUFFER_BYTES)
        self.assertTrue(any(cursor.buffer_size == 0 for path, cursor in self.source._tails.items()
                            if path != self.path), "An incomplete resource offender must release its buffer")

    def test_continuous_appends_keep_parse_checkpoints_and_catch_up_after_quiet(self):
        identifiers = [f"growing-{index}" for index in range(5)]
        paths = []
        for identifier in identifiers:
            path = self.path.with_name(identifier + ".jsonl")
            records = [dict(self.prompt(), sessionId=identifier), dict(self.assistant(), sessionId=identifier)]
            with path.open("w") as stream:
                for event in records:
                    stream.write(json.dumps(event) + "\n")
                for _ in range(33):
                    stream.write(json.dumps({"type": "progress", "sessionId": identifier,
                                             "timestamp": self.now, "content": "x" * 65536}) + "\n")
            paths.append(path)
        self.source.tracked_ids = set(identifiers)
        with patch("claude_source.TRANSCRIPT_POLL_BYTES", 1024 * 1024):
            for _ in range(12):
                rows, _ = self.source.scan(self.now, self.now - 3600)
                self.assertTrue(all(row["history_checking"] and row["status"] != "ready" for row in rows))
                for identifier, path in zip(identifiers, paths):
                    with path.open("a") as stream:
                        stream.write(json.dumps({"type": "progress", "sessionId": identifier,
                                                 "timestamp": self.now, "content": "New disposable progress"}) + "\n")
            self.assertTrue(all(cursor.scan_restarts == 1 for cursor in self.source._tails.values()),
                            "Appending must verify and resume, not restart parsing from byte zero")
            for _ in range(100):
                rows, _ = self.source.scan(self.now, self.now - 3600)
                if len(rows) == 5 and all(not row["history_checking"] for row in rows):
                    break
            self.assertEqual({row["status"] for row in rows}, {"ready"})
            self.assertTrue(all(not row["history_checking"] for row in rows))
            self.assertTrue(all(cursor.scan_restarts == 1 for cursor in self.source._tails.values()))

    def test_record_buffer_identity_and_file_limits_preserve_uncertainty(self):
        self.background_launch()
        with patch("claude_source.TRANSCRIPT_MAX_IDENTITIES", 1):
            source = ClaudeSource(self.root)
            item = source._transcript(self.path)
        self.assertTrue(item["history_checking"])
        self.assertLessEqual(next(iter(source._tails.values())).identity_count, 1)
        with patch("claude_source.TRANSCRIPT_MAX_BUFFER_BYTES", 32):
            source = ClaudeSource(self.root)
            item = source._transcript(self.path)
        self.assertTrue(item["history_checking"])
        self.assertLessEqual(sum(cursor.buffer_size for cursor in source._tails.values()), 32)
        other = self.path.with_name("other.jsonl")
        other.write_text("{}\n")
        with patch("claude_source.TRANSCRIPT_MAX_FILES", 1):
            source = ClaudeSource(self.root)
            source._transcript(self.path)
            item = source._transcript(other)
        self.assertTrue(item["history_checking"])
        self.assertEqual(len(source._tails), 1)

    def test_complete_malformed_or_duplicate_fields_cannot_authorize_ready(self):
        for damaged in (b'{not-json}\n', b'{"type":"progress","type":"user"}\n', b'\xff\n',
                        b'null\n', b'{"type":"assistant","message":{"content":7}}\n'):
            self.path.unlink(missing_ok=True)
            self.source = ClaudeSource(self.root)
            self.write(self.prompt(), self.assistant())
            self.assertEqual(self.scan()["status"], "ready")
            with self.path.open("ab") as stream:
                stream.write(damaged)
            self.write(self.assistant(timestamp=self.now + 1))
            item = self.scan()
            self.assertEqual(item["status"], "disconnected")
            self.assertTrue(item["history_checking"])

    def test_fifo_and_substituted_symlink_cannot_block_or_certify_history(self):
        self.path.unlink(missing_ok=True)
        os.mkfifo(self.path)
        started = time.monotonic()
        item = self.source._transcript(self.path)
        self.assertTrue(item["history_checking"])
        self.assertLess(time.monotonic() - started, 1)
        self.path.unlink()
        target = self.path.with_name("target.jsonl")
        target.write_text(json.dumps({"type": "assistant", "message": {"stop_reason": "end_turn", "content": "Fixture"}}) + "\n")
        self.path.symlink_to(target)
        self.assertTrue(self.source._transcript(self.path)["history_checking"])

    def test_path_replacement_during_read_cannot_commit_the_open_old_file(self):
        self.write(self.prompt(), self.assistant())
        original_lstat = Path.lstat
        replacement = self.path.with_suffix(".replacement")
        replacement.write_text(json.dumps({"sessionId": "session-1", **self.prompt("new-turn", self.now)}) + "\n")
        replaced = False
        def rotate(path, *args, **kwargs):
            nonlocal replaced
            if path == self.path and not replaced:
                replacement.replace(self.path)
                replaced = True
            return original_lstat(path, *args, **kwargs)
        with patch.object(Path, "lstat", rotate):
            item = self.source._transcript(self.path)
        self.assertTrue(item["history_checking"])
        self.assertEqual(self.scan()["status"], "cooking")

    def test_interruption_is_not_completion_or_new_turn(self):
        self.write(self.prompt(), self.assistant("tool_use"), {"type": "user", "uuid": "interrupted",
                   "timestamp": self.now, "message": {"role": "user", "content": "[Request interrupted by user]"}})
        item = self.scan()
        self.assertEqual(item["status"], "disconnected")
        self.assertEqual(item["turn_id"], "prompt-1")
        self.assertIn("interrupted", item["detail"])

    def test_api_error_and_max_tokens_never_mark_ready(self):
        for field, value in (("isApiErrorMessage", True), ("max_tokens", None)):
            self.path.unlink(missing_ok=True)
            reply = self.assistant("max_tokens" if field == "max_tokens" else "end_turn")
            if value is not None:
                reply[field] = value
            self.write(self.prompt(), reply)
            self.assertEqual(self.scan()["status"], "disconnected")

    def test_live_waiting_registry_overrides_old_completed_reply(self):
        self.write(self.prompt(), self.assistant())
        self.registry(status="waiting", waitingFor="permission prompt")
        with patch("claude_source._process_alive", return_value=True):
            item = self.scan()
        self.assertEqual(item["status"], "needs_you")
        self.assertIn("permission", item["detail"])

    def test_stale_registry_and_missing_process_never_cook_forever(self):
        self.write(self.prompt(timestamp=self.now - 600))
        self.registry(status="busy", statusUpdatedAt=(self.now - 600) * 1000)
        with patch("claude_source._process_alive", return_value=False):
            self.assertEqual(self.scan()["status"], "disconnected")
        with patch("claude_source._process_alive", return_value=None):
            self.assertEqual(self.scan()["status"], "disconnected")

    def test_supported_cli_waiting_and_failed_state(self):
        self.write(self.prompt(), self.assistant())
        rows = {"session-1": {"sessionId": "session-1", "status": "waiting", "waitingFor": "input needed"}}
        with patch.object(self.source, "_live_sessions", return_value=(rows, True)):
            self.assertEqual(self.scan()["status"], "needs_you")
        rows["session-1"] = {"sessionId": "session-1", "state": "failed"}
        with patch.object(self.source, "_live_sessions", return_value=(rows, True)):
            self.assertEqual(self.scan()["status"], "disconnected")

    def test_supported_cli_busy_and_absent_process_are_distinct(self):
        self.write(self.prompt())
        self.registry(status="busy")
        rows = {"session-1": {"sessionId": "session-1", "status": "busy"}}
        with patch.object(self.source, "_live_sessions", return_value=(rows, True)):
            self.assertEqual(self.scan()["status"], "cooking")
        with patch.object(self.source, "_live_sessions", return_value=({}, True)):
            with patch("claude_source._process_alive", return_value=True):
                self.assertEqual(self.scan()["status"], "disconnected")

    def test_old_live_wait_is_confirmed_and_current_turn_start_wins(self):
        old = self.now - 72 * 3600
        self.write(self.prompt(timestamp=old + 30))
        self.registry(status="waiting", statusUpdatedAt=(old + 60) * 1000, startedAt=(old - 300) * 1000)
        rows = {"session-1": {"sessionId": "session-1", "pid": 123, "status": "waiting",
                               "waitingFor": "permission prompt", "startedAt": (old - 300) * 1000}}
        with patch.object(self.source, "_live_sessions", return_value=(rows, True)):
            items, source = self.source.scan(self.now, self.now - 24 * 3600)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["confirmed_live"])
        self.assertEqual(items[0]["status"], "needs_you")
        self.assertLess(items[0]["updated_at"], self.now - 24 * 3600)
        self.assertEqual(items[0]["started_at"], old + 30)
        self.assertIn("CLI", source["detail"])

    def test_retained_done_cli_row_is_not_confirmed_live(self):
        rows = {"session-1": {"sessionId": "session-1", "state": "done",
                               "startedAt": (self.now - 72 * 3600) * 1000}}
        with patch.object(self.source, "_live_sessions", return_value=(rows, True)):
            items, _ = self.source.scan(self.now, self.now - 24 * 3600)
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["confirmed_live"])
        self.assertEqual(items[0]["status"], "ready")

    def test_subagent_and_meta_records_do_not_complete_main_turn(self):
        self.write(self.prompt(), {"type": "user", "isMeta": True, "timestamp": self.now,
                                   "message": {"role": "user", "content": "internal reminder"}})
        nested = self.path.parent / "session-1" / "subagents" / "agent-1.jsonl"
        nested.parent.mkdir(parents=True)
        nested.write_text(json.dumps(self.assistant()) + "\n")
        self.write({**self.assistant(), "isSidechain": True})
        items, _ = self.source.scan(self.now, self.now - 3600)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "cooking")
        self.assertEqual(items[0]["turn_id"], "prompt-1")

    def test_names_are_bounded_and_only_explicit_reply_preview_is_exported(self):
        self.write(self.prompt(), self.assistant(), {"type": "custom-title", "customTitle": "A" * 400})
        item = self.scan()
        self.assertEqual(len(item["title"]), 160)
        self.assertEqual(item["preview"], "Disposable test reply")
        self.assertNotIn("Disposable test request", json.dumps(item))
        self.assertFalse(item["can_open"])

    def test_desktop_title_maps_cli_session_and_rename_does_not_reopen_turn(self):
        self.write(self.prompt(), self.assistant())
        self.registry(name="Remote Control session (from a project)", status="idle")
        desktop = self.root / "desktop"
        path = desktop / "account" / "org" / "local_123.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"sessionId": "local_123", "cliSessionId": "session-1",
                                    "title": "Build the TV dashboard", "snapshot": "x" * 50000}))
        self.source.desktop_root = desktop
        before = self.scan()
        self.assertEqual(before["title"], "Build the TV dashboard")
        path.write_text(json.dumps({"cliSessionId": "session-1", "title": "Renamed TV dashboard"}))
        after = self.scan()
        self.assertEqual(after["title"], "Renamed TV dashboard")
        self.assertEqual(before["event_id"], after["event_id"])

    def test_exact_desktop_url_uses_mapped_local_id_and_never_app_fallback(self):
        self.write(self.prompt(), self.assistant())
        desktop = self.root / "desktop"
        path = desktop / "account" / "org" / "local_123.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"sessionId": "local_123", "cliSessionId": "session-1",
                                    "title": "TV dashboard"}))
        self.source.desktop_root = desktop
        self.source.app_available = True
        item = self.scan()
        self.assertTrue(item["can_open"])
        self.assertEqual(item["open_kind"], "url")
        self.assertEqual(item["open_url"], "claude://code/continue?session=local_123")
        path.write_text(json.dumps({"sessionId": "local_123&session=last", "cliSessionId": "session-1",
                                    "title": "TV dashboard"}))
        self.assertFalse(self.scan()["can_open"])
        path.unlink()
        self.assertFalse(self.scan()["can_open"])

    def test_default_slug_uses_first_meaningful_user_caption(self):
        request = self.prompt()
        request["message"]["content"] = """# AGENTS.md instructions
<INSTRUCTIONS>Use direct language. Do not publish.</INSTRUCTIONS>
<environment_context><cwd>/private/work</cwd><shell>bash</shell></environment_context>
Build a dashboard showing aircraft near the TV.
"""
        self.write(request, self.assistant())
        self.registry(name="project-c7", status="idle")
        item = self.scan()
        self.assertEqual(item["title"], "Build a dashboard showing aircraft near the TV.")
        self.assertEqual(item["title_source"], "prompt")
        self.assertLessEqual(len(_prompt_caption("Build " + "an aircraft dashboard " * 30)), 101)

    def test_remote_coordinator_task_caption_does_not_start_user_turn(self):
        event = self.prompt()
        event.update(isMeta=True, promptSource="system", turnOrigin="system")
        event["message"]["content"] = (
            "The note below was written by the coordinator session, not by your user.\n"
            "<relay sender=\"coordinator\">\n<note>Project: What's Up for Fire TV plane radar.\n"
            "Review the existing implementation.</note>\n</relay>")
        self.write(event, self.assistant())
        self.registry(name="Remote Control session (from a project)", status="idle")
        item = self.scan()
        self.assertEqual(item["title"], "What's Up for Fire TV plane radar.")
        self.assertEqual(item["title_source"], "task")
        self.assertIsNone(item["turn_id"])

    def test_relay_job_caption_skips_machine_and_folder_instructions(self):
        event = self.prompt()
        event.update(isMeta=True, promptSource="system", turnOrigin="system")
        event["message"]["content"] = (
            "The note below was written by the coordinator session, not by your user.\n"
            "<relay sender=\"coordinator\"><note>You are running on a Mac in the only allowed folder.\n"
            "Do everything from that folder.\nJob: put the validated dashboard changes into a draft PR.\n"
            "Report exactly what changed.</note></relay>")
        self.write(event, self.assistant())
        self.registry(name="project-c7", status="idle")
        self.assertEqual(self.scan()["title"], "Put the validated dashboard changes into a draft PR.")

    def test_unnamed_session_has_date_and_time_instead_of_identical_project_name(self):
        self.write(self.prompt(), self.assistant())
        self.registry(name="project-c7", status="idle", startedAt=(self.now - 1000) * 1000)
        item = self.scan()
        self.assertTrue(item["title"].startswith("Project · "))
        self.assertEqual(item["title_source"], "project_time")
        self.assertRegex(item["title"], r"\d{1,2}:\d{2} [AP]M")

    def test_fallback_caption_uses_stable_desktop_creation_across_turns_and_scans(self):
        self.write(self.prompt(), self.assistant())
        self.registry(name="project-c7", status="idle", startedAt=(self.now - 1000) * 1000)
        desktop = self.root / "desktop"
        path = desktop / "account" / "org" / "local_123.json"
        path.parent.mkdir(parents=True)
        created = self.now - 7200
        path.write_text(json.dumps({"sessionId": "local_123", "cliSessionId": "session-1",
                                    "createdAt": created * 1000,
                                    "title": "Remote Control session (from a project)"}))
        self.source.desktop_root = desktop
        before = self.scan()
        self.assertTrue(before["title"].endswith(datetime.fromtimestamp(created).strftime("%b %-d, %-I:%M %p")))
        self.write(self.prompt("new-turn", self.now))
        self.registry(name="project-c7", status="busy", startedAt=self.now * 1000)
        # Recreating the source and a new process/turn must preserve its caption.
        self.source = ClaudeSource(self.root)
        self.source.desktop_root = desktop
        after = self.scan()
        self.assertEqual(before["title"], after["title"])
        self.assertNotEqual(before["event_id"], after["event_id"])

    def test_old_tracked_row_refreshes_caption_without_importing_other_history(self):
        old = self.now - 72 * 3600
        request = self.prompt(timestamp=old)
        request["message"]["content"] = "Update the historic Bureau documentation and recovery notes."
        self.write(request, self.assistant(timestamp=old + 20))
        os.utime(self.path, (old + 20, old + 20))
        untracked, _ = self.source.scan(self.now, self.now - 86400)
        self.assertEqual(untracked, [])
        self.source.tracked_ids = {"session-1"}
        tracked, _ = self.source.scan(self.now, self.now - 86400)
        self.assertEqual(len(tracked), 1)
        item = tracked[0]
        self.assertEqual(item["id"], "session-1")
        self.assertEqual(item["title"], "Update the historic Bureau documentation and recovery notes.")
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["updated_at"], old + 20)
        self.assertFalse(item["confirmed_live"])
        self.assertFalse(item["metadata_only"])

    def test_tracked_archived_desktop_metadata_has_no_active_open_link(self):
        desktop = self.root / "desktop"
        path = desktop / "account" / "org" / "local_123.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"sessionId": "local_123", "cliSessionId": "session-1",
                                    "cwd": "/work/project", "createdAt": (self.now - 72 * 3600) * 1000,
                                    "isArchived": True, "title": "Historic Bureau documentation"}))
        self.source.desktop_root = desktop
        self.source.app_available = True
        untracked, _ = self.source.scan(self.now, self.now - 86400)
        self.assertEqual(untracked, [])
        self.source.tracked_ids = {"session-1"}
        tracked, _ = self.source.scan(self.now, self.now - 86400)
        self.assertEqual(len(tracked), 1)
        self.assertEqual(tracked[0]["title"], "Historic Bureau documentation")
        self.assertEqual(tracked[0]["cwd"], "/work/project")
        self.assertEqual(tracked[0]["updated_at"], 0)
        self.assertTrue(tracked[0]["metadata_only"])
        self.assertFalse(tracked[0]["can_open"])
        self.assertEqual(tracked[0]["open_url"], "")

    def test_live_cli_observation_is_not_metadata_only_without_transcript(self):
        rows = {"session-1": {"sessionId": "session-1", "pid": 123,
                               "status": "waiting", "waitingFor": "permission prompt"}}
        with patch.object(self.source, "_live_sessions", return_value=(rows, True)):
            items, _ = self.source.scan(self.now, self.now - 86400)
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["metadata_only"])
        self.assertTrue(items[0]["confirmed_live"])
        self.assertEqual(items[0]["status"], "needs_you")

    def test_live_project_worker_returns_background_lifecycle_without_task_caption(self):
        request = self.prompt()
        request["message"]["content"] = "Build an internal coordinator task that is not the user's root chat."
        self.write(request)
        self.desktop_metadata(remoteControlSpawn={"projectThreadChild": True, "rcChild": True})
        rows = {"session-1": {"sessionId": "session-1", "pid": 123, "status": "busy"}}
        with patch.object(self.source, "_live_sessions", return_value=(rows, True)):
            with patch.object(self.source, "_transcript", wraps=self.source._transcript) as transcript:
                items, _ = self.source.scan(self.now, self.now - 86400)
        transcript.assert_called_once_with(self.path, captions=False, budget=ANY)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["background_reason"], "internal_project_worker")
        self.assertEqual(items[0]["status"], "cooking")
        self.assertTrue(items[0]["can_open"])
        self.assertNotIn("coordinator task", items[0]["title"])
        self.assertEqual(set(self.source.background_reasons), {"session-1"})
        self.assertEqual(self.source.background_reasons["session-1"], "internal_project_worker")

    def test_old_tracked_worker_returns_background_but_untracked_old_history_does_not_import(self):
        old = self.now - 72 * 3600
        self.write(self.prompt(timestamp=old), self.assistant(timestamp=old + 20))
        self.source.tracked_ids = {"session-1"}
        self.desktop_metadata(remoteControlSpawn={"projectThreadChild": True, "rcChild": True})
        items, _ = self.source.scan(self.now, self.now - 86400)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["background_reason"], "internal_project_worker")
        self.assertEqual(items[0]["updated_at"], old + 20)
        self.assertFalse(items[0]["confirmed_live"])
        self.assertIn("session-1", self.source.background_reasons)
        self.source.tracked_ids = set()
        os.utime(self.path, (old, old))
        items, _ = self.source.scan(self.now, self.now - 86400)
        self.assertEqual(items, [])

    def test_generic_named_real_desktop_chat_with_remote_control_remains_openable(self):
        request = self.prompt()
        request["message"]["content"] = "Review the ordinary desktop project's release changes."
        self.write(request, self.assistant())
        self.desktop_metadata(remoteControlSpawn={"rcChild": False})
        item = self.scan()
        self.assertEqual(item["title_source"], "prompt")
        self.assertTrue(item["can_open"])
        self.assertEqual(item["open_url"], "claude://code/continue?session=local_123")
        self.assertEqual(self.source.background_reasons, {})
        self.assertEqual(self.source.classifications["session-1"], "desktop_code_session")

    def test_unmapped_live_cli_and_archived_desktop_are_classified_as_background(self):
        self.write(self.prompt(), self.assistant())
        path = self.desktop_metadata(isArchived=True)
        self.source.tracked_ids = {"session-1"}
        items, _ = self.source.scan(self.now, self.now - 86400)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["background_reason"], "archived_desktop_session")
        self.assertFalse(items[0]["can_open"])
        self.assertEqual(self.source.background_reasons["session-1"], "archived_desktop_session")
        path.unlink()
        rows = {"session-1": {"sessionId": "session-1", "pid": 123, "status": "busy"}}
        with patch.object(self.source, "_live_sessions", return_value=(rows, True)):
            items, _ = self.source.scan(self.now, self.now - 86400)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["background_reason"], "not_a_desktop_code_session")
        self.assertFalse(items[0]["can_open"])
        self.assertEqual(self.source.background_reasons["session-1"], "not_a_desktop_code_session")

    def test_missing_or_corrupt_metadata_index_cannot_reclassify_an_unmapped_session(self):
        self.write(self.prompt(), self.assistant())
        self.source.tracked_ids = {"session-1"}
        self.source.desktop_only = True
        self.source.desktop_root = self.root / "missing"
        items, _ = self.source.scan(self.now, self.now - 86400)
        self.assertEqual(items, [])
        self.assertEqual(self.source.background_reasons, {})
        path = self.desktop_metadata()
        path.write_text('{"sessionId":')
        items, _ = self.source.scan(self.now, self.now - 86400)
        self.assertEqual(items, [])
        self.assertEqual(self.source.background_reasons, {})
        self.assertFalse(self.source._desktop_index_complete)

    def test_older_tracked_desktop_mapping_is_not_lost_after_128_newer_headers(self):
        old = self.now - 72 * 3600
        self.write(self.prompt(timestamp=old), self.assistant(timestamp=old + 20))
        os.utime(self.path, (old, old))
        path = self.desktop_metadata(title="Historic root desktop chat")
        os.utime(path, (old, old))
        for index in range(130):
            extra = path.parent / f"local_new-{index}.json"
            extra.write_text(json.dumps({"sessionId": f"local_new-{index}",
                                         "cliSessionId": f"new-{index}", "title": "Other desktop chat"}))
        self.source.tracked_ids = {"session-1"}
        items, _ = self.source.scan(self.now, self.now - 86400)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Historic root desktop chat")
        self.assertTrue(items[0]["can_open"])
        self.assertNotIn("session-1", self.source.background_reasons)

    def test_nested_worker_flags_do_not_classify_an_ordinary_desktop_chat(self):
        self.write(self.prompt(), self.assistant())
        self.desktop_metadata(title="Ordinary desktop chat", toolSurfaceSnapshot={
            "remoteControlSpawn": {"projectThreadChild": True, "rcChild": True}})
        self.assertEqual(self.scan()["title"], "Ordinary desktop chat")
        self.assertEqual(self.source.background_reasons, {})

    def test_changed_metadata_can_return_a_background_session_to_the_main_queue(self):
        self.write(self.prompt(), self.assistant())
        self.desktop_metadata(remoteControlSpawn={"projectThreadChild": True, "rcChild": True})
        self.source.scan(self.now, self.now - 86400)
        self.assertIn("session-1", self.source.background_reasons)
        self.desktop_metadata(title="Ordinary restored desktop chat", remoteControlSpawn={"rcChild": False})
        items, _ = self.source.scan(self.now, self.now - 86400)
        self.assertEqual(len(items), 1)
        self.assertEqual(self.source.background_reasons, {})

    def test_new_unmapped_cli_helper_without_lifecycle_does_not_import(self):
        self.source.desktop_root = self.root / "desktop"
        self.source.desktop_root.mkdir()
        self.source.desktop_only = True
        rows = {"empty-helper": {"sessionId": "empty-helper", "pid": 123,
                                  "cwd": "/Users/test", "status": "idle", "startedAt": self.now * 1000}}
        with patch.object(self.source, "_live_sessions", return_value=(rows, True)):
            items, _ = self.source.scan(self.now, self.now - 86400)
        self.assertEqual(items, [])
        self.assertNotIn("empty-helper", self.source.background_reasons)
        self.assertEqual(self.source.classifications["empty-helper"], "empty_cli_helper")
        # User-visible records imported by an older version stay accessible.
        self.source.tracked_ids = {"empty-helper"}
        with patch.object(self.source, "_live_sessions", return_value=(rows, True)):
            items, _ = self.source.scan(self.now, self.now - 86400)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["background_reason"], "not_a_desktop_code_session")

    def test_desktop_nested_or_late_titles_do_not_replace_safe_fallback(self):
        self.write(self.prompt(), self.assistant())
        self.registry(name="Known CLI title", status="idle")
        desktop = self.root / "desktop"
        path = desktop / "account" / "org" / "local_123.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"cliSessionId": "session-1", "snapshot": {
            "title": "Nested unrelated title", "large": "x" * 50000}, "title": "Beyond bounded header"}))
        self.source.desktop_root = desktop
        self.assertEqual(self.scan()["title"], "Known CLI title")
        path.write_text(json.dumps({"cliSessionId": "session-1",
                                    "title": "Remote Control session (from a project)"}))
        self.assertEqual(self.scan()["title"], "Known CLI title")

    def test_partial_write_does_not_replace_completed_state(self):
        self.write(self.prompt(), self.assistant())
        before = self.scan()
        with self.path.open("a") as stream:
            stream.write('{"type":"user","message":')
        after = self.scan()
        self.assertEqual(after["status"], "disconnected")
        self.assertTrue(after["history_checking"])
        confirmed = self.source._transcript(self.path)
        self.assertEqual(confirmed["status"], "ready")
        self.assertEqual(confirmed["event_id"], self.assistant()["uuid"])

    def test_permission_error_is_unknown_not_dead(self):
        with patch("claude_source.os.kill", side_effect=PermissionError):
            self.assertIsNone(_process_alive(123))

    def test_final_reply_preview_is_literal_bounded_and_resets_on_new_turn(self):
        final = self.assistant()
        final["message"]["content"] = [{"type": "text", "text": "Original reply.\n" + "x" * 800}]
        self.write(self.prompt(), final)
        item = self.scan()
        self.assertEqual(item["preview"], final["message"]["content"][0]["text"][:500])
        self.assertEqual(item["preview_kind"], "reply")
        self.assertEqual(item["preview_at"], self.now - 10)
        self.write(self.prompt("new-turn", self.now))
        self.assertNotIn("preview", self.scan())

    def test_explicit_question_preview_does_not_export_tool_results(self):
        question = self.assistant("tool_use")
        question["message"]["content"] = [{"type": "tool_use", "name": "AskUserQuestion",
            "input": {"questions": [{"question": "Which layout should I use?", "options": [{"label": "A"}]}]}}]
        self.write(self.prompt(), question)
        item = self.scan()
        self.assertEqual(item["preview"], "Which layout should I use?")
        self.assertEqual(item["preview_kind"], "permission")
        self.write({"type": "user", "uuid": "result", "timestamp": self.now,
                    "message": {"content": [{"type": "tool_result", "content": "A sensitive tool result"}]}})
        self.assertNotIn("preview", self.scan())

    def test_waiting_does_not_relabel_an_old_reply_as_permission(self):
        self.write(self.prompt(), self.assistant())
        self.registry(status="waiting")
        with patch("claude_source._process_alive", return_value=True):
            self.assertNotIn("preview", self.scan())
        self.registry(status="waiting", waitingFor="Approve access to the working folder")
        with patch("claude_source._process_alive", return_value=True):
            item = self.scan()
        self.assertEqual(item["preview"], "Approve access to the working folder")
        self.assertEqual(item["preview_kind"], "permission")

    def test_internal_envelope_is_not_a_handoff_preview(self):
        final = self.assistant()
        final["message"]["content"] = [{"type": "text", "text": "<relay><note>Internal scaffolding</note></relay>"}]
        self.write(self.prompt(), final)
        self.assertNotIn("preview", self.scan())


if __name__ == "__main__":
    unittest.main()
