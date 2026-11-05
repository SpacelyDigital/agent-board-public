"""Large-record tests use disposable transcripts and never provider accounts."""

import hashlib
import http.client
import json
import os
from pathlib import Path
import random
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest.mock import patch

import claude_source
from claude_source import ClaudeSource, _ProjectedJSONRecord, _TranscriptConsumer


class LargeClaudeRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "projects/fixture/session-large.jsonl"
        self.path.parent.mkdir(parents=True)
        self.now = time.time() + 10
        self.source = ClaudeSource(self.root)
        self.source.tracked_ids = {"session-large"}

    def event(self, kind, *, content="Fixture-only content", **fields):
        return dict(sessionId="session-large", type=kind, timestamp=self.now,
                    message={"content": content}, **fields)

    def prompt(self, identifier="prompt-1", **fields):
        return self.event("user", uuid=identifier, **fields)

    def reply(self, identifier="reply-1", **fields):
        event = self.event("assistant", uuid=identifier, **fields)
        event["message"]["stop_reason"] = "end_turn"
        return event

    def append(self, *events):
        with self.path.open("ab") as stream:
            for event in events:
                stream.write(json.dumps(event, ensure_ascii=False).encode() + b"\n")

    def drain(self, source=None, limit=400):
        source = source or self.source
        for _ in range(limit):
            observations, _ = source.scan(self.now + 1, self.now - 3600)
            self.assertLessEqual(sum(cursor.buffer_size for cursor in source._tails.values()),
                                 claude_source.TRANSCRIPT_MAX_BUFFER_BYTES)
            if observations and all(cursor.phase == "idle" for cursor in source._tails.values()):
                return observations[0]
        self.fail("The incremental fixture index did not finish within its bounded polls")

    def test_large_embedded_image_tool_output_and_final_text_complete_warm_and_cold(self):
        image = {"type": "image", "source": {"type": "base64", "data": "A" * (8 * 1024 * 1024)}}
        self.append(self.prompt(content=[{"type": "text", "text": "Inspect this disposable image fixture"}, image]),
                    self.event("user", uuid="tool-output", content=[
                        {"type": "tool_result", "tool_use_id": "tool-1", "content": "PRIVATE-TOOL" * 1200000}]),
                    self.reply(content=[{"type": "text", "text": "Finished the fixture. 🟢\n" + "終" * 700000}]))
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch("claude_source.TRANSCRIPT_POLL_BYTES", 256 * 1024):
            first = self.source.scan(self.now + 1, self.now - 3600)[0][0]
            self.assertTrue(first["history_checking"])
            self.assertNotEqual(first["status"], "ready")
            warm = self.drain()
            cold = self.drain(ClaudeSource(self.root))
        self.assertEqual(warm["status"], "ready")
        self.assertTrue(warm["history_verified"])
        self.assertTrue(warm["preview"].startswith("Finished the fixture. 🟢"))
        self.assertEqual(len(warm["preview"]), 500)
        self.assertNotIn("PRIVATE-TOOL", json.dumps(warm))
        self.assertEqual(cold["event_id"], warm["event_id"])
        self.assertEqual(cold["turn_id"], "prompt-1")
        self.assertEqual(warm["history_revision"]["sha256"], before)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_partial_large_record_split_inside_utf8_waits_for_append_and_newline(self):
        self.append(self.prompt(), self.reply())
        self.assertEqual(self.drain()["status"], "ready")
        new = self.prompt("prompt-2", content=[{"type": "image", "source": {"data": "A" * 2000000}},
                                                {"type": "text", "text": "A new fixture with 🟢 content"}])
        encoded = json.dumps(new, ensure_ascii=False).encode()
        split = encoded.index("🟢".encode()) + 2
        with self.path.open("ab") as stream:
            stream.write(encoded[:split])
        for _ in range(8):
            item = self.source.scan(self.now + 1, self.now - 3600)[0][0]
            self.assertTrue(item["history_checking"])
            self.assertNotEqual(item["status"], "ready")
        with self.path.open("ab") as stream:
            stream.write(encoded[split:])
        self.assertTrue(self.source.scan(self.now + 1, self.now - 3600)[0][0]["history_checking"])
        with self.path.open("ab") as stream:
            stream.write(b"\n")
        self.assertEqual(self.drain()["status"], "cooking")
        self.append(self.reply("reply-2"))
        self.assertEqual(self.drain()["status"], "ready")

    def test_large_background_fields_after_payload_keep_work_pending_until_matching_notification(self):
        request = self.event("assistant", content=[{"type": "tool_use", "name": "Bash", "id": "tool-bg",
                                                    "input": {"command": "x" * 2000000, "run_in_background": True}}])
        request["message"]["stop_reason"] = "tool_use"
        result = self.event("user", content=[{"type": "tool_result", "tool_use_id": "tool-bg",
                                             "content": "PRIVATE" * 400000}],
                            toolUseResult={"output": "PRIVATE" * 400000, "backgroundTaskId": "task-bg"})
        self.append(self.prompt(), request, result, self.reply())
        waiting = self.drain()
        self.assertEqual(waiting["status"], "cooking")
        self.assertTrue(self.source._transcript(self.path)["background_pending"])
        self.assertFalse(waiting["history_checking"])
        self.append(self.event("queue-operation", operation="enqueue",
                               content="<task-notification><task-id>task-bg</task-id><status>completed</status></task-notification>"),
                    self.reply("reply-after-background"))
        # queue-operation uses a top-level content field in the real schema.
        rows = self.path.read_text().splitlines()
        queue = json.loads(rows[-2])
        queue["content"] = queue["message"]["content"]
        rows[-2] = json.dumps(queue)
        self.path.write_text("\n".join(rows) + "\n")
        completed = self.drain()
        self.assertEqual(completed["status"], "ready")
        self.assertFalse(self.source._transcript(self.path)["background_pending"])

    def test_malformed_or_duplicate_fields_in_discarded_payload_never_authorize_ready(self):
        prefix = b'{"padding":"' + b"x" * 1100000 + b'",'
        corruptions = (
            b'"type":"progress","type":"assistant"}',
            b'"nested":{"same":1,"same":2},"type":"progress"}',
            b'"image":"bad\\q","type":"progress"}',
            b'"image":"bad\xff","type":"progress"}',
            b'"array":[1,],"type":"progress"}',
            b'"number":01,"type":"progress"}',
            b'"ignored":NaN,"type":"progress"}',
            b'"ignored":Infinity,"type":"progress"}',
            b'"ignored":-Infinity,"type":"progress"}',
            b'"ignored":1e999,"type":"progress"}',
            b'"nested":' + b"[" * 65 + b"0" + b"]" * 65 + b',"type":"progress"}',
        )
        for damaged in corruptions:
            with self.subTest(damaged=damaged[:60]):
                self.path.write_bytes(prefix + damaged + b"\n")
                self.append(self.prompt(), self.reply())
                item = self.drain(ClaudeSource(self.root))
                self.assertTrue(item["history_checking"])
                self.assertFalse(item["history_verified"])
                self.assertEqual(item["status"], "disconnected")

    def test_small_nonfinite_json_constants_cannot_authorize_ready(self):
        for constant in (b"NaN", b"Infinity", b"-Infinity", b"1e999"):
            with self.subTest(constant=constant):
                self.path.write_bytes(b'{"type":"progress","ignored":' + constant + b'}\n')
                self.append(self.prompt(), self.reply())
                item = self.drain(ClaudeSource(self.root))
                self.assertTrue(item["history_checking"])
                self.assertEqual(item["status"], "disconnected")

    def test_large_progress_structured_content_does_not_poison_lifecycle(self):
        self.append(self.prompt(), self.event("progress", content={"payload": "x" * 2000000}), self.reply())
        item = self.drain()
        self.assertEqual(item["status"], "ready")
        self.assertTrue(item["history_verified"])

    def test_same_inode_large_rewrite_and_replacement_invalidate_old_generation(self):
        self.append(self.prompt(), self.reply(content="Complete " + "x" * 2000000))
        initial = self.drain()
        with patch("claude_source.TRANSCRIPT_POLL_BYTES", 128 * 1024):
            self.append(self.prompt("prompt-2", content="Next " + "x" * 2000000))
            first = self.source.scan(self.now + 1, self.now - 3600)[0][0]
            self.assertTrue(first["history_checking"])
            self.append(self.reply("reply-2"))
            appended = self.drain()
        self.assertEqual(appended["status"], "ready")
        self.assertEqual(appended["history_revision"]["generation"], initial["history_revision"]["generation"])
        old = self.path.stat().st_ino
        self.path.write_bytes(self.path.read_bytes().replace(b'"stop_reason": "end_turn"', b'"stop_reason": "tool_use"'))
        self.assertEqual(self.path.stat().st_ino, old)
        rewritten = self.drain()
        self.assertEqual(rewritten["status"], "cooking")
        self.assertNotEqual(rewritten["history_revision"]["generation"], initial["history_revision"]["generation"])
        replacement = self.path.with_suffix(".replacement")
        replacement.write_text(json.dumps(self.prompt("replacement-turn", content="Replacement " + "x" * 2000000)) + "\n")
        replacement.replace(self.path)
        replaced = self.drain()
        self.assertEqual(replaced["status"], "cooking")
        self.assertEqual(replaced["turn_id"], "replacement-turn")
        self.assertNotEqual(replaced["history_revision"]["generation"], rewritten["history_revision"]["generation"])

    def test_streaming_projection_matches_lifecycle_for_many_chunk_boundaries(self):
        events = [self.prompt(content=[{"type": "text", "text": "Inspect the fixture. 🟢 \" \\ \n\t"},
                                       {"type": "image", "source": {"data": "aGVsbG8="}}]),
                  self.event("assistant", content=[{"type": "tool_use", "name": "AskUserQuestion",
                                                     "input": {"questions": [{"question": "Which 🟢 fixture?"}]}}]),
                  self.reply(content="Finished 🟢 fixture")]
        randomizer = random.Random(555)
        for ensure_ascii in (True, False):
            expected = _TranscriptConsumer("session-large", True)
            actual = _TranscriptConsumer("session-large", True)
            for index, event in enumerate(events):
                raw = json.dumps(event, ensure_ascii=ensure_ascii).encode()
                parser = _ProjectedJSONRecord()
                offset = 0
                while offset < len(raw):
                    size = randomizer.randint(1, 13)
                    used = parser.feed(raw[offset:offset + size], time.monotonic() + 10)
                    self.assertGreater(used, 0)
                    offset += used
                actual.consume(parser.finish(), index)
                expected.consume(event, index)
                self.assertEqual(actual.snapshot(), expected.snapshot())

    def test_truncated_prefix_cannot_forge_complete_task_notification(self):
        notification = "<task-notification><task-id>task-bg</task-id><status>completed</status></task-notification>"
        text = notification + " " * (claude_source.TRANSCRIPT_TEXT_CHARS - len(notification)) + "unexpected suffix"
        parser = _ProjectedJSONRecord()
        raw = json.dumps(dict(type="queue-operation", operation="enqueue", content=text)).encode()
        parser.feed(raw, time.monotonic() + 10)
        projected = parser.finish()
        self.assertEqual(claude_source._task_notification(projected["content"]), ("", "", ""))

    def test_resource_limits_fail_closed_and_expired_budget_preserves_parse_position(self):
        parser = _ProjectedJSONRecord()
        raw = json.dumps(dict(type="progress", ignored=[0] * 1000000)).encode()
        used = parser.feed(raw, time.monotonic() - 1)
        self.assertEqual(used, 0)
        offset = 0
        while offset < len(raw):
            offset += parser.feed(raw[offset:offset + 65536], time.monotonic() + .01)
            self.assertLessEqual(parser.buffer_size, claude_source.TRANSCRIPT_RECORD_BYTES)
        self.assertEqual(parser.finish(), {"type": "progress"})
        for event in (dict(type="progress", **{f"field-{index}": 0 for index in range(17000)}),
                      dict(type="assistant", message={"content": [dict(type="text", text="x" * 65536)] * 30})):
            with self.assertRaises(ValueError):
                parser = _ProjectedJSONRecord()
                parser.feed(json.dumps(event).encode(), time.monotonic() + 10)
                parser.finish()

    def test_explicit_desktop_roots_disable_command_discovery_and_confine_paths(self):
        desktop = self.root / "desktop"
        with patch("claude_source._find_claude", side_effect=AssertionError("No installed CLI access")):
            source = ClaudeSource(self.root, desktop_root=desktop, allow_commands=False, confine_paths=True)
        self.assertIsNone(source.command)
        self.assertEqual(source.desktop_root, desktop)
        self.assertTrue(source.desktop_only)
        outside = self.root / "outside.jsonl"
        outside.write_text(json.dumps(self.reply()) + "\n")
        self.path.symlink_to(outside)
        self.assertTrue(source._transcript(self.path)["history_checking"])

    def test_desktop_index_filters_bad_paths_before_stat_and_keeps_good_metadata(self):
        desktop = self.root / "desktop"
        folder = desktop / "account/org"
        folder.mkdir(parents=True)
        good = folder / "local_good.json"
        good.write_text(json.dumps(dict(sessionId="local_good", cliSessionId="session-large", title="Disposable metadata")))
        outside = self.root / "outside-desktop.json"
        outside.write_text(json.dumps(dict(sessionId="local_escape", cliSessionId="private-outside", title="Outside grant")))
        escape = folder / "local_escape.json"
        escape.symlink_to(outside)
        (folder / "local_broken.json").symlink_to(self.root / "absent.json")
        (folder / "local_directory.json").mkdir()
        os.mkfifo(folder / "local_fifo.json")
        real_stat = Path.stat

        def guarded_stat(path, *args, **kwargs):
            if path == escape and kwargs.get("follow_symlinks", True):
                raise AssertionError("An unvalidated symlink must not be followed for sorting")
            return real_stat(path, *args, **kwargs)

        source = ClaudeSource(self.root, desktop_root=desktop, allow_commands=False, confine_paths=True)
        with patch.object(Path, "stat", guarded_stat):
            sessions = source._desktop_sessions({"session-large"})
        self.assertEqual(set(sessions), {"session-large"})
        self.assertTrue(sessions["session-large"]["_classification_checked"])
        self.assertFalse(source._desktop_index_complete)

    def test_same_session_in_two_project_files_is_one_unverified_card_until_unambiguous(self):
        self.append(self.prompt(), self.reply())
        self.assertEqual(self.drain()["status"], "ready")
        duplicate = self.root / "projects/other-project" / self.path.name
        duplicate.parent.mkdir(parents=True)
        duplicate.write_text(json.dumps(self.prompt("conflicting-turn")) + "\n")
        observations, _ = self.source.scan(self.now + 1, self.now - 3600)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["status"], "disconnected")
        self.assertTrue(observations[0]["history_checking"])
        self.assertIn("Multiple local histories", observations[0]["detail"])
        duplicate.unlink()
        self.append(self.prompt("unambiguous-new-turn"))
        recovered = self.drain()
        self.assertEqual(recovered["status"], "cooking")
        self.assertEqual(recovered["turn_id"], "unambiguous-new-turn")

    def test_monitor_fixture_workflow_review_append_partial_completion_and_restart(self):
        from server import Monitor
        monitor_root = self.root / "monitor-data"
        monitor = Monitor(monitor_root, codex_root=self.root / "absent-codex", claude_root=self.root,
                          usage_enabled=False, projects_enabled=False)
        self.append(self.prompt(), self.reply())
        monitor.scan()
        first = monitor.store.get("claude:session-large")
        self.assertEqual(first["status"], "ready")
        monitor.store.review(first["key"], first["event_id"])
        self.append(self.prompt("prompt-2", content=[{"type": "image", "source": {"data": "A" * 2000000}}]))
        for _ in range(100):
            monitor.scan()
            cooking = monitor.store.get(first["key"])
            if not cooking["history_checking"]:
                break
        self.assertEqual(cooking["status"], "cooking")
        self.assertEqual(len(monitor.store.snapshot()["sessions"]), 1)
        partial = json.dumps(self.reply("reply-2", content="Finished " + "x" * 2000000)).encode()
        with self.path.open("ab") as stream:
            stream.write(partial[:-1])
        monitor.scan()
        self.assertTrue(monitor.store.get(first["key"])["history_checking"])
        with self.path.open("ab") as stream:
            stream.write(partial[-1:] + b"\n")
        for _ in range(100):
            monitor.scan()
            complete = monitor.store.get(first["key"])
            if not complete["history_checking"]:
                break
        self.assertEqual(complete["status"], "ready")
        self.assertNotEqual(complete["event_id"], first["event_id"])
        monitor.store.review(complete["key"], complete["event_id"])
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        restarted = Monitor(monitor_root, codex_root=self.root / "absent-codex", claude_root=self.root,
                            usage_enabled=False, projects_enabled=False)
        for _ in range(100):
            restarted.scan()
            if not restarted.store.get(first["key"])["history_checking"]:
                break
        self.assertFalse(restarted.store.snapshot()["sessions"])
        self.assertEqual(len(restarted.store.snapshot()["history"]), 1)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_64mib_image_http_review_and_restart_keep_bounded_memory_and_source_bytes(self):
        from server import BoardHTTPServer, Handler, Monitor
        monitor_root = self.root / "http-monitor"
        monitor = Monitor(monitor_root, codex_root=self.root / "absent-codex", claude_root=self.root,
                          usage_enabled=False, projects_enabled=False)
        with self.path.open("wb") as stream:
            stream.write(('{"sessionId":"session-large","type":"user","uuid":"large-image-prompt",'
                          '"timestamp":' + str(self.now) + ',"message":{"content":[{"type":"image",'
                          '"source":{"data":"').encode())
            for _ in range(1024):
                stream.write(b"A" * 65536)
            stream.write(b'"}}]}}\n')
        self.append(self.reply("large-image-complete"))

        def digest():
            hasher = hashlib.sha256()
            with self.path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(524288), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()

        before = digest()
        tracemalloc.start()
        try:
            for _ in range(400):
                monitor.scan()
                item = monitor.store.get("claude:session-large")
                if item and not item["history_checking"]:
                    break
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(item["status"], "ready")
        self.assertFalse(item["history_checking"])
        self.assertLess(peak, 16 * 1024 * 1024, "A large ignored image must not be decoded into memory")
        server = BoardHTTPServer(("127.0.0.1", 0), Handler)
        server.monitor, server.access_token = monitor, "a" * 64
        server.instance_id = "disposable-large-image-http-test"
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()

        def request(method, path, body=None):
            headers = {"X-Agent-Board-Access": server.access_token,
                       "X-Agent-Board-Token": monitor.token, "Content-Type": "application/json"}
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request(method, path, body=None if body is None else json.dumps(body).encode(), headers=headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        try:
            status, board = request("GET", "/api/state")
            self.assertEqual(status, 200)
            self.assertEqual(len(board["sessions"]), 1)
            item = board["sessions"][0]
            status, _ = request("POST", "/api/sessions/" + item["key"] + "/review", {"event_id": item["event_id"]})
            self.assertEqual(status, 200)
            self.assertFalse(request("GET", "/api/state")[1]["sessions"])
            self.assertEqual(len(request("GET", "/api/state")[1]["history"]), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1)
        restarted = Monitor(monitor_root, codex_root=self.root / "absent-codex", claude_root=self.root,
                            usage_enabled=False, projects_enabled=False)
        for _ in range(400):
            restarted.scan()
            if not restarted.store.get(item["key"])["history_checking"]:
                break
        self.assertFalse(restarted.store.snapshot()["sessions"])
        self.assertEqual(len(restarted.store.snapshot()["history"]), 1)
        self.assertEqual(digest(), before)


if __name__ == "__main__":
    unittest.main()
