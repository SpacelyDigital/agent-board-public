"""Exercise the private native snapshot, board lifecycle and exact chat links."""

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import server
from board_store import Conflict
from chat_links import UnsupportedChatLink, open_command


FIRST = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"
LAUNCH = "ABCDEF12-3456-4789-8123-123456789ABC"
WINDOW = "window:0123456789abcdef0123456789abcdef"


class ClaudeDesktopChatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.token_file = self.folder / (".collector-access-" + LAUNCH)
        self.token = "a" * 64
        self.token_file.write_text(self.token)
        self.token_file.chmod(0o600)
        self.file = self.folder / ("claude-chat-status-" + LAUNCH + ".json")
        self.launch = server.NativeLaunchFiles(self.folder, LAUNCH, self.token_file, self.token, parent_lifetime=True)
        self.addCleanup(self.launch.cleanup)
        self.source = server.ClaudeDesktopChatSource(self.launch, self.file, LAUNCH)
        self.monitor = self.make_monitor(self.source)
        self.now = time.time() + 1

    def make_monitor(self, source, *, chatgpt=None):
        # Construct neither real provider reader nor any optional account helper.
        with patch.object(server, "CodexSource"), patch.object(server, "ClaudeSource"), \
             patch.object(server, "UsageSource", side_effect=AssertionError("Account usage read")), \
             patch.object(server, "ClaudeProjectSource", side_effect=AssertionError("Account project read")):
            monitor = server.Monitor(self.folder, claude_chat_source=source, chatgpt_chat_source=chatgpt)
        monitor.sources = []
        return monitor

    @staticmethod
    def row(ident=FIRST, status="cooking", title="Check keyboard navigation"):
        return {"id": ident, "title": title, "status": status}

    def publish(self, *chats, observed=None):
        self.write(json.dumps({"observed_at": self.now if observed is None else observed, "chats": chats}).encode())

    def write(self, raw):
        temporary = self.folder / "producer-temporary"
        temporary.write_bytes(raw)
        temporary.chmod(0o600)
        temporary.replace(self.file)

    def scan(self, elapsed=1):
        self.now += elapsed
        with patch.object(server.time, "time", return_value=self.now):
            self.monitor.scan()
            return self.monitor.snapshot()

    def get(self, ident=FIRST):
        return self.monitor.store.get("claude:desktop_chat:" + ident)

    def test_observed_cooking_becomes_ready_once_and_review_survives_idle_polls(self):
        self.publish(self.row(status="idle"))
        self.scan()
        self.assertIsNone(self.get())
        self.publish(self.row())
        self.scan()
        cooking = self.get()
        self.assertEqual(cooking["status"], "cooking")
        self.publish(self.row(title="Check larger text"))
        self.scan()
        updated = self.get()
        self.assertEqual(updated["event_id"], cooking["event_id"])
        self.assertEqual(updated["started_at"], cooking["started_at"])
        self.assertEqual(updated["title"], "Check larger text")
        self.publish(self.row(status="idle", title="Check larger text"))
        state = self.scan()
        ready = self.get()
        self.assertEqual(ready["status"], "ready")
        self.assertNotEqual(ready["event_id"], cooking["event_id"])
        self.assertTrue(state["sessions"][0]["notification_eligible"])
        self.monitor.store.review(ready["key"], ready["event_id"])
        self.publish(self.row(status="idle"))
        state = self.scan()
        self.assertEqual(state["sessions"], [])
        self.assertEqual(state["history"][0]["event_id"], ready["event_id"])
        self.assertEqual(state["history"][0]["reviewed_event"], ready["event_id"])
        self.publish(self.row())
        self.scan()
        with self.assertRaises(Conflict):
            self.monitor.store.review(ready["key"], ready["event_id"])
        self.publish(self.row(status="idle"))
        self.scan()
        self.assertNotEqual(self.get()["event_id"], ready["event_id"])
        self.assertTrue(self.get()["notification_eligible"])

    def test_missing_chat_or_same_title_for_different_id_does_not_complete_turn(self):
        self.publish(self.row())
        self.scan()
        event = self.get()["event_id"]
        self.publish(self.row(SECOND, "idle"))
        snapshot = self.scan()
        self.assertTrue(snapshot["sessions"][0]["source_unavailable"])
        self.assertEqual(self.get()["event_id"], event)
        self.assertIsNone(self.get(SECOND))
        self.publish(self.row(status="idle"))
        self.scan()
        self.assertEqual(self.get()["status"], "cooking")
        self.assertEqual(self.get()["event_id"], event)

    def test_expired_snapshot_and_unobserved_time_gap_break_continuity(self):
        for elapsed_poll in (True, False):
            with self.subTest(elapsed_poll=elapsed_poll):
                self.publish(self.row())
                self.scan()
                event = self.get()["event_id"]
                if elapsed_poll:
                    snapshot = self.scan(self.source.MAX_AGE)
                    self.assertTrue(snapshot["sessions"][0]["source_unavailable"])
                else:
                    self.now += self.source.MAX_AGE + 1
                self.publish(self.row(status="idle"))
                self.scan()
                self.assertEqual(self.get()["status"], "cooking")
                self.assertEqual(self.get()["event_id"], event)

    def test_invalid_or_replayed_observations_cannot_complete_a_chat(self):
        self.publish(self.row())
        self.scan()
        event = self.get()["event_id"]
        for observed in (self.now - 2, self.now - 1, self.now + 30):
            with self.subTest(observed=observed):
                self.publish(self.row(status="idle"), observed=observed)
                snapshot = self.scan()
                self.assertTrue(snapshot["sessions"][0]["source_unavailable"])
                self.assertEqual(self.get()["event_id"], event)
        # Even replaying the exact prior Cooking data after the gap cannot
        # restore continuity. Wait for a new observation.
        self.publish(self.row(), observed=self.source.observed_at)
        self.assertTrue(self.scan()["sessions"][0]["source_unavailable"])
        self.publish(self.row(status="idle"))
        self.scan()
        self.assertEqual(self.get()["status"], "cooking")

    def test_parser_refuses_unknown_duplicate_and_oversize_snapshots(self):
        invalid = (
            lambda: self.publish(self.row(status="unknown")),
            lambda: self.publish(self.row(), self.row(status="idle")),
            lambda: self.publish(self.row(ident="not-a-chat-id")),
            lambda: self.publish(self.row(title="a" * 501)),
            lambda: self.publish(*(self.row() for _ in range(65))),
            lambda: self.write(b" " * 65_537),
            lambda: self.write(b'{"observed_at": true, "chats": []}'),
            lambda: self.write(b'{"observed_at": 1, "observed_at": 2, "chats": []}'),
            lambda: self.write(b"\xff"),
        )
        for change in invalid:
            self.publish(self.row())
            self.scan()
            event = self.get()["event_id"]
            change()
            self.assertTrue(self.scan()["sessions"][0]["source_unavailable"])
            self.publish(self.row(status="idle"))
            self.scan()
            self.assertEqual(self.get()["event_id"], event)

    def test_unsafe_files_never_supply_idle(self):
        canary = self.folder / "other-private-file"
        for kind in ("symlink", "hardlink", "public", "fifo", "missing"):
            with self.subTest(kind=kind):
                self.publish(self.row())
                self.scan()
                event = self.get()["event_id"]
                self.publish(self.row(status="idle"))
                private_idle = self.file.read_bytes()
                self.file.unlink()
                canary.write_bytes(private_idle)
                canary.chmod(0o600)
                if kind == "symlink":
                    self.file.symlink_to(canary)
                elif kind == "hardlink":
                    os.link(canary, self.file)
                elif kind == "public":
                    self.file.write_bytes(private_idle)
                    self.file.chmod(0o644)
                elif kind == "fifo":
                    os.mkfifo(self.file, 0o600)
                self.assertTrue(self.scan()["sessions"][0]["source_unavailable"])
                self.assertEqual(self.get()["event_id"], event)
                self.assertEqual(canary.read_bytes(), private_idle)
                self.assertEqual(canary.stat().st_mode & 0o777, 0o600)

    def test_claude_diagnostics_accept_only_bounded_numeric_counters(self):
        self.write(json.dumps({"observed_at": self.now, "chats": [self.row()],
                               "diagnostics": {"composers": 1, "idle_controls": 0, "request_limit_reached": 0,
                                   "complete_idle_scopes": 0, "incomplete_control_scopes": 2,
                                   "child_count_unavailable": 3, "incomplete_child_batches": 1,
                                   "empty_child_values": 4, "child_truncations": 2,
                                   "child_count_mismatches": 1, "child_copy_failures": 0,
                                   "depth_truncations": 3, "max_child_count": 34,
                                   "control_read_failures": 0, "control_child_failures": 1,
                                   "control_depth_truncations": 1, "max_control_child_count": 26}}).encode())
        self.scan()
        self.assertEqual(self.get()["status"], "cooking")
        for invalid in ({"title": "Private text"}, {"composers": True}, {"request_limit_reached": 2}, {"composers": 10001},
                        {"empty_child_values": "Private text"}, {"incomplete_control_scopes": -1}):
            self.write(json.dumps({"observed_at": self.now, "chats": [], "diagnostics": invalid}).encode())
            self.assertTrue(self.scan()["sessions"][0]["source_unavailable"])
    def test_restart_and_opt_out_do_not_resume_a_pending_turn_from_idle(self):
        self.publish(self.row())
        self.scan()
        event = self.get()["event_id"]
        self.publish(self.row(status="idle"))
        self.monitor = self.make_monitor(None)
        with patch.object(server.ClaudeDesktopChatSource, "read_snapshot", side_effect=AssertionError("Opted out read")):
            state = self.scan()
        self.assertEqual(state["sessions"], [])
        self.assertEqual(state["counts"]["cooking"], 0)
        self.monitor = self.make_monitor(server.ClaudeDesktopChatSource(self.launch, self.file, LAUNCH))
        self.scan()
        self.assertEqual(self.get()["event_id"], event)
        self.assertEqual(self.get()["status"], "cooking")

    def test_claude_code_exception_does_not_demote_or_stop_desktop_chat_scan(self):
        class FailingClaudeCode:
            def scan(self, *_):
                raise OSError("Fake Claude Code source error")

        source = FailingClaudeCode()
        self.monitor.sources = [source]
        self.publish(self.row())
        with self.assertLogs(server.LOG, level="WARNING"):
            self.scan()
        self.publish(self.row(status="idle"))
        with self.assertLogs(server.LOG, level="WARNING"):
            snapshot = self.scan()
        self.assertEqual(source.tracked_ids, set())
        self.assertEqual(self.get()["status"], "ready")
        self.assertFalse(snapshot["sessions"][0]["source_unavailable"])
        health = {item["id"]: item["available"] for item in snapshot["sources"]}
        self.assertEqual(health, {"claude": False, "claude_chats": True})

    def test_only_exact_owned_launch_path_is_accepted(self):
        with self.assertRaises(ValueError):
            server.ClaudeDesktopChatSource(self.launch, self.folder / "some-other-file", LAUNCH)
        with self.assertRaises(ValueError):
            server.ClaudeDesktopChatSource(self.launch, self.file, LAUNCH.lower())
        unvalidated = server.NativeLaunchFiles(self.folder, LAUNCH, self.token_file, self.token)
        with self.assertRaises(ValueError):
            server.ClaudeDesktopChatSource(unvalidated, self.file, LAUNCH)
        self.folder.chmod(0o755)
        self.publish(self.row())
        self.scan()
        self.assertIsNone(self.get())

    def test_launch_flag_rejects_non_native_and_isolated_modes_before_file_access(self):
        status = ["--claude-chat-status-file", str(self.file)]
        native = ["--parent-lifetime-stdin", "--instance-id", LAUNCH, "--access-token-file", str(self.token_file)]
        for args in (status, status + native + ["--sandbox"], status + native + ["--store-profile"],
                     status + native + ["--sample-mode"], status + native + ["--codex-root", str(self.folder)]):
            with self.subTest(args=args), patch.object(server.sys, "argv", ["server", *args]), \
                 patch.object(server, "read_access_token", side_effect=AssertionError("Invalid launch read")), \
                 redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as rejected:
                server.main()
            self.assertEqual(rejected.exception.code, 2)

    def test_only_exact_same_id_chat_link_opens_in_default_browser(self):
        self.publish(self.row())
        self.scan()
        session = self.get()
        url = "https://claude.ai/chat/" + FIRST
        self.assertEqual(open_command(session), ["/usr/bin/open", url])
        for invalid in (url + "?prompt=hello", url + "#fragment", url.replace(FIRST, SECOND),
                        url.replace("claude.ai", "claude.ai.example"), url.replace("claude.ai", "claude.ai:443")):
            with self.subTest(invalid=invalid), self.assertRaises(UnsupportedChatLink):
                open_command({**session, "open_url": invalid})
        with self.assertRaises(UnsupportedChatLink):
            open_command({**session, "source_kind": "local"})

    def test_chatgpt_and_claude_have_independent_status_and_exact_links(self):
        chatgpt_file = self.folder / ("chatgpt-chat-status-" + LAUNCH + ".json")
        chatgpt = server.ChatGPTDesktopChatSource(self.launch, chatgpt_file, LAUNCH)
        self.monitor = self.make_monitor(self.source, chatgpt=chatgpt)
        claude_file = self.file
        self.publish(self.row(title="Disposable Claude chat"))
        self.file = chatgpt_file
        self.publish(self.row(status="idle", title="Disposable ChatGPT chat"))
        snapshot = self.scan()
        self.assertEqual([item["provider"] for item in snapshot["sessions"]], ["claude"])
        self.publish(self.row(title="Disposable ChatGPT chat"))
        snapshot = self.scan()
        self.assertEqual(snapshot["counts"]["cooking"], 2)
        self.assertEqual({item["id"] for item in snapshot["sources"]}, {"claude_chats", "chatgpt_chats"})
        self.publish(self.row(status="idle", title="Disposable ChatGPT chat"))
        snapshot = self.scan()
        chats = {item["provider"]: item for item in snapshot["sessions"]}
        self.assertEqual(chats["claude"]["status"], "cooking")
        self.assertEqual(chats["chatgpt"]["status"], "ready")
        self.assertTrue(chats["chatgpt"]["notification_eligible"])
        url = "https://chatgpt.com/c/" + FIRST
        self.assertEqual(open_command(chats["chatgpt"]), ["/usr/bin/open", url])
        for invalid in (url + "?prompt=hello", url.replace(FIRST, SECOND),
                        url.replace("chatgpt.com", "chatgpt.com.example"), "https://claude.ai/chat/" + FIRST):
            with self.subTest(invalid=invalid), self.assertRaises(UnsupportedChatLink):
                open_command({**chats["chatgpt"], "open_url": invalid})
        self.file = claude_file
        self.publish(self.row(status="idle", title="Disposable Claude chat"))
        snapshot = self.scan()
        self.assertEqual(snapshot["counts"]["ready"], 2)

    def test_ignore_hides_regular_chats_independently_without_erasing_preferences(self):
        chatgpt_file = self.folder / ("chatgpt-chat-status-" + LAUNCH + ".json")
        chatgpt = server.ChatGPTDesktopChatSource(self.launch, chatgpt_file, LAUNCH)
        self.monitor = self.make_monitor(self.source, chatgpt=chatgpt)
        rows = []
        for provider in ("claude", "chatgpt"):
            for index, ident in enumerate((FIRST, SECOND, "33333333-3333-4333-8333-333333333333")):
                rows.append({"provider": provider, "id": "desktop_chat:" + ident,
                             "source_kind": "desktop_chat", "title": f"{provider} chat {index}",
                             "updated_at": self.now, "status": "ready", "event_id": "fixture"})
        rows.extend({"provider": provider, "id": "local", "title": "Unaffected local session",
                     "updated_at": self.now, "status": "ready"} for provider in ("claude", "codex"))
        self.monitor.store.apply(rows, self.now)
        for provider in ("claude", "chatgpt"):
            history = self.monitor.store.get(provider + ":desktop_chat:" + FIRST)
            self.monitor.store.review(history["key"], history["event_id"])
            background = provider + ":desktop_chat:" + SECOND
            self.monitor.store.move(background, "background")
            self.monitor.store.rename(background, "Saved local name")
        for claude_source, chatgpt_source, visible in ((None, chatgpt, {"chatgpt"}),
                                                       (self.source, None, {"claude"}),
                                                       (None, None, set()),
                                                       (self.source, chatgpt, {"claude", "chatgpt"})):
            self.monitor = self.make_monitor(claude_source, chatgpt=chatgpt_source)
            snapshot = self.scan()
            for group in ("sessions", "history", "background"):
                self.assertEqual({item["provider"] for item in snapshot[group]
                                  if item.get("source_kind") == "desktop_chat"}, visible)
            self.assertEqual(snapshot["counts"]["ready"], 2 + len(visible))
            self.assertEqual({item["provider"] for item in snapshot["sessions"] if item["id"] == "local"},
                             {"claude", "codex"})
            self.assertTrue(all(item["title"] == "Saved local name" for item in snapshot["background"]))
            self.assertTrue(all(item["event_id"] == item["reviewed_event"] for item in snapshot["history"]))

    def test_first_seen_idle_is_only_open_in_background_until_it_closes_or_expires(self):
        chatgpt_file = self.folder / ("chatgpt-chat-status-" + LAUNCH + ".json")
        chatgpt = server.ChatGPTDesktopChatSource(self.launch, chatgpt_file, LAUNCH)
        self.monitor = self.make_monitor(self.source, chatgpt=chatgpt)
        self.publish(self.row(status="idle", title="Disposable Claude chat"))
        self.file = chatgpt_file
        self.publish(self.row(status="idle", title="Disposable ChatGPT chat"))
        snapshot = self.scan()
        self.assertEqual(snapshot["sessions"], [])
        self.assertEqual(snapshot["history"], [])
        self.assertTrue(all(value == 0 for value in snapshot["counts"].values()))
        self.assertEqual({item["provider"] for item in snapshot["background"]}, {"claude", "chatgpt"})
        for item in snapshot["background"]:
            self.assertTrue(item["open_only"])
            self.assertFalse(item["notification_eligible"])
            self.assertIsNone(self.monitor.store.get(item["key"]))
            with patch.object(server.time, "time", return_value=self.now):
                self.assertEqual(open_command(self.monitor.open_desktop_chat(item["key"])), open_command(item))
            with self.assertRaises(KeyError):
                self.monitor.store.rename(item["key"], "An unsaved chat cannot be renamed.")
        key = "chatgpt:desktop_chat:" + FIRST
        with patch.object(server.time, "time", return_value=self.now + chatgpt.MAX_AGE + 1):
            self.assertIsNone(self.monitor.open_desktop_chat(key))
            self.assertEqual(self.monitor.snapshot()["background"], [])
        self.publish()
        snapshot = self.scan()
        self.assertEqual([item["provider"] for item in snapshot["background"]], ["claude"])
        self.assertIsNone(self.monitor.open_desktop_chat(key))

    def test_open_idle_does_not_duplicate_a_saved_reply_or_reopen_reviewed_history(self):
        self.publish(self.row())
        self.scan()
        self.publish(self.row(status="idle"))
        snapshot = self.scan()
        ready = snapshot["sessions"][0]
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(snapshot["background"], [])
        self.monitor.store.review(ready["key"], ready["event_id"])
        self.publish(self.row(status="idle"))
        snapshot = self.scan()
        self.assertEqual(snapshot["sessions"], [])
        self.assertEqual(snapshot["history"][0]["key"], ready["key"])
        self.assertEqual(snapshot["background"], [])

    def test_chatgpt_waits_for_a_recognized_chat_without_erasing_saved_replies(self):
        self.file = self.folder / ("chatgpt-chat-status-" + LAUNCH + ".json")
        chatgpt = server.ChatGPTDesktopChatSource(self.launch, self.file, LAUNCH)
        self.monitor = self.make_monitor(None, chatgpt=chatgpt)
        self.publish(self.row())
        self.assertNotIn("waiting_for_chat", self.scan()["sources"][0])
        self.publish(self.row(status="idle"))
        snapshot = self.scan()
        self.assertEqual(snapshot["sessions"][0]["status"], "ready")
        self.assertNotIn("waiting_for_chat", snapshot["sources"][0])
        diagnostics = {"apps": 1, "windows": 3, "classifications": 0, "deadline_reached": 0}
        self.write(json.dumps({"observed_at": self.now, "chats": [], "diagnostics": diagnostics}).encode())
        snapshot = self.scan()
        self.assertEqual(snapshot["sessions"][0]["status"], "ready")
        self.assertFalse(snapshot["sessions"][0]["source_unavailable"])
        self.assertTrue(snapshot["sources"][0]["available"])
        self.assertTrue(snapshot["sources"][0]["waiting_for_chat"])
        self.assertNotIn("diagnostics", snapshot["sources"][0])
        for invalid in ({"apps": True}, {"windows": 10_001}, {"shell_urls": -1},
                        {"deadline_reached": 2}, {"unrecognized": 1}):
            self.write(json.dumps({"observed_at": self.now, "chats": [], "diagnostics": invalid}).encode())
            snapshot = self.scan()
            self.assertFalse(snapshot["sources"][0]["available"])
            self.assertNotIn("waiting_for_chat", snapshot["sources"][0])
            self.assertEqual(snapshot["sessions"][0]["status"], "ready")

    def test_chatgpt_window_is_visible_and_tracks_replies_without_a_direct_link(self):
        self.file = self.folder / ("chatgpt-chat-status-" + LAUNCH + ".json")
        chatgpt = server.ChatGPTDesktopChatSource(self.launch, self.file, LAUNCH)
        self.monitor = self.make_monitor(None, chatgpt=chatgpt)
        key = "chatgpt:desktop_chat:" + WINDOW
        self.publish(self.row(WINDOW, "idle", "Disposable ChatGPT window"))
        open_only = self.scan()["background"][0]
        self.assertEqual(open_only["key"], key)
        self.assertTrue(open_only["open_only"])
        self.assertFalse(open_only["can_open"])
        self.assertNotIn("open_url", open_only)
        self.assertIsNone(self.monitor.store.get(key))
        self.publish(self.row(WINDOW, "cooking", "Disposable ChatGPT window"))
        self.assertEqual(self.scan()["sessions"][0]["status"], "cooking")
        self.publish(self.row(WINDOW, "idle", "Disposable ChatGPT window"),
                     self.row(FIRST, "idle", "Another ChatGPT chat with a verified ID"))
        snapshot = self.scan()
        self.assertEqual(len(snapshot["sessions"]), 1)
        ready = snapshot["sessions"][0]
        self.assertEqual(ready["key"], key)
        self.assertEqual(ready["title"], "Disposable ChatGPT window")
        self.assertEqual(ready["status"], "ready")
        self.assertFalse(ready["can_open"])
        self.assertNotIn("open_url", ready)
        self.assertEqual(open_command(snapshot["background"][0]), ["/usr/bin/open", "https://chatgpt.com/c/" + FIRST])
        with self.assertRaises(UnsupportedChatLink):
            open_command({**ready, "open_url": "https://chatgpt.com/c/" + FIRST})
        forged = self.monitor.store.normalize({**ready, "can_open": True, "open_url": "https://chatgpt.com/c/" + FIRST}, self.now)
        self.assertEqual(forged["can_open"], 0)
        self.assertNotIn("open_url", json.loads(forged["extra"]))

    def test_window_id_cannot_complete_a_changed_title_or_be_imported_by_claude(self):
        self.publish(self.row(WINDOW))
        snapshot = self.scan()
        self.assertFalse(snapshot["sources"][0]["available"])
        self.assertEqual(snapshot["sessions"], [])
        self.assertIsNone(self.monitor.store.normalize({"provider": "claude", "source_kind": "desktop_chat",
            "id": "desktop_chat:" + WINDOW, "title": "Disposable Claude chat", "status": "ready",
            "updated_at": self.now}, self.now))
        self.file = self.folder / ("chatgpt-chat-status-" + LAUNCH + ".json")
        chatgpt = server.ChatGPTDesktopChatSource(self.launch, self.file, LAUNCH)
        self.monitor = self.make_monitor(None, chatgpt=chatgpt)
        self.publish(self.row(WINDOW, "cooking", "First window title"))
        self.scan()
        self.publish(self.row(WINDOW, "idle", "Second window title"))
        snapshot = self.scan()
        self.assertEqual(snapshot["sessions"][0]["status"], "cooking")
        self.assertTrue(snapshot["sessions"][0]["source_unavailable"])
        self.assertEqual(snapshot["counts"]["ready"], 0)


if __name__ == "__main__":
    unittest.main()
