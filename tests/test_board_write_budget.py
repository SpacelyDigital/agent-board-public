"""Idle polling must not rewrite saved state or weaken its recovery guarantees."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from board_store import BoardStore
from codex_source import CodexSource
from server import Monitor


class BoardWriteBudgetTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "board.sqlite3"
        self.store = BoardStore(self.path)
        self.addCleanup(self.store.close)
        self.now = self.store.activated_at + 1

    def observation(self, ident="fixture", **changes):
        row = dict(provider="codex", id=ident, title="Disposable fixture", status="ready",
                   detail="A completed fixture reply.", updated_at=self.now, event_id="reply-1")
        row.update(changes)
        return row

    def persisted_time(self, key="codex:fixture"):
        with self.store.connect() as db:
            return db.execute("SELECT observed_at FROM sessions WHERE key=?", (key,)).fetchone()[0]

    def test_200_unchanged_rows_50_polls_have_no_changes_or_wal_growth(self):
        rows = [self.observation(str(i), **({"background_reason": "fixture"} if i < 50 else {}))
                for i in range(200)]
        self.store.apply(rows, self.now)
        changes = self.store._db.total_changes
        wal = Path(str(self.path) + "-wal")
        before = (wal.stat().st_ino, wal.stat().st_size, wal.stat().st_mtime_ns,
                  self.path.stat().st_mtime_ns)
        connection = self.store._db
        statements = []
        connection.set_trace_callback(statements.append)
        for poll in range(1, 51):
            self.store.apply(rows, self.now + poll)
            self.store.set_background_sessions("codex", {str(i): "fixture" for i in range(50)})
            self.store.mark_unavailable("codex", "Unavailable", {"codex:" + str(i) for i in range(200)})
        connection.set_trace_callback(None)
        self.assertIs(self.store._db, connection)
        self.assertEqual(connection.total_changes, changes)
        self.assertEqual(before, (wal.stat().st_ino, wal.stat().st_size, wal.stat().st_mtime_ns,
                                  self.path.stat().st_mtime_ns))
        self.assertFalse(any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements))
        self.assertEqual(self.store.get("codex:100")["observed_at"], self.now + 50)
        self.assertEqual(self.persisted_time("codex:100"), self.now)

    def test_freshness_is_live_in_memory_and_persisted_once_per_minute(self):
        row = self.observation()
        self.store.apply([row], self.now)
        changes = self.store._db.total_changes
        for seconds in (10, 30, 59):
            self.store.apply([row], self.now + seconds)
            self.assertEqual(self.store.get("codex:fixture")["observed_at"], self.now + seconds)
            self.assertEqual(self.store.snapshot()["sessions"][0]["observed_at"], self.now + seconds)
        self.assertEqual(self.store._db.total_changes, changes)
        self.store.apply([row], self.now + 60)
        self.assertEqual(self.store._db.total_changes, changes + 1)
        self.store.apply([row], self.now + 119)
        self.assertEqual(self.store._db.total_changes, changes + 1)
        self.assertEqual(self.persisted_time(), self.now + 60)
        self.store.close()
        reopened = BoardStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get("codex:fixture")["observed_at"], self.now + 60)
        self.assertEqual(reopened._db.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_semantic_change_persists_immediately_without_losing_review(self):
        row = self.observation()
        self.store.apply([row], self.now)
        saved = self.store.get("codex:fixture")
        self.store.review(saved["key"], saved["event_id"])
        self.store.apply([dict(row, title="Changed fixture name")], self.now + 2)
        current = self.store.get(saved["key"])
        self.assertEqual(current["reviewed_event"], saved["event_id"])
        self.assertEqual(current["observed_at"], self.now + 2)
        self.assertEqual(self.persisted_time(), self.now + 2)
        self.store.apply([dict(row, status="cooking", event_id="next-turn", updated_at=self.now + 3)], self.now + 3)
        self.assertEqual(self.store.get(saved["key"])["status"], "cooking")

    def test_metadata_and_checking_noops_preserve_lifecycle_and_freshness_rules(self):
        row = self.observation()
        self.store.apply([row], self.now)
        saved = self.store.get("codex:fixture")
        metadata = dict(row, metadata_only=True, title="Metadata title", updated_at=self.now - 1)
        self.store.apply([metadata], self.now + 1)
        changes = self.store._db.total_changes
        self.store.apply([metadata], self.now + 2)
        self.assertEqual(self.store._db.total_changes, changes)
        self.assertEqual(self.store.get(saved["key"])["observed_at"], self.now)
        checking = dict(row, history_checking=True, status="disconnected", detail="Checking fixture history")
        self.store.apply([checking], self.now + 3)
        changes = self.store._db.total_changes
        self.store.apply([checking], self.now + 4)
        self.assertEqual(self.store._db.total_changes, changes)
        current = self.store.get(saved["key"])
        self.assertEqual(current["event_id"], saved["event_id"])
        self.assertTrue(current["history_checking"])
        self.assertFalse(current["notification_eligible"])
        self.assertEqual(current["observed_at"], self.now + 4)

    def test_background_and_unavailable_repeat_without_writing(self):
        row = self.observation(status="cooking")
        self.store.apply([row], self.now)
        self.store.set_background_sessions("codex", {"fixture": "internal"}, self.now)
        changes = self.store._db.total_changes
        self.store.set_background_sessions("codex", {"fixture": "internal", "missing": "internal"}, self.now + 1)
        self.assertEqual(self.store._db.total_changes, changes)
        self.store.apply([row], self.now + 2)
        changes = self.store._db.total_changes
        self.store.apply([row], self.now + 3)
        self.assertEqual(self.store._db.total_changes, changes)
        self.store.mark_unavailable("codex", "Source unavailable")
        changes = self.store._db.total_changes
        self.store.mark_unavailable("codex", "Source unavailable")
        self.assertEqual(self.store._db.total_changes, changes)

    def test_failed_transaction_does_not_publish_freshness_and_can_retry(self):
        rows = [self.observation("first"), self.observation("second")]
        self.store.apply(rows, self.now)
        with self.store.connect() as db:
            db.execute("""CREATE TRIGGER fixture_full BEFORE UPDATE ON sessions
                WHEN new.id='second' BEGIN SELECT RAISE(ABORT,'fixture storage failure'); END""")
        changed = [rows[0], dict(rows[1], title="Changed title")]
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.apply(changed, self.now + 20)
        self.assertEqual(self.store.get("codex:first")["observed_at"], self.now)
        self.assertEqual(self.store.get("codex:second")["title"], rows[1]["title"])
        self.assertFalse(self.store._db.in_transaction)
        with self.store.connect() as db:
            db.execute("DROP TRIGGER fixture_full")
        self.store.apply(changed, self.now + 21)
        self.assertEqual(self.store.get("codex:first")["observed_at"], self.now + 21)
        self.assertEqual(self.store.get("codex:second")["title"], "Changed title")

    def test_project_live_timestamp_uses_memory_fence_not_stale_disk_timestamp(self):
        row = self.observation("project:fixture", provider="claude", source_kind="project")
        self.store.apply([row], self.now)
        self.store.apply([row], self.now + 20)
        self.assertEqual(self.persisted_time("claude:project:fixture"), self.now)
        stale = dict(row, status="cooking", event_id="resume", updated_at=self.now - 5,
                     confirmed_live=True, project_status_observed_at=self.now + 10)
        self.store.apply([stale], self.now + 21)
        self.assertEqual(self.store.get("claude:project:fixture")["status"], "ready")
        self.store.apply([dict(stale, project_status_observed_at=self.now + 22)], self.now + 22)
        self.assertEqual(self.store.get("claude:project:fixture")["status"], "cooking")

    def test_batched_get_uses_one_select_and_preserves_preferences(self):
        rows = [self.observation(str(i)) for i in range(200)]
        self.store.apply(rows, self.now)
        self.store.rename("codex:0", "Saved name")
        self.store.move("codex:1", "background")
        statements = []
        self.store._db.set_trace_callback(statements.append)
        found = self.store.get_many("codex:" + str(i) for i in range(200))
        self.store._db.set_trace_callback(None)
        self.assertEqual(len(found), 200)
        self.assertEqual(sum(s.startswith("SELECT") for s in statements), 1)
        self.assertEqual(found["codex:0"]["title"], "Saved name")
        self.assertEqual(found["codex:1"]["background_reason"], "manual")

    def test_polling_reading_and_review_share_connection_without_losing_ack(self):
        row = self.observation()
        self.store.apply([row], self.now)
        event = self.store.get("codex:fixture")["event_id"]
        errors = []
        def run(action):
            try:
                for _ in range(50):
                    action()
            except BaseException as exc:
                errors.append(type(exc).__name__)
        jobs = [lambda: self.store.apply([row], self.now + 20),
                lambda: self.store.review("codex:fixture", event), self.store.snapshot]
        threads = [threading.Thread(target=run, args=(job,)) for job in jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.store.get("codex:fixture")["reviewed_event"], event)
        self.assertFalse(self.store._db.in_transaction)

    def test_corruption_recovery_preserves_uncheckpointed_wal(self):
        if not hasattr(sqlite3.Connection, "setconfig"):
            self.skipTest("SQLite close-checkpoint control requires Python3.12")
        row = self.observation()
        self.store.apply([row], self.now)
        self.store._db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
        self.store.close()
        wal = Path(str(self.path) + "-wal")
        before = {p.name: p.read_bytes() for p in (self.path, wal)}
        class IntegrityFailure(BoardStore):
            failed_once = False
            def _initialize(self):
                if not self.failed_once:
                    self.failed_once = True
                    with self.connect() as db:
                        db.execute("SELECT count(*) FROM sessions").fetchone()
                    error = sqlite3.DatabaseError("Fixture integrity failure")
                    error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
                    raise error
                super()._initialize()
        recovered = IntegrityFailure(self.path)
        self.addCleanup(recovered.close)
        copies = next(self.path.parent.glob("recovery/*/copies"))
        for name, data in before.items():
            self.assertEqual((copies / name).read_bytes(), data)
        self.assertIn("original files", recovered.recovery_notice)
        recovered.close()
        reopened = BoardStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.recovery_notice, recovered.recovery_notice)

    def test_close_is_idempotent_and_does_not_silently_reopen(self):
        self.store.close()
        self.store.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            self.store.snapshot()

    def test_monitor_reads_scan_rows_in_one_batch(self):
        root = Path(self.folder.name)
        monitor = Monitor(root / "monitor", codex_root=root / "codex", claude_root=root / "claude")
        self.addCleanup(monitor.store.close)
        class Fixture(CodexSource):
            def __init__(self):
                pass
            def scan(self, now, cutoff):
                return [dict(provider="codex", id=str(i), title="Fixture", status="cooking",
                             updated_at=now, event_id="work") for i in range(200)], {
                                 "available": True, "last_seen": now}
        monitor.sources = [Fixture()]
        with patch.object(monitor.store, "get", side_effect=AssertionError("per-row get")), \
                patch.object(monitor.store, "get_many", wraps=monitor.store.get_many) as batch:
            monitor.scan()
        batch.assert_called_once()
        self.assertEqual(len(monitor.store.snapshot()["sessions"]), 200)


if __name__ == "__main__":
    unittest.main()
