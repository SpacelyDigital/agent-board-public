import tempfile
import time
import unittest
import json
from unittest.mock import patch
from pathlib import Path

from board_store import BoardStore, Conflict
from claude_source import ClaudeSource
from codex_source import CodexSource
from server import Monitor


class BoardWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "board.sqlite3"
        self.store = BoardStore(self.path)
        self.at = self.store.activated_at

    def observation(self, status="cooking", event="turn-1-start", stamp=None, ident="thread-1"):
        return dict(provider="codex", id=ident, title="Disposable workflow", cwd="/tmp/project",
                    status=status, event_id=event, updated_at=stamp if stamp is not None else self.at + 1,
                    started_at=self.at, can_open=True)

    def test_review_restore_resume_and_restart_are_persistent(self):
        self.store.apply([self.observation()])
        self.assertEqual(self.store.snapshot()["counts"]["cooking"], 1)
        self.store.apply([self.observation("ready", "turn-1-complete", self.at + 2)])
        ready = self.store.snapshot()["sessions"][0]
        self.store.review(ready["key"], ready["event_id"])
        restart = BoardStore(self.path)
        self.assertFalse(restart.snapshot()["sessions"])
        self.assertEqual(len(restart.snapshot()["history"]), 1)
        restart.apply([self.observation("ready", "turn-1-complete", self.at + 2)])
        self.assertFalse(restart.snapshot()["sessions"], "Polling must not resurrect an acknowledged reply")
        restart.restore(ready["key"])
        self.assertEqual(len(restart.snapshot()["sessions"]), 1)
        restart.review(ready["key"], ready["event_id"])
        restart.apply([self.observation("cooking", "turn-2-start", self.at + 3)])
        self.assertEqual(restart.snapshot()["counts"]["cooking"], 1)
        self.assertFalse(restart.snapshot()["history"])

    def test_history_confidence_preserves_review_snooze_and_lifecycle_across_restart(self):
        ready = dict(self.observation("ready", "complete", self.at + 2), provider="claude")
        reviewed = dict(ready, id="reviewed")
        self.store.apply([ready, reviewed])
        original = self.store.get("claude:thread-1")
        reviewed_original = self.store.get("claude:reviewed")
        self.store.snooze(original["key"], original["event_id"], minutes=10, now=self.at + 3)
        self.store.review(reviewed_original["key"], reviewed_original["event_id"])
        snoozed_until = self.store.get(original["key"])["snoozed_until"]
        checking = dict(ready, status="disconnected", event_id="not-a-lifecycle", updated_at=0,
                        history_checking=True, detail="Checking local chat history.")
        self.store.apply([checking, dict(checking, id="reviewed")], now=self.at + 4)
        self.store.mark_unavailable("claude", "Not found")
        restart = BoardStore(self.path)
        active = restart.get(original["key"])
        self.assertEqual(active["event_id"], original["event_id"])
        self.assertEqual(active["status"], "disconnected")
        self.assertEqual(active["confirmed_status"], "ready")
        self.assertEqual(active["snoozed_until"], snoozed_until)
        self.assertFalse(active["notification_eligible"])
        history = restart.snapshot()["history"]
        self.assertEqual([row["key"] for row in history], [reviewed_original["key"]])
        restart.apply([ready, reviewed], now=self.at + 5)
        reconfirmed = restart.get(original["key"])
        self.assertFalse(reconfirmed["history_checking"])
        self.assertEqual(reconfirmed["event_id"], original["event_id"])
        self.assertEqual(reconfirmed["snoozed_until"], snoozed_until)
        with patch("board_store.time.time", return_value=snoozed_until + 1):
            self.assertTrue(restart.get(original["key"])["notification_eligible"], "A future snooze reminder must still be eligible")
            self.assertFalse(restart.get(original["key"])["reconfirmation_silent"])
        restart.apply([dict(ready, event_id="new-completion", updated_at=self.at + 6)], now=self.at + 6)
        new = restart.get(original["key"])
        self.assertTrue(new["notification_eligible"])
        self.assertEqual(new["snoozed_until"], 0)

    def test_unchanged_reconfirmation_cannot_replay_native_attention(self):
        ready = self.observation("ready", "complete", self.at + 2)
        self.store.apply([ready])
        original = self.store.get("codex:thread-1")
        self.store.apply([dict(ready, status="disconnected", updated_at=0,
                               history_checking=True)], now=self.at + 3)
        for _ in range(3):
            self.store.apply([ready], now=self.at + 4)
            item = self.store.get(original["key"])
            self.assertEqual(item["event_id"], original["event_id"])
            self.assertTrue(item["notification_eligible"], "Reconfirmed replies must remain in the native queue")
            self.assertTrue(item["reconfirmation_silent"])
        self.store.apply([dict(ready, status="cooking", event_id="new-start", updated_at=self.at + 5)])
        self.store.apply([dict(ready, event_id="new-complete", updated_at=self.at + 6)])
        self.assertTrue(self.store.get(original["key"])["notification_eligible"])

    def test_monitor_long_history_catchup_keeps_reviewed_state_then_surfaces_new_reply(self):
        budget = patch("claude_source.TRANSCRIPT_POLL_BYTES", 4 * 1024 * 1024)
        budget.start()
        self.addCleanup(budget.stop)
        root = Path(self.temp.name)
        claude = root / "claude"
        path = claude / "projects/disposable/session-1.jsonl"
        path.parent.mkdir(parents=True)
        monitor = Monitor(root / "monitor-data", codex_root=root / "codex", claude_root=claude,
                          usage_enabled=False, projects_enabled=False)
        stamp = monitor.store.activated_at + 1
        def append(*events):
            with path.open("a") as stream:
                for event in events:
                    stream.write(json.dumps({"sessionId": "session-1", "timestamp": stamp, **event}) + "\n")
        prompt = {"type": "user", "uuid": "p1", "message": {"content": "A disposable task"}}
        reply = {"type": "assistant", "uuid": "r1", "message": {"stop_reason": "end_turn", "content": "Fixture reply"}}
        append(prompt, reply)
        monitor.scan()
        original = monitor.store.get("claude:session-1")
        self.assertEqual(original["status"], "ready")
        monitor.store.review(original["key"], original["event_id"])
        for _ in range(65):
            append({"type": "progress", "content": "x" * 65536})
        append(dict(prompt, uuid="p2", timestamp=stamp + 1), dict(reply, uuid="r2", timestamp=stamp + 2))
        monitor.scan()
        checking = monitor.store.get(original["key"])
        self.assertTrue(checking["history_checking"])
        self.assertEqual(checking["event_id"], original["event_id"])
        self.assertEqual(checking["reviewed_event"], original["event_id"])
        self.assertFalse(monitor.store.snapshot()["sessions"])
        for _ in range(30):
            monitor.scan()
            latest = monitor.store.get(original["key"])
            if not latest["history_checking"]:
                break
        self.assertFalse(latest["history_checking"])
        self.assertEqual(latest["status"], "ready")
        self.assertNotEqual(latest["event_id"], original["event_id"])
        self.assertEqual(latest["reviewed_event"], original["event_id"])
        self.assertTrue(latest["notification_eligible"])

    def test_stale_review_cannot_hide_a_new_turn(self):
        self.store.apply([self.observation("ready", "turn-1-complete")])
        old = self.store.snapshot()["sessions"][0]
        self.store.apply([self.observation("ready", "turn-2-complete", self.at + 3)])
        with self.assertRaises(Conflict):
            self.store.review(old["key"], old["event_id"])
        self.assertEqual(len(self.store.snapshot()["sessions"]), 1)
        self.store.apply([self.observation("cooking", "turn-3-start", self.at + 4)])
        with self.assertRaises(Conflict):
            self.store.review(old["key"], self.store.get(old["key"])["event_id"])

    def test_verified_backdated_replacements_recover_after_restart_without_losing_new_turns(self):
        root = Path(self.temp.name)
        for change in ("replace", "rewrite", "truncate"):
            for cold in (False, True):
                for preference in ("none", "reviewed", "snoozed"):
                    with self.subTest(change=change, cold=cold, preference=preference):
                        base = root / f"{change}-{cold}-{preference}"
                        def create_monitor():
                            monitor = Monitor(base / "data", codex_root=base / "codex", claude_root=base / "claude",
                                              usage_enabled=False, projects_enabled=False)
                            monitor.sources = [monitor.sources[1]]
                            return monitor
                        monitor = create_monitor()
                        stamp = monitor.store.activated_at
                        path = base / "claude/projects/disposable/session.jsonl"
                        path.parent.mkdir(parents=True)
                        def prompt(ident, at):
                            return {"type": "user", "uuid": ident, "promptId": ident, "timestamp": at,
                                    "message": {"content": "Disposable request"}}
                        def reply(ident, at):
                            return {"type": "assistant", "uuid": ident, "timestamp": at,
                                    "message": {"stop_reason": "end_turn", "content": "Disposable reply"}}
                        def write(destination, events, mode="w"):
                            with destination.open(mode) as output:
                                for event in events:
                                    output.write(json.dumps({"sessionId": "session", **event}) + "\n")
                        progress = [{"type": "progress", "content": "x" * 65536} for _ in range(66)]
                        write(path, [prompt("old-turn", stamp - 30), *(progress if change == "truncate" else []),
                                     reply("old-ready", stamp + 20)])
                        def drain():
                            for _ in range(40):
                                monitor.scan()
                                row = monitor.store.get("claude:session")
                                if row and not row["history_checking"]:
                                    return row
                            self.fail("A supported fixture history did not finish indexing")
                        old = drain()
                        self.assertEqual(old["status"], "ready")
                        before_revision = monitor.store.transcript_revision("session")
                        self.assertIsNotNone(before_revision)
                        if preference == "reviewed":
                            monitor.store.review(old["key"], old["event_id"])
                        elif preference == "snoozed":
                            monitor.store.snooze(old["key"], old["event_id"], minutes=10, now=stamp)
                        previous = monitor.store.get(old["key"])
                        inode = path.stat().st_ino
                        destination = path.with_suffix(".new") if change == "replace" else path
                        write(destination, [prompt("new-turn", stamp - 20), *(progress if change != "truncate" else [])])
                        if change == "replace":
                            destination.replace(path)
                        else:
                            self.assertEqual(path.stat().st_ino, inode)
                        if cold:
                            monitor = create_monitor()
                        with patch("claude_source.TRANSCRIPT_POLL_BYTES", 1024 * 1024):
                            monitor.scan()
                            checking = monitor.store.get(old["key"])
                            if change != "truncate":
                                self.assertTrue(checking["history_checking"])
                                self.assertEqual(checking["event_id"], old["event_id"])
                                self.assertEqual(checking["reviewed_event"], previous["reviewed_event"])
                                self.assertEqual(checking["snoozed_until"], previous["snoozed_until"])
                            new = drain()
                            self.assertEqual((new["status"], new["turn_id"]), ("cooking", "new-turn"))
                            self.assertEqual(new["updated_at"], stamp - 20)
                            self.assertEqual(len(monitor.store.snapshot()["sessions"]), 1)
                            after_revision = monitor.store.transcript_revision("session")
                            self.assertNotEqual(before_revision["generation"], after_revision["generation"])
                            write(path, [reply("new-ready", stamp - 10)], "a")
                            completed = drain()
                        self.assertEqual((completed["status"], completed["turn_id"]), ("ready", "new-turn"))
                        self.assertEqual(completed["updated_at"], stamp - 10)
                        self.assertTrue(completed["notification_eligible"])
                        self.assertFalse(completed["reconfirmation_silent"])
                        self.assertEqual(completed["snoozed_until"], 0)
                        self.assertNotEqual(completed["event_id"], old["event_id"])
                        self.assertEqual(monitor.store.transcript_revision("session")["generation"], after_revision["generation"])

    def test_older_append_keeps_generation_and_cannot_revert_a_cached_newer_turn(self):
        base = Path(self.temp.name) / "older-append"
        monitor = Monitor(base / "data", codex_root=base / "codex", claude_root=base / "claude",
                          usage_enabled=False, projects_enabled=False)
        monitor.sources = [monitor.sources[1]]
        stamp = monitor.store.activated_at
        path = base / "claude/projects/disposable/session.jsonl"
        path.parent.mkdir(parents=True)
        events = [{"type": "user", "uuid": "current-turn", "timestamp": stamp + 20,
                   "message": {"content": "Disposable request"}}]
        def append(event):
            with path.open("a") as output:
                output.write(json.dumps({"sessionId": "session", **event}) + "\n")
        append(events[0])
        monitor.scan()
        old = monitor.store.get("claude:session")
        revision = monitor.store.transcript_revision("session")
        # Reconstructing the collector must verify and retain the saved prefix,
        # rather than treating every cold start as replacement authority.
        monitor.sources = [ClaudeSource(base / "claude")]
        append({"type": "assistant", "uuid": "delayed-old-reply", "timestamp": stamp - 20,
                "message": {"stop_reason": "end_turn", "content": "Older reply"}})
        monitor.scan()
        saved = monitor.store.get(old["key"])
        self.assertEqual(saved["status"], "disconnected")
        self.assertEqual((saved["turn_id"], saved["updated_at"]), ("current-turn", stamp + 20))
        self.assertEqual(monitor.store.transcript_revision("session")["generation"], revision["generation"])

    def test_metadata_cannot_assert_revision_authority_or_clear_indexing_uncertainty(self):
        revision = {"file_id": "a" * 64, "size": 100, "sha256": "b" * 64, "generation": "c" * 64}
        ready = dict(self.observation("ready", "complete", self.at + 20), provider="claude",
                     history_verified=True, history_revision=revision)
        self.store.apply([ready])
        old = self.store.get("claude:thread-1")
        metadata = dict(ready, status="cooking", event_id="older", updated_at=self.at - 20,
                        metadata_only=True, title="Fresh display title", history_revision=dict(revision, generation="d" * 64))
        for stamp in (self.at - 20, self.at + 20, self.at + 40):
            self.store.apply([dict(metadata, updated_at=stamp)])
            saved = self.store.get(old["key"])
            self.assertEqual((saved["status"], saved["event_id"]), ("ready", old["event_id"]))
            self.assertEqual(self.store.transcript_revision("thread-1"), revision)
        self.store.apply([dict(ready, history_checking=True)])
        for stamp in (self.at - 20, self.at + 20, self.at + 40):
            self.store.apply([dict(metadata, updated_at=stamp)])
            saved = self.store.get(old["key"])
            self.assertTrue(saved["history_checking"])
            self.assertEqual(saved["status"], "disconnected")
            self.assertEqual(self.store.transcript_revision("thread-1"), revision)
            self.assertNotIn("history_revision", saved)

    def test_legacy_backdated_history_cannot_restore_stale_ready_after_restart(self):
        ready = dict(self.observation("ready", "complete", self.at + 20), provider="claude")
        self.store.apply([ready])
        old = self.store.get("claude:thread-1")
        self.store.apply([dict(ready, history_checking=True)])
        restart = BoardStore(self.path)
        revision = {"file_id": "a" * 64, "size": 100, "sha256": "b" * 64, "generation": "c" * 64}
        older = dict(ready, status="cooking", event_id="older-new-turn", updated_at=self.at - 20,
                     history_verified=True, history_revision=revision)
        restart.apply([older])
        saved = restart.get(old["key"])
        self.assertTrue(saved["history_checking"])
        self.assertEqual(saved["event_id"], old["event_id"])
        self.assertEqual(saved["status"], "disconnected")
        self.assertEqual(restart.transcript_revision("thread-1"), revision)

    def test_malformed_persisted_revision_cannot_crash_or_authorize_an_older_turn(self):
        ready = dict(self.observation("ready", "complete", self.at + 20), provider="claude")
        revision = {"file_id": "a" * 64, "size": 100, "sha256": "b" * 64, "generation": "c" * 64}
        for malformed in ("broken", [revision], {}, dict(revision, size=True), dict(revision, sha256="invalid")):
            with self.subTest(malformed=malformed):
                self.store.apply([ready])
                old = self.store.get("claude:thread-1")
                with self.store.connect() as db:
                    db.execute("UPDATE sessions SET extra=? WHERE key=?",
                               (json.dumps({"history_checking": True, "history_revision": malformed}), old["key"]))
                self.store.apply([dict(ready, status="cooking", event_id="older-new-turn", updated_at=self.at - 20,
                                       history_verified=True, history_revision=revision)])
                saved = self.store.get(old["key"])
                self.assertTrue(saved["history_checking"])
                self.assertEqual(saved["event_id"], old["event_id"])
                self.assertEqual(self.store.transcript_revision("thread-1"), revision)

    def test_old_completed_chats_stay_out_but_current_work_appears(self):
        self.store.apply([self.observation("ready", "old-complete", self.at - 1000, "old"),
                          self.observation("cooking", "old-start", self.at - 20, "working"),
                          self.observation("needs_you", "waiting", self.at - 60, "waiting")])
        active = self.store.snapshot()["sessions"]
        self.assertEqual({x["id"] for x in active}, {"working", "waiting"})
        self.assertEqual(active[0]["status"], "needs_you")

    def test_provider_failure_preserves_ready_and_unconfirms_running(self):
        self.store.apply([self.observation("ready", "complete", ident="finished"),
                          self.observation("cooking", "start", ident="working")])
        self.store.mark_unavailable("codex", "Unavailable")
        states = {x["id"]: x["status"] for x in self.store.snapshot()["sessions"]}
        self.assertEqual(states, {"finished": "ready", "working": "disconnected"})

    def test_old_live_waits_appear_but_idle_completed_history_waits_for_new_work(self):
        old_wait = self.observation("needs_you", "waiting-for-three-days", self.at - 3 * 86400, "waiting")
        old_wait["confirmed_live"] = True
        open_reply = self.observation("ready", "open-but-overlooked", self.at - 2 * 3600, "open")
        open_reply["confirmed_live"] = True
        old_reply = self.observation("ready", "closed-history", self.at - 2 * 3600, "closed")
        self.store.apply([old_wait, open_reply, old_reply])
        self.assertEqual({x["id"] for x in self.store.snapshot()["sessions"]}, {"waiting"})
        new_turn = self.observation("cooking", "new-user-turn", self.at + 1, "open")
        self.store.apply([new_turn])
        self.assertEqual({x["id"] for x in self.store.snapshot()["sessions"]}, {"waiting", "open"})

    def test_background_routing_preserves_access_reviews_and_survives_restart(self):
        worker = dict(self.observation("ready", "worker-complete", ident="worker"), provider="claude")
        real = dict(self.observation("ready", "real-complete", ident="real"), provider="claude")
        reviewed = dict(self.observation("ready", "reviewed-complete", ident="reviewed"), provider="claude")
        self.store.apply([worker, real, reviewed])
        item = self.store.get("claude:reviewed")
        self.store.review(item["key"], item["event_id"])
        self.store.set_background_sessions("claude", {"worker": "project_worker", "reviewed": "project_worker"})
        restarted = BoardStore(self.path)
        snapshot = restarted.snapshot()
        self.assertEqual([x["id"] for x in snapshot["sessions"]], ["real"])
        self.assertFalse(snapshot["history"])
        self.assertEqual({x["id"] for x in snapshot["background"]}, {"worker", "reviewed"})
        self.assertEqual(restarted.get("claude:worker")["background_reason"], "project_worker")
        self.assertEqual(restarted.session_ids("claude"), {"real", "worker", "reviewed"})
        with restarted.connect() as db:
            saved = db.execute("SELECT * FROM sessions WHERE key=?", (item["key"],)).fetchone()
        self.assertEqual(saved["reviewed_event"], item["event_id"])
        self.assertEqual(saved["status"], "ready")
        # The source can later establish eligibility, e.g. when a delayed
        # Desktop mapping arrives. The original acknowledgement remains valid.
        restarted.apply([reviewed])
        self.assertEqual([x["id"] for x in restarted.snapshot()["history"]], ["reviewed"])

    def test_monitor_moves_previously_imported_workers_to_background(self):
        worker = dict(self.observation("ready", "worker-complete", ident="worker"), provider="claude")
        real = dict(self.observation("ready", "real-complete", ident="real"), provider="claude")

        class DesktopClaude(ClaudeSource):
            def scan(self, now, cutoff):
                self.background_reasons = {"worker": "project_worker"}
                return [real], {"available": True}

        monitor = Monitor(self.path.parent, codex_root=self.path.parent / "codex",
                          claude_root=self.path.parent / "claude")
        monitor.store.apply([worker, real])
        monitor.sources = [DesktopClaude(self.path.parent / "claude")]
        monitor.scan()
        self.assertEqual([x["id"] for x in monitor.snapshot()["sessions"]], ["real"])
        self.assertEqual([x["id"] for x in monitor.snapshot()["background"]], ["worker"])

    def test_background_work_keeps_updating_without_entering_main_queue(self):
        worker = dict(self.observation("cooking", "start", ident="worker"),
                      provider="claude", background_reason="internal_project_worker")
        self.store.apply([worker])
        worker.update(status="ready", event_id="complete", updated_at=self.at + 2)
        self.store.apply([worker])
        snapshot = BoardStore(self.path).snapshot()
        self.assertFalse(snapshot["sessions"])
        self.assertFalse(snapshot["history"])
        self.assertEqual(sum(snapshot["counts"].values()), 0)
        self.assertEqual(snapshot["background"][0]["status"], "ready")

    def test_name_and_placement_overrides_survive_polling_and_restart(self):
        source = dict(self.observation("ready", "complete"), provider="claude",
                      background_reason="internal_project_worker")
        self.store.apply([source])
        key = "claude:thread-1"
        self.store.rename(key, "My active project")
        self.store.move(key, "board")
        source["title"] = "A changed source title"
        self.store.apply([source])
        reopened = BoardStore(self.path)
        item = reopened.snapshot()["sessions"][0]
        self.assertEqual(item["title"], "My active project")
        self.assertEqual(item["automatic_title"], "A changed source title")
        self.assertFalse(reopened.snapshot()["background"])
        reopened.move(key, "auto")
        reopened.rename(key, None)
        background = reopened.snapshot()["background"][0]
        self.assertEqual(background["title"], "A changed source title")
        self.assertFalse(background["notification_eligible"])

    def test_snooze_is_revision_checked_and_does_not_silence_a_new_reply(self):
        self.store.apply([self.observation("ready", "complete")])
        item = self.store.get("codex:thread-1")
        self.store.snooze(item["key"], item["event_id"], now=time.time())
        snoozed = BoardStore(self.path).get(item["key"])
        self.assertFalse(snoozed["notification_eligible"])
        self.assertGreater(snoozed["snoozed_until"], time.time())
        with self.store.connect() as db:
            db.execute("UPDATE session_preferences SET snoozed_until=? WHERE key=?", (time.time() - 1, item["key"]))
        expired = self.store.get(item["key"])
        self.assertTrue(expired["notification_eligible"])
        self.assertGreater(expired["snoozed_until"], 0)
        self.store.apply([self.observation("ready", "new-complete", self.at + 2)])
        newer = self.store.get(item["key"])
        self.assertEqual(newer["snoozed_until"], 0)
        self.assertTrue(newer["notification_eligible"])
        with self.assertRaises(Conflict):
            self.store.snooze(item["key"], item["event_id"])

    def test_waiting_clock_and_preview_are_preserved_during_metadata_refresh(self):
        source = self.observation("ready", "complete", self.at + 1)
        source.update(preview="The requested draft is ready.", preview_kind="reply", preview_at=self.at + 1)
        self.store.apply([source])
        source.update(updated_at=self.at + 10, title="Renamed by the source")
        self.store.apply([source])
        item = self.store.get("codex:thread-1")
        self.assertEqual(item["attention_since"], self.at + 1)
        self.assertEqual(item["preview"], "The requested draft is ready.")

    def test_project_history_is_accessible_without_entering_main_queue(self):
        old = dict(self.observation("ready", "old", self.at - 86400, "project:older"),
                   provider="claude", source_kind="project", background_reason="older_project_thread")
        self.store.apply([old])
        self.assertFalse(self.store.snapshot()["sessions"])
        self.assertEqual(self.store.snapshot()["background"][0]["id"], "project:older")

    def test_local_scan_does_not_unconfirm_project_parents(self):
        parent = dict(self.observation("needs_you", "parent-prompt", ident="project:parent"),
                      provider="claude", source_kind="project")

        class EmptyLocalClaude(ClaudeSource):
            def scan(self, now, cutoff):
                self.observed_tracked = set(self.tracked_ids)
                return [], {"available": True}

        monitor = Monitor(self.path.parent, codex_root=self.path.parent / "codex", claude_root=self.path.parent / "claude")
        local = EmptyLocalClaude(self.path.parent / "claude")
        monitor.sources = [local]
        monitor.store.apply([parent])
        monitor.scan()
        self.assertEqual(monitor.store.get("claude:project:parent")["status"], "needs_you")
        self.assertNotIn("project:parent", local.observed_tracked)

    def test_partial_project_read_keeps_confirmed_rows_and_unconfirms_only_missing_work(self):
        valid = dict(self.observation("cooking", "working", ident="project:valid"), provider="claude", source_kind="project")
        missing = dict(valid, id="project:missing")
        local = dict(self.observation("needs_you", "local-prompt", ident="local"), provider="claude")

        class PartialProjects:
            def scan(self, now, cutoff):
                return [valid], {"id": "claude_projects", "available": False}

        monitor = Monitor(self.path.parent, codex_root=self.path.parent / "codex", claude_root=self.path.parent / "claude")
        monitor.store.apply([valid, missing, local])
        monitor.project_source = PartialProjects()
        monitor.scan_projects()
        self.assertEqual(monitor.store.get("claude:project:valid")["status"], "cooking")
        self.assertEqual(monitor.store.get("claude:project:missing")["status"], "disconnected")
        self.assertEqual(monitor.store.get("claude:local")["status"], "needs_you")

    def test_same_raw_event_with_new_status_has_a_different_review_revision(self):
        self.store.apply([self.observation("ready", "same")])
        old = self.store.snapshot()["sessions"][0]
        self.store.apply([self.observation("needs_you", "same", self.at + 2)])
        with self.assertRaises(Conflict):
            self.store.review(old["key"], old["event_id"])

    def test_older_disconnected_observation_preserves_completed_reply_and_review(self):
        self.store.apply([self.observation("ready", "complete", self.at + 3)])
        completed = self.store.snapshot()["sessions"][0]
        self.store.review(completed["key"], completed["event_id"])
        self.store.apply([self.observation("disconnected", "unreadable", self.at + 2)])
        restored = BoardStore(self.path).snapshot()
        self.assertFalse(restored["sessions"])
        self.assertEqual(restored["history"][0]["status"], "ready")
        self.assertEqual(restored["history"][0]["event_id"], completed["event_id"])

    def test_title_and_exact_link_refresh_preserve_review_and_newer_turn(self):
        self.store.apply([self.observation("ready", "complete", self.at + 3)])
        ready = self.store.snapshot()["sessions"][0]
        self.store.review(ready["key"], ready["event_id"])
        metadata = self.observation("disconnected", "old-source", self.at + 2)
        metadata.update(title="A recognizable chat title", open_kind="chat", open_label="Open Codex",
                        open_url="codex://threads/thread-1", title_source="desktop")
        self.store.apply([metadata])
        stored = self.store.get(ready["key"])
        self.assertEqual(stored["title"], "A recognizable chat title")
        self.assertEqual(stored["open_url"], "codex://threads/thread-1")
        self.assertEqual(stored["status"], "ready")
        self.assertEqual(stored["event_id"], ready["event_id"])
        self.assertFalse(self.store.snapshot()["sessions"])

    def test_usage_is_disabled_for_disposable_sources(self):
        monitor = Monitor(self.path.parent, codex_root=self.path.parent / "codex",
                          claude_root=self.path.parent / "claude")
        self.assertIsNone(monitor.usage_source)
        self.assertEqual({x["id"] for x in monitor.snapshot()["usage"]}, {"claude", "codex"})
        self.assertTrue(all(not x["loading"] for x in monitor.snapshot()["usage"]))

    def test_legacy_unlinked_claude_row_gets_identifiable_title_without_losing_review(self):
        old = self.observation("ready", "completed")
        old.update(provider="claude", title="bureau-obscura-93", cwd="/projects/bureau-obscura",
                   open_kind="app", open_label="Open Claude")
        self.store.apply([old])
        before = self.store.get("claude:thread-1")
        self.store.review(before["key"], before["event_id"])
        migrated = BoardStore(self.path)
        after = migrated.get(before["key"])
        self.assertTrue(after["title"].startswith("Bureau obscura · "))
        self.assertFalse(after["can_open"])
        self.assertEqual(after["status"], "ready")
        self.assertEqual(after["event_id"], before["event_id"])
        self.assertFalse(migrated.snapshot()["sessions"])

    def test_provider_failure_keeps_reviewed_waiting_session_in_history(self):
        self.store.apply([self.observation("needs_you", "waiting")])
        waiting = self.store.snapshot()["sessions"][0]
        self.store.review(waiting["key"], waiting["event_id"])
        self.store.mark_unavailable("codex", "Unavailable")
        snapshot = BoardStore(self.path).snapshot()
        self.assertFalse(snapshot["sessions"])
        self.assertEqual(snapshot["history"][0]["status"], "needs_you")
        self.assertEqual(snapshot["history"][0]["event_id"], waiting["event_id"])

    def test_healthy_scan_unconfirms_missing_work_but_preserves_present_ready_and_history(self):
        current = self.observation("cooking", "present-start", ident="present")

        class PartialCodex(CodexSource):
            def scan(self, now, cutoff):
                return [current], {"available": True}

        monitor = Monitor(self.path.parent,
                          codex_root=self.path.parent / "codex",
                          claude_root=self.path.parent / "claude")
        monitor.store.apply([
            current,
            self.observation("cooking", "missing-start", ident="missing"),
            self.observation("needs_you", "missing-wait", ident="waiting"),
            self.observation("ready", "complete", ident="finished"),
            self.observation("needs_you", "reviewed-wait", ident="reviewed"),
        ])
        reviewed = monitor.store.get("codex:reviewed")
        monitor.store.review(reviewed["key"], reviewed["event_id"])
        monitor.sources = [PartialCodex(self.path.parent / "codex")]
        monitor.scan()
        snapshot = monitor.snapshot()
        states = {item["id"]: item["status"] for item in snapshot["sessions"]}
        self.assertEqual(states, {
            "present": "cooking", "missing": "disconnected",
            "waiting": "disconnected", "finished": "ready",
        })
        self.assertIn("not found", monitor.store.get("codex:missing")["detail"])
        self.assertEqual([item["id"] for item in snapshot["history"]], ["reviewed"])

    def test_desktop_metadata_refresh_does_not_confirm_missing_work(self):
        working = self.observation("cooking", "working")
        working.update(provider="claude")
        caption = dict(working, status="disconnected", updated_at=0,
                       metadata_only=True, title="Recognizable task caption")

        class MetadataOnlyClaude(ClaudeSource):
            def scan(self, now, cutoff):
                return [caption], {"available": True}

        monitor = Monitor(self.path.parent, codex_root=self.path.parent / "codex",
                          claude_root=self.path.parent / "claude")
        monitor.store.apply([working])
        monitor.sources = [MetadataOnlyClaude(self.path.parent / "claude")]
        monitor.scan()
        saved = monitor.store.get("claude:thread-1")
        self.assertEqual(saved["title"], "Recognizable task caption")
        self.assertEqual(saved["status"], "disconnected")

    def test_old_transcript_cannot_confirm_a_newer_cached_working_turn(self):
        current = self.observation("cooking", "current-turn", self.at + 3)
        current.update(provider="claude")
        old = dict(current, status="ready", updated_at=self.at + 1,
                   title="Updated title from an older transcript")

        class OlderClaude(ClaudeSource):
            def scan(self, now, cutoff):
                return [old], {"available": True}

        monitor = Monitor(self.path.parent, codex_root=self.path.parent / "codex",
                          claude_root=self.path.parent / "claude")
        monitor.store.apply([current])
        monitor.sources = [OlderClaude(self.path.parent / "claude")]
        monitor.scan()
        saved = monitor.store.get("claude:thread-1")
        self.assertEqual(saved["title"], old["title"])
        self.assertEqual(saved["status"], "disconnected")
        self.assertEqual(saved["updated_at"], current["updated_at"])


if __name__ == "__main__":
    unittest.main()
