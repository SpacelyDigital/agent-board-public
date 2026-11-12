"""Fault injection into private disposable board databases, never owner data."""

import os
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

from board_store import BoardStore


class StoreRedTeamTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "data" / "board.sqlite3"
        self.path.parent.mkdir(mode=0o700)

    def observation(self, **changes):
        result = dict(provider="codex", id="fixture-chat", title="Disposable recovery test",
                      cwd="/tmp/fixture", status="ready", updated_at=time.time() + 1,
                      event_id="reply-1", can_open=False)
        result.update(changes)
        return result

    def test_confirmed_corruption_preserves_bytes_and_explains_new_board_after_restart(self):
        damaged = b"This is deliberately not a SQLite database.\n" * 100
        self.path.write_bytes(damaged)
        self.path.chmod(0o600)
        recovered = BoardStore(self.path)
        self.assertEqual(recovered.snapshot()["sessions"], [])
        self.assertIn("original files were preserved", recovered.snapshot()["recovery_notice"])
        backups = list((self.path.parent / "recovery").glob("*/board.sqlite3"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), damaged)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backups[0].parent.stat().st_mode), 0o700)
        recovered.apply([self.observation()])
        restarted = BoardStore(self.path)
        self.assertEqual(len(restarted.snapshot()["sessions"]), 1)
        self.assertEqual(restarted.recovery_notice, recovered.recovery_notice)
        self.assertEqual(len(list((self.path.parent / "recovery").glob("*/board.sqlite3"))), 1)

    def test_corruption_copy_failure_does_not_replace_the_original(self):
        damaged = b"damaged private database"
        self.path.write_bytes(damaged)
        with patch("board_store.shutil.copyfileobj", side_effect=OSError("injected disk full")):
            with self.assertRaises(OSError):
                BoardStore(self.path)
        self.assertEqual(self.path.read_bytes(), damaged)

    def test_invalid_activation_metadata_is_backed_up_and_repaired_without_losing_chat_state(self):
        store = BoardStore(self.path)
        store.apply([self.observation()])
        store.rename("codex:fixture-chat", "My saved title")
        item = store.get("codex:fixture-chat")
        store.review(item["key"], item["event_id"])
        with store.connect() as db:
            db.execute("UPDATE metadata SET value='NaN' WHERE key='activated_at'")
        restarted = BoardStore(self.path)
        json.dumps(restarted.snapshot(), allow_nan=False)
        self.assertEqual(restarted.snapshot()["history"][0]["title"], "My saved title")
        self.assertIn("Existing chats and preferences were kept", restarted.recovery_notice)
        backups = list((self.path.parent / "recovery").glob("*/board-before-metadata-repair.sqlite3"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as preserved:
            self.assertEqual(preserved.execute("SELECT value FROM metadata WHERE key='activated_at'").fetchone()[0], "NaN")
            self.assertEqual(preserved.execute("SELECT title_override FROM session_preferences").fetchone()[0], "My saved title")
        self.assertEqual(BoardStore(self.path).recovery_notice, restarted.recovery_notice)

    def test_substituted_recovery_directory_is_rejected_without_following_it(self):
        damaged = b"damaged private database"
        self.path.write_bytes(damaged)
        alternate = Path(self.directory.name) / "not-board-data"
        alternate.mkdir(mode=0o755)
        original_mode = stat.S_IMODE(alternate.stat().st_mode)
        (self.path.parent / "recovery").symlink_to(alternate, target_is_directory=True)
        with self.assertRaises(PermissionError):
            BoardStore(self.path)
        self.assertEqual(self.path.read_bytes(), damaged)
        self.assertEqual(list(alternate.iterdir()), [])
        self.assertEqual(stat.S_IMODE(alternate.stat().st_mode), original_mode)

    def test_partial_move_and_replacement_failures_roll_back_all_originals(self):
        for failure_number in (2, 4):
            with self.subTest(failure_number=failure_number):
                store = BoardStore(self.path)
                store.apply([self.observation()])
                # Model a stopped damaged database, rather than overwriting
                # SQLite's live memory-mapped WAL beneath an open connection.
                store._close_connection()
                originals = {self.path: self.path.read_bytes()}
                for suffix in ("-wal", "-shm"):
                    path = Path(str(self.path) + suffix)
                    path.write_bytes(("disposable " + suffix).encode())
                    path.chmod(0o600)
                    originals[path] = path.read_bytes()
                real_replace = os.replace
                calls = []
                def injected_replace(source, target):
                    calls.append((source, target))
                    if len(calls) == failure_number:
                        raise OSError("injected replacement failure")
                    return real_replace(source, target)
                with patch("board_store.os.replace", side_effect=injected_replace):
                    with self.assertRaises(OSError):
                        store._recover_corrupt_database()
                for path, content in originals.items():
                    self.assertEqual(path.read_bytes(), content)
                for suffix in ("-wal", "-shm"):
                    Path(str(self.path) + suffix).unlink()

    def test_busy_database_is_not_quarantined_or_reset(self):
        store = BoardStore(self.path)
        store.apply([self.observation()])
        lock = sqlite3.connect(self.path)
        try:
            lock.execute("BEGIN IMMEDIATE")
            lock.execute("UPDATE metadata SET value=value")
            # WAL permits a healthy existing board to reopen for reads while
            # another writer is active. A write must still fail without reset.
            reopened = BoardStore(self.path)
            with self.assertRaises(sqlite3.OperationalError) as failure:
                reopened.rename("codex:fixture-chat", "Blocked fixture edit")
            self.assertEqual(failure.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            self.assertFalse((self.path.parent / "recovery").exists())
        finally:
            lock.rollback()
            lock.close()
        self.assertEqual(len(BoardStore(self.path).snapshot()["sessions"]), 1)

    def test_unreadable_persisted_extra_does_not_break_the_board_or_override_status(self):
        store = BoardStore(self.path)
        store.apply([self.observation()])
        for extra in ("[]", "null", "{bad json", '{"status":"bogus","key":"hijacked","preview":{}}'):
            with store.connect() as db:
                db.execute("UPDATE sessions SET extra=?", (extra,))
            current = BoardStore(self.path).snapshot()["sessions"][0]
            self.assertEqual(current["status"], "ready")
            self.assertEqual(current["key"], "codex:fixture-chat")

    def test_malformed_observations_and_surrogates_are_bounded(self):
        store = BoardStore(self.path)
        invalid = [None, [], "bad", self.observation(status=[]), self.observation(updated_at="NaN"),
                   self.observation(updated_at=10 ** 1000), self.observation(id="bad\ud800id")]
        store.apply(invalid)
        self.assertEqual(store.snapshot()["sessions"], [])
        store.apply([self.observation(title="lone \ud800 surrogate", preview="x" * 1000000,
                                      preview_at=10 ** 1000, open_url={"unsafe": "value"})])
        current = store.snapshot()["sessions"][0]
        self.assertEqual(current["title"], "lone ? surrogate")
        self.assertEqual(len(current["preview"]), 2400)
        self.assertNotIn("preview_at", current)
        self.assertNotIn("open_url", current)
        store.rename(current["key"], "renamed \ud800")
        self.assertEqual(store.get(current["key"])["title"], "renamed ?")

    def test_overlong_event_ids_cannot_collide_by_truncation(self):
        store = BoardStore(self.path)
        for ending in ("old", "new"):
            self.assertIsNone(store.normalize(self.observation(event_id="x" * 4096 + ending), time.time()))
        first = store.normalize(self.observation(event_id="event-\ud800"), time.time())
        second = store.normalize(self.observation(event_id="event-?"), time.time())
        self.assertNotEqual(first["event_id"], second["event_id"])

    def test_private_modes_and_substituted_database_paths(self):
        store = BoardStore(self.path)
        self.path.chmod(0o644)
        BoardStore(self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        alternate = self.path.with_name("other.sqlite3")
        alternate.symlink_to(self.path)
        with self.assertRaises(OSError):
            BoardStore(alternate)
        hardlink = self.path.with_name("hardlink.sqlite3")
        os.link(self.path, hardlink)
        with self.assertRaises(PermissionError):
            BoardStore(hardlink)


if __name__ == "__main__":
    unittest.main()
