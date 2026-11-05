"""Adversarial adapters use disposable data, fake CLIs and no provider account."""
import base64
import json
import math
import os
from pathlib import Path
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from claude_source import ClaudeSource, _prompt_caption, _task_caption, _timestamp as claude_timestamp
from codex_source import CodexSource, _FileState, _timestamp as codex_timestamp
from claude_project_source import _collect, _session_token, ProjectUnavailable
from process_reader import OutputLimitExceeded, read_process
from usage_source import _claude_token, parse_claude_usage, parse_codex_usage, UsageUnavailable

NOW = 1_800_000_000.0


class AdapterRedteamTests(unittest.TestCase):
    def test_500_codex_chats_with_corrupt_interleaved_events(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = root / "sessions"
            paths.mkdir()
            database = root / "state_5.sqlite"
            expected = {}
            with sqlite3.connect(database) as conn:
                conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER, title TEXT, cwd TEXT)")
                for index in range(500):
                    ident = f"synthetic-{index}"
                    path = paths / (ident + ".jsonl")
                    records = [{"type": "session_meta", "payload": {"id": ident}},
                               {"type": "event_msg", "timestamp": NOW - 5,
                                "payload": {"type": "task_started", "turn_id": "current"}},
                               {"type": "response_item", "timestamp": math.nan,
                                "payload": {"type": "message", "role": "assistant", "phase": "final", "content": index}},
                               {"type": "response_item", "timestamp": NOW - 4,
                                "payload": {"type": "function_call", "name": ["request_user_input"]}},
                               {"type": "event_msg", "timestamp": NOW - 3,
                                "payload": {"type": "task_complete", "turn_id": "old"}}]
                    status, kind = (("ready", "task_complete"), ("needs_you", "approval_requested"),
                                    ("cooking", "task_started"), ("disconnected", "turn_failed"))[index % 4]
                    records.append({"type": "event_msg", "timestamp": NOW - 1,
                                    "payload": {"type": kind, "turn_id": "current", "status": {"malformed": True}}})
                    path.write_bytes(b"\n".join(json.dumps(row).encode() for row in records)
                                     + b"\n{bad json}\n" + b"[" * 1500 + b"0" + b"]" * 1500 + b"\n")
                    expected[ident] = status
                    conn.execute("INSERT INTO threads VALUES (?,?,?,?,?)", (ident, str(path), NOW, "<script>fixture</script>" + "x" * 300, "/fixture/" + "x" * 3000))
            before = database.read_bytes()
            rows, health = CodexSource(root).scan(NOW, NOW - 60)
            self.assertTrue(health["available"])
            self.assertEqual({row["id"] for row in rows}, set(expected))
            self.assertTrue(all(row["status"] == "disconnected" and row["history_checking"] for row in rows),
                            "Corrupt or incomplete rollout history cannot certify a current lifecycle")
            self.assertTrue(all(len(row["title"]) <= 160 and len(row["cwd"]) <= 2048 for row in rows))
            json.dumps(rows, allow_nan=False)
            self.assertEqual(database.read_bytes(), before)

    def test_500_claude_chats_with_corrupt_interleaved_events(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "projects/fixture"
            project.mkdir(parents=True)
            expected = {}
            for index in range(500):
                ident = f"fixture-{index}"
                path = project / (ident + ".jsonl")
                stop = ("end_turn", "tool_use", "tool_use", "max_tokens")[index % 4]
                content = ([{"type": "tool_use", "name": "AskUserQuestion", "input": {"questions": [{"question": "Which fixture?"}]}}]
                           if index % 4 == 1 else "A fixture-only reply.")
                records = [{"type": "user", "sessionId": ident, "timestamp": NOW - 5, "message": {"content": "Test a disposable fixture workflow"}},
                           {"type": "assistant", "timestamp": math.inf, "message": 7},
                           {"type": "assistant", "sessionId": ident, "timestamp": NOW - 1,
                            "message": {"stop_reason": stop, "content": content}}]
                path.write_bytes(b"\n".join(json.dumps(row).encode() for row in records)
                                 + b"\n" + b"[" * 1500 + b"0" + b"]" * 1500 + b"\n{partial")
                os.utime(path, (NOW, NOW))
                expected[ident] = ("ready", "needs_you", "cooking", "disconnected")[index % 4]
            with patch("claude_source._find_claude", side_effect=AssertionError("No installed CLI may be used")):
                source = ClaudeSource(root)
                source.tracked_ids = set(expected)
                rows, health = source.scan(NOW, NOW - 60)
            self.assertTrue(health["available"])
            # Corrupt complete records and a trailing partial write leave the
            # lifecycle unverified; they cannot authorize any final Ready.
            self.assertEqual({row["id"] for row in rows}, set(expected))
            self.assertTrue(all(row["status"] == "disconnected" and row["history_checking"] for row in rows))
            self.assertTrue(all(not row["can_open"] for row in rows))
            json.dumps(rows, allow_nan=False)

    def test_1000_mutated_codex_events_do_not_raise_or_export_nonfinite_times(self):
        randomizer = random.Random(7331)
        values = [None, True, False, 0, [], {}, [{}], {"x": 1}, "x", math.nan, math.inf, 10**1000]
        for _ in range(1000):
            state = _FileState()
            state.consume(json.dumps({"type": "event_msg", "timestamp": NOW - 10,
                                      "payload": {"type": "task_started", "turn_id": "current"}}).encode())
            event = {"type": randomizer.choice(["event_msg", "response_item"]), "timestamp": randomizer.choice(values),
                     "payload": {"type": randomizer.choice(["task_complete", "message", "function_call", "request_user_input"]),
                                 "role": "assistant", "phase": "final", "name": "request_user_input", "content": []}}
            event["payload"][randomizer.choice(["status", "content", "name", "arguments", "completed_at", "turn_id"])] = randomizer.choice(values)
            state.consume(json.dumps(event).encode())
            self.assertTrue(math.isfinite(state.transition_at))
            json.dumps({"at": state.transition_at, "preview_at": state.preview_at}, allow_nan=False)

    def test_project_schema_mutations_keep_authority_and_identity_separate(self):
        class FakeClient:
            def __init__(self, mutation): self.mutation = mutation
            def get(self, path):
                if path == "/v1/code/channels?scope=all":
                    return {"data": [{"id": "chan_fixture123", "name": "Fixture", "channel_session_id": "cse_fixture123"}]}
                if "/sessions/" in path:
                    return {"id": "cse_fixture123", "status_bucket": "blocked", "last_event_at": NOW - 5}
                if "/timeline?" in path:
                    return {"data": [{"id": "cmsg_fixture123", "has_thread": True, "thread_status": "needs_input",
                                      "last_reply_at": NOW - 1, **self.mutation}]}
                return {"data": []}
        values = [None, True, False, 0, [], {}, [{}], {"id": []}, "x", math.nan, math.inf]
        for field in ("bound_sessions", "thread_status", "last_reply_at", "reply_count", "body", "thread_preview", "id"):
            for value in values:
                rows, _ = _collect(NOW, NOW - 60, set(), {}, FakeClient({field: value}))
                json.dumps(rows, allow_nan=False)
                for row in rows:
                    self.assertTrue(row["open_url"].startswith("claude://code/project/chan_fixture123"))

    def test_nonfinite_or_overflow_times_and_usage_are_rejected(self):
        for value in (math.nan, math.inf, -math.inf, 10**1000):
            self.assertEqual(claude_timestamp(value), 0)
            self.assertEqual(codex_timestamp(value), 0)
            with self.assertRaises(UsageUnavailable):
                parse_claude_usage({"five_hour": {"utilization": value, "resets_at": NOW}})
        for value in (None, [], True, "secret fixture response"):
            for parser in (parse_claude_usage, parse_codex_usage):
                with self.assertRaises(UsageUnavailable): parser(value)

    def test_index_rollout_identity_mismatch_cannot_finish_another_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "wrong.jsonl"
            path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "wrong-chat"}}) + "\n"
                            + json.dumps({"type": "event_msg", "timestamp": NOW, "payload": {"type": "task_complete"}}) + "\n")
            with sqlite3.connect(root / "state_5.sqlite") as conn:
                conn.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, updated_at INTEGER)")
                conn.execute("INSERT INTO threads VALUES (?,?,?)", ("expected-chat", str(path), NOW))
            rows, _ = CodexSource(root).scan(NOW, NOW - 60)
            self.assertEqual(rows[0]["status"], "disconnected")
            self.assertIn("identity", rows[0]["detail"])

    def test_malformed_database_paths_and_nonregular_logs_do_not_block_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fifo = root / "never-open.jsonl"
            os.mkfifo(fifo)
            with sqlite3.connect(root / "state_5.sqlite") as conn:
                conn.execute("CREATE TABLE threads (id TEXT, rollout_path, updated_at INTEGER)")
                for ident, path in (("integer-path", 100), ("null-path", None), ("nul-path", "bad\0path"), ("fifo", str(fifo))):
                    conn.execute("INSERT INTO threads VALUES (?,?,?)", (ident, path, NOW))
            started = time.monotonic()
            rows, _ = CodexSource(root).scan(NOW, NOW - 60)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual({row["id"] for row in rows}, {"nul-path", "fifo"})
            self.assertTrue(all(row["status"] == "disconnected" for row in rows))

    def test_repeated_unclosed_context_tags_do_not_stall_or_expose_system_text(self):
        started = time.monotonic()
        self.assertEqual(_prompt_caption("<INSTRUCTIONS>" * 35000 + "Internal fixture instructions must remain hidden"), "")
        self.assertEqual(_task_caption({"type": "user", "isMeta": True,
                         "message": {"content": "coordinator session <relay>" + "<note>" * 35000}}), ("", ""))
        _prompt_caption("<INSTRUCTIONS" * 35000)
        _prompt_caption("```fixture" * 35000)
        self.assertEqual(_task_caption({"type": "user", "isMeta": True,
                         "message": {"content": "coordinator session <relay>" + "<note" * 35000}}), ("", ""))
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(_prompt_caption("<system>Private fixture</system>\nBuild a simple local dashboard for agents"),
                         "Build a simple local dashboard for agents")

    def test_account_tokens_reject_header_controls_nonfinite_expiry_and_wrong_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": "active", "oauth:tokenCacheV2": base64.b64encode(b"fixture").decode()}))
            for module, function, scope, exception in (("usage_source", _claude_token, "user:profile", UsageUnavailable),
                                                       ("claude_project_source", _session_token, "user:sessions:claude_code", ProjectUnavailable)):
                for token, expiry in (("fixture\r\nX-Secret: escaped", NOW * 1000 + 10000), ("fixture", math.inf), ("", NOW * 1000 + 10000)):
                    cache = {"acct:active|client:organization:https://api.anthropic.com:" + scope: {"token": token, "expiresAt": expiry}}
                    with patch(module + "._keychain_password", return_value=b"fixture"), patch(module + "._active_claude_org", return_value="organization"), patch(module + "._decrypt_chromium", return_value=json.dumps(cache).encode()):
                        with self.assertRaises(exception): function(root, NOW)


class ProcessReaderRedteamTests(unittest.TestCase):
    def test_output_is_bounded_while_the_child_is_running(self):
        started = time.monotonic()
        with self.assertRaises(OutputLimitExceeded):
            read_process([sys.executable, "-c", "import os,time; os.write(1,b'x'*65536); time.sleep(60)"], timeout=3, max_output=2048)
        self.assertLess(time.monotonic() - started, 2)

    def test_child_cannot_hold_stdin_write_past_timeout(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            read_process([sys.executable, "-c", "import time; time.sleep(60)"], input=b"x" * (4 * 1024 * 1024), timeout=0.2, max_output=2048)
        self.assertLess(time.monotonic() - started, 2)

    def test_timeout_discards_secret_stderr(self):
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            read_process([sys.executable, "-c", "import os,time; os.write(2,b'Bearer secret-fixture'); time.sleep(60)"], timeout=0.2, max_output=2048)
        self.assertIsNone(caught.exception.output)
        self.assertIsNone(caught.exception.stderr)


if __name__ == "__main__":
    unittest.main()
