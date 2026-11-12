"""Exercise checkpointed index fallback without granting source writes."""

import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import codex_source
from codex_source import CodexSource


class CodexIndexSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        self.root, self.private = base / "source", base / "private"
        self.root.mkdir(mode=0o700)
        self.private.mkdir(mode=0o700)
        self.database = self.root / "state_5.sqlite"
        self.original_connect = sqlite3.connect
        connection = self.original_connect(self.database)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, updated_at INTEGER, title TEXT, archived INTEGER)")
        connection.execute("CREATE TABLE thread_spawn_edges (child_thread_id TEXT)")
        connection.executemany("INSERT INTO threads VALUES (?,?,?,?,?)", [
            ("live", str(self.root / "sessions/live.jsonl"), 100, "Disposable live chat", 0),
            ("archived", str(self.root / "sessions/old.jsonl"), 100, "Disposable archived chat", 1)])
        connection.execute("INSERT INTO thread_spawn_edges VALUES ('child')")
        connection.commit()
        connection.close()  # Unlike transaction context exit, this removes WAL/SHM.
        self.original_bytes = self.database.read_bytes()
        self.source = self.reader()

    def reader(self, root=None, **settings):
        options = {"confine_paths": True, "snapshot_parent": self.private, **settings}
        source = CodexSource(root or self.root, **options)
        self.addCleanup(source.close)
        return source

    def fresh_reader(self):
        self.source.close()
        self.source = self.reader()

    def blocked(self, code=sqlite3.SQLITE_CANTOPEN):
        error = sqlite3.OperationalError("Disposable source open failure")
        error.sqlite_errorcode = code
        def connect(database, *args, **kwargs):
            if database == self.database.as_uri() + "?mode=ro":
                raise error
            return self.original_connect(database, *args, **kwargs)
        return patch("codex_source.sqlite3.connect", side_effect=connect)

    def assert_clean(self):
        self.assertEqual(list(self.private.iterdir()), [])

    def test_closed_wal_index_and_archive_exclusions_read_from_private_copy(self):
        self.assertFalse((self.root / "state_5.sqlite-wal").exists())
        with self.blocked():
            rows, error = self.source._thread_rows(0)
            excluded = self.source._excluded_ids()
        self.assertIsNone(error)
        self.assertEqual([row["id"] for row in rows], ["live"])
        self.assertEqual(excluded, {"archived", "child"})
        self.assertEqual(self.database.read_bytes(), self.original_bytes)
        self.assertEqual({path.name for path in self.root.iterdir()}, {"state_5.sqlite"})
        self.assert_clean()

    def test_thirty_unchanged_scans_copy_once_and_share_one_index_transaction(self):
        original_copy, original_write = codex_source._private_index_snapshot, os.write
        written, transactions = [], []
        def write(descriptor, data):
            result = original_write(descriptor, data)
            written.append(result)
            return result
        def connect(database, *args, **kwargs):
            if database == self.database.as_uri() + "?mode=ro":
                error = sqlite3.OperationalError("Disposable read-only source")
                error.sqlite_errorcode = sqlite3.SQLITE_READONLY
                raise error
            connection = self.original_connect(database, *args, **kwargs)
            connection.set_trace_callback(transactions.append)
            return connection
        with patch("codex_source.sqlite3.connect", side_effect=connect), \
                patch("codex_source._private_index_snapshot", wraps=original_copy) as copies, \
                patch("codex_source.os.write", side_effect=write):
            for step in range(30):
                observations, health = self.source.scan(100 + step * 2, 0)
                self.assertTrue(health["available"])
                self.assertEqual([item["id"] for item in observations], ["live"])
                self.assertEqual(self.source._excluded_index_ids, {"archived", "child"})
        self.assertEqual(copies.call_count, 1)
        self.assertEqual(sum(written), 0)
        self.assertEqual(transactions.count("BEGIN"), 30)
        self.assertEqual(transactions.count("ROLLBACK"), 30)
        self.assertEqual(transactions.count("SELECT id FROM threads WHERE archived = 1"), 30)
        self.assertEqual(self.database.read_bytes(), self.original_bytes)
        self.assert_clean()

    def test_closed_wal_in_read_only_folder_uses_one_real_copy_for_thirty_scans(self):
        self.root.chmod(0o555)
        self.database.chmod(0o444)
        self.addCleanup(self.root.chmod, 0o700)
        self.addCleanup(self.database.chmod, 0o600)
        connection = None
        try:
            connection = self.original_connect(self.database.as_uri() + "?mode=ro", uri=True)
            connection.execute("PRAGMA schema_version").fetchone()
        except sqlite3.Error as error:
            self.assertIn(getattr(error, "sqlite_errorcode", 0) & 0xff,
                          (sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CANTOPEN))
        else:
            self.skipTest("This SQLite/environment can open closed WAL indexes without folder writes")
        finally:
            if connection is not None:
                connection.close()
        original_write, written = os.write, []
        def write(descriptor, data):
            result = original_write(descriptor, data)
            written.append(result)
            return result
        before = self.wal_metadata(self.database)
        with patch("codex_source._private_index_snapshot", wraps=codex_source._private_index_snapshot) as copies, \
                patch("codex_source.os.write", side_effect=write):
            for step in range(30):
                items, health = self.source.scan(100 + step * 2, 0)
                self.assertTrue(health["available"], health["detail"])
                self.assertEqual([item["id"] for item in items], ["live"])
        self.assertEqual(copies.call_count, 1)
        self.assertEqual(sum(written), 0)
        self.assertEqual(self.wal_metadata(self.database), before)
        self.assertEqual({path.name for path in self.root.iterdir()}, {"state_5.sqlite"})
        self.assert_clean()

    def test_eighty_mib_index_copies_once_within_total_write_budget(self):
        connection = self.original_connect(self.database)
        connection.execute("CREATE TABLE padding (value BLOB)")
        connection.execute("INSERT INTO padding VALUES (zeroblob(?))", (80 * 1024 * 1024,))
        connection.commit()
        connection.close()
        size = self.database.stat().st_size
        self.assertGreater(size, 80 * 1024 * 1024)
        original_write, written = os.write, []
        def write(descriptor, data):
            result = original_write(descriptor, data)
            written.append(result)
            return result
        with self.blocked(), patch("codex_source._private_index_snapshot", wraps=codex_source._private_index_snapshot) as copies, \
                patch("codex_source.os.write", side_effect=write):
            for _ in range(30):
                rows, error = self.source._thread_rows(0)
                self.assertIsNone(error)
                self.assertEqual([row["id"] for row in rows], ["live"])
        self.assertEqual(copies.call_count, 1)
        self.assertEqual(sum(written), 0)
        self.assertLess(sum(written), codex_source._INDEX_COPY_BYTES)
        self.assert_clean()

    def test_oversized_copy_backs_off_without_writes_and_recovers_after_deadline(self):
        with self.database.open("r+b") as writer:
            writer.truncate(codex_source._INDEX_COPY_BYTES + 1)
        clock = [10.0]
        with self.blocked(), patch("codex_source.time.monotonic", side_effect=lambda: clock[0]), \
                patch("codex_source._private_index_snapshot", wraps=codex_source._private_index_snapshot) as copies, \
                patch("codex_source.os.write", side_effect=AssertionError("An oversized index must not be copied")):
            for step in range(30):
                clock[0] = 10 + step * 2
                rows, health = self.source.scan(100 + step * 2, 0)
                self.assertEqual(rows, [])
                self.assertFalse(health["available"])
                self.assertIn("128 MiB", health["detail"])
                self.assertIn("60 seconds", health["detail"])
            self.assertEqual(copies.call_count, 1)
            self.assertIsNone(self.source._cached_index)
        self.database.write_bytes(self.original_bytes)
        with self.blocked(), patch("codex_source.time.monotonic", side_effect=lambda: clock[0]), \
                patch("codex_source._private_index_snapshot", wraps=codex_source._private_index_snapshot) as copies:
            clock[0] = 69.99
            self.assertIsNotNone(self.source._thread_rows(0)[1])
            self.assertEqual(copies.call_count, 0)
            clock[0] = 70
            self.assertIsNone(self.source._thread_rows(0)[1])
            self.assertEqual(copies.call_count, 1)
        self.assertEqual(self.source._index_retry_at, 0)
        self.assertIsNone(self.source._index_error_detail)
        self.assert_clean()

    def test_changed_cached_source_discards_rows_and_exclusions_before_retry(self):
        clock = [10.0]
        original_excluded = self.source._excluded_ids
        def mutate(connection=None, columns=None):
            result = original_excluded(connection, columns)
            writer = self.original_connect(self.database)
            writer.execute("UPDATE threads SET title = 'Current title' WHERE id = 'live'")
            writer.commit()
            writer.close()
            return result
        with self.blocked(), patch("codex_source.time.monotonic", side_effect=lambda: clock[0]):
            self.assertIsNone(self.source._thread_rows(0)[1])
            with patch.object(self.source, "_excluded_ids", side_effect=mutate):
                rows, error = self.source._thread_rows(0)
            self.assertEqual(rows, [])
            self.assertIsNotNone(error)
            self.assertEqual(self.source._indexed_paths, set())
            self.assertEqual(self.source._excluded_index_ids, set())
            self.assertIsNone(self.source._cached_index)
            with patch("codex_source._private_index_snapshot", side_effect=AssertionError("Backoff must not copy")):
                self.assertIsNotNone(self.source._thread_rows(0)[1])
            clock[0] = 70
            rows, error = self.source._thread_rows(0)
            self.assertIsNone(error)
            self.assertEqual(rows[0]["title"], "Current title")
        self.assert_clean()

    def test_live_index_queries_share_one_consistent_transaction(self):
        writer = self.original_connect(self.database)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE threads SET title='Before live change' WHERE id='live'")
        writer.commit()
        original_excluded, changed = self.source._excluded_ids, []
        def change_before_exclusions(connection=None, columns=None):
            if not changed:
                changed.append(True)
                writer.execute("UPDATE threads SET title='After live change', archived=1 WHERE id='live'")
                writer.commit()
            return original_excluded(connection, columns)
        try:
            with patch.object(self.source, "_excluded_ids", side_effect=change_before_exclusions), \
                    patch("codex_source.sqlite3.connect", wraps=self.original_connect) as connections, \
                    patch("codex_source._private_index_snapshot", side_effect=AssertionError("Live WAL is readable directly")):
                rows, error = self.source._thread_rows(0)
                self.assertIsNone(error)
                self.assertEqual(rows[0]["title"], "Before live change")
                self.assertNotIn("live", self.source._excluded_index_ids)
                self.assertEqual(connections.call_count, 1)
                rows, error = self.source._thread_rows(0)
                self.assertIsNone(error)
                self.assertEqual(rows, [])
                self.assertIn("live", self.source._excluded_index_ids)
                self.assertEqual(connections.call_count, 1)
        finally:
            writer.close()

    def test_new_live_wal_invalidates_copy_and_returns_to_direct_reads(self):
        with self.blocked():
            self.assertIsNone(self.source._thread_rows(0)[1])
        cached_connection = self.source._cached_index[3]
        writer = self.original_connect(self.database)
        try:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE threads SET title='Latest live title' WHERE id='live'")
            writer.commit()
            with patch("codex_source._private_index_snapshot", side_effect=AssertionError("Readable live WAL must not copy")):
                rows, error = self.source._thread_rows(0)
            self.assertIsNone(error)
            self.assertEqual(rows[0]["title"], "Latest live title")
            self.assertIsNotNone(self.source._cached_index)
            self.assertIsNot(self.source._cached_index[3], cached_connection)
            with self.assertRaises(sqlite3.ProgrammingError):
                cached_connection.execute("SELECT 1")
        finally:
            writer.close()

    def test_normal_reader_reuses_connection_for_thirty_committed_revisions(self):
        writer = self.original_connect(self.database)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE threads SET title='Revision 0' WHERE id='live'")
        writer.commit()
        try:
            with patch("codex_source.sqlite3.connect", wraps=self.original_connect) as connections, \
                    patch("codex_source._private_index_snapshot", side_effect=AssertionError("Live WAL must use SQLite")):
                for revision in range(30):
                    writer.execute("UPDATE threads SET title=? WHERE id='live'", (f"Revision {revision}",))
                    writer.commit()
                    rows, error = self.source._thread_rows(0)
                    self.assertIsNone(error)
                    self.assertEqual(rows[0]["title"], f"Revision {revision}")
                    self.assertFalse(self.source._cached_index[3].in_transaction)
                    if revision == 10:
                        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if revision == 20:
                        writer.close()
                        writer = self.original_connect(self.database)
                self.assertEqual(connections.call_count, 1)
                retained = self.source._cached_index[3]
                self.source.close()
                with self.assertRaises(sqlite3.ProgrammingError):
                    retained.execute("SELECT 1")
        finally:
            writer.close()

    def test_normal_reader_source_or_sidecar_replacement_during_query_rejects_rows(self):
        for kind in ("database", "root", "wal", "shm", "journal", "mode"):
            with self.subTest(kind=kind):
                self.fresh_reader()
                # A normal read initializes empty SQLite sidecars. Each case
                # starts with a disposable complete database and independent cache.
                self.database.write_bytes(self.original_bytes)
                with patch("codex_source.time.monotonic", return_value=10):
                    self.assertIsNone(self.source._thread_rows(0)[1])
                    connection = self.source._cached_index[3]
                    original_excluded = self.source._excluded_ids
                    def replace(connection=None, columns=None):
                        result = original_excluded(connection, columns)
                        if kind == "database":
                            replacement = self.root / "replacement"
                            replacement.write_bytes(self.original_bytes)
                            os.replace(replacement, self.database)
                        elif kind == "root":
                            old = self.root.with_name("old-normal-root")
                            self.root.rename(old)
                            self.root.mkdir(mode=0o700)
                            self.database.write_bytes(self.original_bytes)
                        elif kind == "mode":
                            self.database.chmod(0o444)
                        else:
                            name = self.root / ("state_5.sqlite-" + kind)
                            replacement = self.root / "replacement-sidecar"
                            replacement.write_bytes(name.read_bytes() if name.exists() else b"")
                            os.replace(replacement, name)
                        return result
                    with patch.object(self.source, "_excluded_ids", side_effect=replace):
                        rows, error = self.source._thread_rows(0)
                    self.assertEqual(rows, [])
                    self.assertIsNotNone(error)
                    self.assertEqual(self.source._indexed_paths, set())
                    self.assertEqual(self.source._excluded_index_ids, set())
                    self.assertIsNone(self.source._cached_index)
                    with self.assertRaises(sqlite3.ProgrammingError):
                        connection.execute("SELECT 1")
                self.database.chmod(0o600)
                for suffix in ("wal", "shm", "journal"):
                    (self.root / ("state_5.sqlite-" + suffix)).unlink(missing_ok=True)
                with patch("codex_source.time.monotonic", return_value=70):
                    rows, error = self.source._thread_rows(0)
                    self.assertIsNone(error)
                    self.assertEqual(rows[0]["id"], "live")
                self.source.close()

    def test_normal_cache_closes_before_reopening_replaced_index(self):
        self.assertIsNone(self.source._thread_rows(0)[1])
        previous = self.source._cached_index[3]
        replacement = self.root / "replacement.sqlite"
        replacement.write_bytes(self.original_bytes)
        writer = self.original_connect(replacement)
        writer.execute("UPDATE threads SET title='Replacement revision' WHERE id='live'")
        writer.commit()
        writer.close()
        os.replace(replacement, self.database)
        def connect(path, *args, **kwargs):
            with self.assertRaises(sqlite3.ProgrammingError):
                previous.execute("SELECT 1")
            return self.original_connect(path, *args, **kwargs)
        with patch("codex_source.sqlite3.connect", side_effect=connect) as connections:
            rows, error = self.source._thread_rows(0)
        self.assertIsNone(error)
        self.assertEqual(rows[0]["title"], "Replacement revision")
        self.assertEqual(connections.call_count, 1)

    def test_cached_full_source_signature_invalidates_before_reuse(self):
        for change in ("replace", "preserved-mtime-write", "mode", "empty-wal", "root-replaced"):
            with self.subTest(change=change):
                self.fresh_reader()
                before = self.database.stat()
                with self.blocked(), patch("codex_source._private_index_snapshot", wraps=codex_source._private_index_snapshot) as copies:
                    self.assertIsNone(self.source._thread_rows(0)[1])
                    if change == "replace":
                        replacement = self.root / "replacement"
                        replacement.write_bytes(self.database.read_bytes())
                        os.replace(replacement, self.database)
                    elif change == "preserved-mtime-write":
                        with self.database.open("r+b") as writer:
                            writer.seek(60)
                            writer.write(b"\x00\x00\x00\x2a")
                        os.utime(self.database, ns=(before.st_atime_ns, before.st_mtime_ns))
                    elif change == "mode":
                        self.database.chmod(0o444)
                    elif change == "empty-wal":
                        self.empty_wal()
                    else:
                        retired = self.root.with_name("old-source")
                        self.root.rename(retired)
                        self.root.mkdir(mode=0o700)
                        self.database.write_bytes((retired / "state_5.sqlite").read_bytes())
                    rows, error = self.source._thread_rows(0)
                    self.assertIsNone(error)
                    self.assertEqual(rows[0]["id"], "live")
                    self.assertEqual(copies.call_count, 2)
                self.source.close()
                self.database.chmod(0o600)
                (self.root / "state_5.sqlite-wal").unlink(missing_ok=True)
        self.assert_clean()

    def test_cached_wal_changes_never_reuse_unverified_rows(self):
        for change in ("replace-empty", "grow-truncate", "nonempty", "journal", "remove"):
            with self.subTest(change=change):
                self.fresh_reader()
                wal = self.empty_wal()
                before = wal.stat()
                with self.blocked(), patch("codex_source._private_index_snapshot", wraps=codex_source._private_index_snapshot) as copies:
                    self.assertIsNone(self.source._thread_rows(0)[1])
                    if change == "replace-empty":
                        replacement = self.root / "replacement-wal"
                        replacement.touch(mode=0o600)
                        os.replace(replacement, wal)
                    elif change == "grow-truncate":
                        wal.write_bytes(b"writer")
                        wal.write_bytes(b"")
                        os.utime(wal, ns=(before.st_atime_ns, before.st_mtime_ns))
                    elif change == "nonempty":
                        wal.write_bytes(b"writer")
                    elif change == "journal":
                        (self.root / "state_5.sqlite-journal").touch()
                    else:
                        wal.unlink()
                    rows, error = self.source._thread_rows(0)
                    self.assertEqual(copies.call_count, 2)
                    if change in ("nonempty", "journal"):
                        self.assertEqual(rows, [])
                        self.assertIsNotNone(error)
                        self.assertIsNone(self.source._cached_index)
                    else:
                        self.assertIsNone(error)
                        self.assertEqual(rows[0]["id"], "live")
                self.source.close()
                wal.unlink(missing_ok=True)
                (self.root / "state_5.sqlite-journal").unlink(missing_ok=True)
        self.assert_clean()

    def test_source_alias_repoint_drops_old_cache_and_child_symlink_is_rejected(self):
        alias = self.root.with_name("selected")
        alias.symlink_to(self.root, target_is_directory=True)
        source = self.reader(alias)
        replacement = self.root.with_name("replacement-source")
        replacement.mkdir(mode=0o700)
        database = replacement / "state_5.sqlite"
        database.write_bytes(self.original_bytes)
        connection = self.original_connect(database)
        connection.execute("UPDATE threads SET title='Replacement title' WHERE id='live'")
        connection.commit()
        connection.close()
        original_copy = codex_source._private_index_snapshot
        def connect(path, *args, **kwargs):
            if isinstance(path, str) and path.endswith("state_5.sqlite?mode=ro"):
                error = sqlite3.OperationalError("Disposable read-only grant")
                error.sqlite_errorcode = sqlite3.SQLITE_READONLY
                raise error
            return self.original_connect(path, *args, **kwargs)
        with patch("codex_source.sqlite3.connect", side_effect=connect), \
                patch("codex_source._private_index_snapshot", wraps=original_copy) as copies:
            self.assertIsNone(source._thread_rows(0)[1])
            alias.unlink()
            alias.symlink_to(replacement, target_is_directory=True)
            rows, error = source._thread_rows(0)
            self.assertIsNone(error)
            self.assertEqual(rows[0]["title"], "Replacement title")
            self.assertEqual(copies.call_count, 2)
            database.unlink()
            database.symlink_to(self.database)
            rows, error = source._thread_rows(0)
            self.assertEqual(rows, [])
            self.assertIsNotNone(error)
            self.assertIsNone(source._cached_index)
        self.assert_clean()

    def test_close_waits_for_active_index_query_then_releases_cache_once(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        failures = []
        def query():
            try:
                with self.source._index_connection(self.database) as connection:
                    connection.execute("SELECT 1")
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError("Close fixture timed out")
            except BaseException as error:
                failures.append(error)
        def close():
            self.source.close()
            closed.set()
        with self.blocked():
            self.assertIsNone(self.source._thread_rows(0)[1])
            cached_connection = self.source._cached_index[3]
            worker = threading.Thread(target=query)
            closer = threading.Thread(target=close)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                closer.start()
                self.assertFalse(closed.wait(.02))
            finally:
                release.set()
                worker.join(2)
                if closer.ident is not None:
                    closer.join(2)
            self.assertFalse(worker.is_alive())
            self.assertFalse(closer.is_alive())
            self.assertEqual(failures, [])
            self.assertTrue(closed.is_set())
            self.source.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            cached_connection.execute("SELECT 1")
        with patch("codex_source.sqlite3.connect", side_effect=AssertionError("Closed sources must not reopen")), \
                patch.object(self.source, "_unindexed_paths", side_effect=AssertionError("Closed sources must not scan")):
            self.assertIsNotNone(self.source._thread_rows(0)[1])
            items, health = self.source.scan(100, 0)
            self.assertEqual(items, [])
            self.assertFalse(health["available"])
        self.assert_clean()

    def test_selected_root_alias_allows_the_direct_index(self):
        alias = self.root.parent / "selected-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        connection = self.original_connect(self.database)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.close()
        source = self.reader(alias)
        rows, error = source._thread_rows(0)
        self.assertIsNone(error)
        self.assertEqual([row["id"] for row in rows], ["live"])
        self.assertEqual(source._excluded_ids(), {"archived", "child"})
        self.assert_clean()

    def test_selected_root_alias_allows_only_the_checkpointed_fallback(self):
        alias = self.root.parent / "selected-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        source = self.reader(alias)
        with self.blocked():
            rows, error = source._thread_rows(0)
            excluded = source._excluded_ids()
        self.assertIsNone(error)
        self.assertEqual([row["id"] for row in rows], ["live"])
        self.assertEqual(excluded, {"archived", "child"})
        self.assertEqual(self.database.read_bytes(), self.original_bytes)
        self.assert_clean()

    def test_selected_root_alias_does_not_allow_a_child_database_symlink(self):
        alias = self.root.parent / "selected-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        outside = self.root.parent / "outside.sqlite"
        self.database.rename(outside)
        self.database.symlink_to(outside)
        source = self.reader(alias)
        with patch("codex_source.sqlite3.connect", side_effect=AssertionError("A child symlink must not be opened")):
            rows, error = source._thread_rows(0)
            excluded = source._excluded_ids()
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)
        self.assertEqual(excluded, set())
        self.assertEqual(outside.read_bytes(), self.original_bytes)
        self.assert_clean()

    def test_memory_snapshot_is_query_only_and_releases_input_mapping(self):
        original_mmap, mappings = codex_source.mmap.mmap, []
        def allocate(descriptor, size):
            self.assertEqual(descriptor, -1)
            mapping = original_mmap(descriptor, size)
            mappings.append(mapping)
            return mapping
        with self.blocked(), patch("codex_source.mmap.mmap", side_effect=allocate), \
                patch("codex_source.os.write", side_effect=AssertionError("Memory snapshot must not write files")):
            with self.source._index_connection(self.database) as connection:
                self.assertEqual(connection.execute("PRAGMA database_list").fetchone()[2], "")
                self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA temp_store").fetchone()[0], 2)
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "memory")
                self.assertEqual(len(mappings), 1)
                self.assertTrue(mappings[0].closed)
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("DELETE FROM threads")
        self.assertEqual(self.database.read_bytes(), self.original_bytes)
        self.assertEqual(self.original_bytes[18:20], b"\x02\x02")
        self.assert_clean()

    def test_missing_deserialize_and_memory_failure_back_off_without_disk_fallback(self):
        for failure in ("unsupported", "allocation"):
            with self.subTest(failure=failure):
                self.fresh_reader()
                clock = [10.0]
                fault = (patch("codex_source.sqlite3.Connection", object) if failure == "unsupported" else
                         patch("codex_source.mmap.mmap", side_effect=MemoryError("Disposable memory exhaustion")))
                with self.blocked(), fault, patch("codex_source.time.monotonic", side_effect=lambda: clock[0]), \
                        patch("codex_source.os.write", side_effect=AssertionError("No disk fallback")), \
                        patch("codex_source._private_index_snapshot", wraps=codex_source._private_index_snapshot) as snapshots:
                    for step in range(30):
                        clock[0] = 10 + 2 * step
                        rows, error = self.source._thread_rows(0)
                        self.assertEqual(rows, [])
                        self.assertIn("60 seconds", error)
                    self.assertEqual(snapshots.call_count, 1)
                    self.assertIsNone(self.source._cached_index)
                with self.blocked(), patch("codex_source.time.monotonic", return_value=70):
                    self.assertIsNone(self.source._thread_rows(0)[1])
                self.assert_clean()

    def test_deserialize_failure_releases_input_and_source_descriptors(self):
        original_mmap, mappings = codex_source.mmap.mmap, []
        def allocate(*args):
            mapping = original_mmap(*args)
            mappings.append(mapping)
            return mapping
        class FailedConnection:
            closed = False
            def execute(self, statement):
                return self
            def deserialize(self, data):
                raise MemoryError("Disposable deserialize failure")
            def close(self):
                self.closed = True
        failed = FailedConnection()
        def connect(path, *args, **kwargs):
            if path == ":memory:":
                return failed
            error = sqlite3.OperationalError("Disposable read-only source")
            error.sqlite_errorcode = sqlite3.SQLITE_READONLY
            raise error
        descriptors, tracker = self.tracked_opens()
        with patch("codex_source.sqlite3.connect", side_effect=connect), tracker, \
                patch("codex_source.mmap.mmap", side_effect=allocate):
            rows, error = self.source._thread_rows(0)
        self.assertEqual(rows, [])
        self.assertIn("60 seconds", error)
        self.assertTrue(failed.closed)
        self.assertTrue(mappings[0].closed)
        self.assert_descriptors_closed(descriptors)
        self.assert_clean()

    def test_query_error_removes_copy_and_sidecars(self):
        with self.blocked(), self.assertRaises(sqlite3.Error):
            with self.source._index_connection(self.database) as connection:
                connection.execute("SELECT nonexistent_column FROM threads")
        self.assert_clean()
        self.assertEqual(self.database.read_bytes(), self.original_bytes)

    def test_wal_and_rollback_journal_each_block_fallback(self):
        for suffix in ("-wal", "-journal"):
            with self.subTest(suffix=suffix):
                self.fresh_reader()
                sidecar = self.root / ("state_5.sqlite" + suffix)
                sidecar.write_bytes(b"Disposable sidecar")
                with self.blocked():
                    rows, error = self.source._thread_rows(0)
                self.assertEqual(rows, [])
                self.assertIsNotNone(error)
                self.assert_clean()
                sidecar.unlink()

    def test_new_sidecar_during_copy_rejects_result_and_cleans(self):
        original_read = os.read
        for suffix in ("-wal", "-journal"):
            self.fresh_reader()
            sidecar = self.root / ("state_5.sqlite" + suffix)
            def read(descriptor, size):
                if os.fstat(descriptor).st_ino == self.database.stat().st_ino:
                    sidecar.write_bytes(b"Writer started")
                return original_read(descriptor, size)
            with self.subTest(suffix=suffix), self.blocked(), patch("codex_source.os.read", side_effect=read):
                rows, error = self.source._thread_rows(0)
            self.assertEqual(rows, [])
            self.assertIsNotNone(error)
            self.assert_clean()
            sidecar.unlink()

    def test_new_journal_during_query_cannot_return_success(self):
        with self.blocked(), self.assertRaises(ValueError):
            with self.source._index_connection(self.database) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM threads").fetchone()[0], 2)
                (self.root / "state_5.sqlite-journal").write_bytes(b"Writer started")
        self.assert_clean()

    def test_repeated_source_rewrites_are_bounded_and_rejected(self):
        original_read = os.read
        calls = []
        def read(descriptor, size):
            if os.fstat(descriptor).st_ino == self.database.stat().st_ino:
                calls.append(True)
                with self.database.open("r+b") as writer:
                    writer.seek(60)
                    writer.write(len(calls).to_bytes(4, "big"))
            return original_read(descriptor, size)
        with self.blocked(), patch("codex_source.os.read", side_effect=read):
            rows, error = self.source._thread_rows(0)
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)
        self.assertEqual(len(calls), 2)
        self.assert_clean()

    def test_source_path_replacement_retries_with_current_identity(self):
        original_read = os.read
        replaced = []
        def read(descriptor, size):
            if not replaced and os.fstat(descriptor).st_ino == self.database.stat().st_ino:
                replaced.append(True)
                self.database.rename(self.root / "retired.sqlite")
                self.database.write_bytes(self.original_bytes)
            return original_read(descriptor, size)
        with self.blocked(), patch("codex_source.os.read", side_effect=read):
            rows, error = self.source._thread_rows(0)
        self.assertIsNone(error)
        self.assertEqual([row["id"] for row in rows], ["live"])
        self.assert_clean()

    def test_size_and_cooperative_time_budgets_fail_closed(self):
        for setting, value in (("_INDEX_COPY_BYTES", len(self.original_bytes) - 1), ("_INDEX_COPY_SECONDS", 0)):
            self.fresh_reader()
            with self.subTest(setting=setting), self.blocked(), patch.object(codex_source, setting, value):
                rows, error = self.source._thread_rows(0)
            self.assertEqual(rows, [])
            self.assertIsNotNone(error)
            self.assert_clean()

    def test_retry_byte_budget_is_shared_between_attempts(self):
        original_read = os.read
        copied = []
        def read(descriptor, size):
            copied.append(size)
            with self.database.open("r+b") as writer:
                writer.seek(60)
                writer.write(len(copied).to_bytes(4, "big"))
            return original_read(descriptor, size)
        with self.blocked(), patch("codex_source.os.read", side_effect=read), \
                patch.object(codex_source, "_INDEX_COPY_BYTES", len(self.original_bytes) + 1):
            rows, error = self.source._thread_rows(0)
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)
        self.assertEqual(len(copied), 1)
        self.assert_clean()

    def test_cooperative_deadline_after_partial_memory_read_releases_resources(self):
        connection = self.original_connect(self.database)
        connection.execute("CREATE TABLE padding (value BLOB)")
        connection.execute("INSERT INTO padding VALUES (zeroblob(?))", (2 * 1024 * 1024,))
        connection.commit()
        connection.close()
        before = self.database.read_bytes()
        original_read, original_mmap = os.read, codex_source.mmap.mmap
        read_bytes, clock, mappings = [], [0], []
        def read(descriptor, size):
            result = original_read(descriptor, size)
            read_bytes.append(len(result))
            clock[0] = 2
            return result
        def allocate(*args):
            mapping = original_mmap(*args)
            mappings.append(mapping)
            return mapping
        descriptors, tracker = self.tracked_opens()
        with self.blocked(), tracker, patch("codex_source.time.monotonic", side_effect=lambda: clock[0]), \
                patch("codex_source.os.read", side_effect=read), patch("codex_source.mmap.mmap", side_effect=allocate):
            rows, error = self.source._thread_rows(0)
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)
        self.assertGreater(sum(read_bytes), 0)
        self.assertLess(sum(read_bytes), len(before))
        self.assertTrue(mappings[0].closed)
        self.assert_descriptors_closed(descriptors)
        self.assertEqual(self.database.read_bytes(), before)
        self.assert_clean()

    def test_source_symlink_hardlink_and_fifo_are_rejected(self):
        original = self.root / "original.sqlite"
        self.database.rename(original)
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                self.fresh_reader()
                if kind == "symlink":
                    self.database.symlink_to(original)
                elif kind == "hardlink":
                    os.link(original, self.database)
                else:
                    os.mkfifo(self.database)
                with self.blocked():
                    rows, error = self.source._thread_rows(0)
                self.assertEqual(rows, [])
                self.assertIsNotNone(error)
                self.assert_clean()
                self.database.unlink()

    def test_other_sqlite_errors_do_not_trigger_copy(self):
        with self.blocked(sqlite3.SQLITE_CORRUPT), patch("codex_source._private_index_snapshot", side_effect=AssertionError("No fallback")):
            rows, error = self.source._thread_rows(0)
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)
        self.assert_clean()

    def test_non_wal_header_does_not_trigger_copy(self):
        with self.database.open("r+b") as writer:
            writer.seek(18)
            writer.write(b"\x01\x01")
        with self.blocked():
            rows, error = self.source._thread_rows(0)
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)
        self.assert_clean()

    def test_unconfined_or_unconfigured_readers_do_not_copy(self):
        for source in (self.reader(confine_paths=False), self.reader(snapshot_parent=None)):
            with self.blocked():
                rows, error = source._thread_rows(0)
            self.assertEqual(rows, [])
            self.assertIsNotNone(error)
            self.assert_clean()


    def test_deferred_source_error_closes_connection_before_fallback(self):
        class Deferred:
            closed = False
            def execute(self, query):
                if query == "PRAGMA schema_version":
                    error = sqlite3.OperationalError("Disposable deferred failure")
                    error.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
                    raise error
                return self
            def close(self):
                self.closed = True
        deferred = Deferred()
        def connect(database, *args, **kwargs):
            if database == self.database.as_uri() + "?mode=ro":
                return deferred
            self.assertTrue(deferred.closed)
            return self.original_connect(database, *args, **kwargs)
        with patch("codex_source.sqlite3.connect", side_effect=connect):
            rows, error = self.source._thread_rows(0)
        self.assertIsNone(error)
        self.assertEqual(len(rows), 1)
        self.assertTrue(deferred.closed)
        self.assert_clean()

    def test_successful_direct_connection_is_explicitly_closed(self):
        class Direct:
            closed = False
            def execute(self, query):
                return self
            def fetchone(self):
                return (1,)
            def close(self):
                self.closed = True
        direct = Direct()
        with patch("codex_source.sqlite3.connect", return_value=direct):
            with self.source._index_connection(self.database) as connection:
                self.assertIs(connection, direct)
                self.assertFalse(direct.closed)
        self.assertFalse(direct.closed)
        self.source.close()
        self.assertTrue(direct.closed)
        self.assert_clean()


    def test_unknown_named_files_and_links_are_preserved(self):
        unrelated = self.private / "notes.txt"
        unrelated.write_bytes(b"Keep this unrelated file")
        link = self.private / "notes-link"
        link.symlink_to(unrelated)
        with self.blocked():
            rows, error = self.source._thread_rows(0)
        self.assertIsNone(error)
        self.assertEqual(len(rows), 1)
        self.assertEqual(unrelated.read_bytes(), b"Keep this unrelated file")
        self.assertTrue(link.is_symlink())
        self.assertEqual({path.name for path in self.private.iterdir()}, {"notes.txt", "notes-link"})



    def empty_wal(self):
        wal = self.root / "state_5.sqlite-wal"
        wal.touch(mode=0o600, exist_ok=False)
        return wal

    def wal_metadata(self, wal):
        value = wal.lstat()
        return (value.st_dev, value.st_ino, value.st_size, value.st_mode, value.st_uid,
                value.st_gid, value.st_nlink, value.st_mtime_ns, value.st_ctime_ns)

    def tracked_opens(self):
        original_open, descriptors = os.open, []
        def open_file(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            descriptors.append(descriptor)
            return descriptor
        return descriptors, patch("codex_source.os.open", side_effect=open_file)

    def assert_descriptors_closed(self, descriptors):
        for descriptor in set(descriptors):
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def copy_mutation(self, mutation):
        original_read, changed = os.read, []
        inode = self.database.stat().st_ino
        def read(descriptor, size):
            data = original_read(descriptor, size)
            if os.fstat(descriptor).st_ino == inode and not changed:
                changed.append(True)
                mutation()
            return data
        with self.blocked(), patch("codex_source.os.read", side_effect=read):
            rows, error = self.source._thread_rows(0)
        self.assertTrue(changed)
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)
        self.assert_clean()

    def test_stable_empty_wal_reads_live_rows_and_archive_exclusions(self):
        wal = self.empty_wal()
        before = self.wal_metadata(wal)
        with self.blocked():
            rows, error = self.source._thread_rows(0)
            excluded = self.source._excluded_ids()
            with self.source._index_connection(self.database) as connection:
                self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA database_list").fetchone()[2], "")
                with self.assertRaises(sqlite3.Error):
                    connection.execute("DELETE FROM threads")
        self.assertIsNone(error)
        self.assertEqual([row["id"] for row in rows], ["live"])
        self.assertEqual(excluded, {"archived", "child"})
        self.assertEqual(self.wal_metadata(wal), before)
        self.assertEqual(wal.read_bytes(), b"")
        self.assertEqual(self.database.read_bytes(), self.original_bytes)
        self.assertEqual({path.name for path in self.root.iterdir()}, {"state_5.sqlite", wal.name})
        self.assert_clean()

    def test_fallback_never_reads_or_writes_empty_wal_contents(self):
        wal = self.empty_wal()
        inode = wal.stat().st_ino
        originals = {name: getattr(os, name) for name in ("read", "pread", "write")}
        def checked(name):
            def operation(descriptor, *args):
                self.assertNotEqual(os.fstat(descriptor).st_ino, inode, "Fallback accessed WAL contents")
                return originals[name](descriptor, *args)
            return operation
        with self.blocked(), patch("codex_source.os.read", side_effect=checked("read")), \
                patch("codex_source.os.pread", side_effect=checked("pread")), \
                patch("codex_source.os.write", side_effect=checked("write")):
            rows, error = self.source._thread_rows(0)
        self.assertIsNone(error)
        self.assertEqual(len(rows), 1)
        self.assertEqual(wal.read_bytes(), b"")
        self.assert_clean()

    def test_nonempty_wal_and_even_empty_journal_still_block_copy(self):
        for suffix, size in (("-wal", 1), ("-wal", 31), ("-wal", 32),
                             ("-journal", 0), ("-journal", 32)):
            sidecar = self.root / ("state_5.sqlite" + suffix)
            with self.subTest(suffix=suffix, size=size):
                self.fresh_reader()
                sidecar.write_bytes(b"x" * size)
                before = sidecar.read_bytes()
                with self.blocked():
                    rows, error = self.source._thread_rows(0)
                self.assertEqual(rows, [])
                self.assertIsNotNone(error)
                self.assertEqual(sidecar.read_bytes(), before)
                self.assert_clean()
                sidecar.unlink()

    def test_valid_nonempty_wal_with_uncheckpointed_data_is_refused(self):
        writer = self.original_connect(self.database)
        try:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO threads VALUES ('pending','','101','Uncheckpointed fixture',0)")
            writer.commit()
            wal = self.root / "state_5.sqlite-wal"
            self.assertGreater(wal.stat().st_size, 32)
            before = wal.read_bytes(), self.wal_metadata(wal), self.database.read_bytes()
            with self.blocked():
                rows, error = self.source._thread_rows(0)
            self.assertEqual(rows, [])
            self.assertIsNotNone(error)
            self.assertEqual((wal.read_bytes(), self.wal_metadata(wal), self.database.read_bytes()), before)
            self.assert_clean()
        finally:
            writer.close()

    def test_empty_wal_links_and_special_files_are_refused(self):
        target = self.root / "unrelated-empty"
        target.touch(mode=0o600)
        wal = self.root / "state_5.sqlite-wal"
        for kind in ("symlink", "hardlink", "fifo", "directory"):
            with self.subTest(kind=kind):
                self.fresh_reader()
                if kind == "symlink":
                    wal.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, wal)
                elif kind == "fifo":
                    os.mkfifo(wal)
                else:
                    wal.mkdir()
                with self.blocked():
                    rows, error = self.source._thread_rows(0)
                self.assertEqual(rows, [])
                self.assertIsNotNone(error)
                self.assertEqual(target.read_bytes(), b"")
                self.assert_clean()
                wal.rmdir() if wal.is_dir() else wal.unlink()

    def test_foreign_owned_empty_wal_is_refused_before_open(self):
        wal = self.empty_wal()
        original_stat, original_open = os.stat, os.open
        def metadata(name, *args, **kwargs):
            value = original_stat(name, *args, **kwargs)
            if name == wal.name and kwargs.get("dir_fd") is not None:
                fields = list(value)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return value
        def open_file(name, *args, **kwargs):
            self.assertNotEqual(name, wal.name, "Foreign-owned WAL must not be opened")
            return original_open(name, *args, **kwargs)
        with self.blocked(), patch("codex_source.os.stat", side_effect=metadata), \
                patch("codex_source.os.open", side_effect=open_file):
            rows, error = self.source._thread_rows(0)
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)
        self.assert_clean()

    def test_wal_replacement_between_stat_and_open_cannot_block_or_leak(self):
        wal = self.empty_wal()
        original_open, descriptors, replaced = os.open, [], []
        def open_file(name, flags, *args, **kwargs):
            if name == wal.name and not replaced:
                self.assertTrue(flags & os.O_NONBLOCK)
                self.assertTrue(flags & os.O_NOFOLLOW)
                self.assertTrue(flags & os.O_CLOEXEC)
                replaced.append(True)
                wal.unlink()
                os.mkfifo(wal)
            descriptor = original_open(name, flags, *args, **kwargs)
            descriptors.append(descriptor)
            return descriptor
        with self.blocked(), patch("codex_source.os.open", side_effect=open_file):
            rows, error = self.source._thread_rows(0)
        self.assertTrue(replaced)
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)
        self.assert_descriptors_closed(descriptors)
        self.assert_clean()

    def test_empty_wal_open_validation_error_closes_new_descriptor(self):
        wal = self.empty_wal()
        original_open, original_fstat = os.open, os.fstat
        descriptors, wal_descriptors = [], []
        def open_file(name, *args, **kwargs):
            descriptor = original_open(name, *args, **kwargs)
            descriptors.append(descriptor)
            if name == wal.name:
                wal_descriptors.append(descriptor)
            return descriptor
        def metadata(descriptor):
            if descriptor in wal_descriptors:
                raise OSError("Disposable validation failure")
            return original_fstat(descriptor)
        with self.blocked(), patch("codex_source.os.open", side_effect=open_file), \
                patch("codex_source.os.fstat", side_effect=metadata):
            rows, error = self.source._thread_rows(0)
        self.assertTrue(wal_descriptors)
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)
        self.assert_descriptors_closed(descriptors)
        self.assert_clean()

    def test_empty_wal_open_permission_error_closes_root(self):
        wal = self.empty_wal()
        before = self.wal_metadata(wal)
        original_open, descriptors, attempted = os.open, [], []
        def open_file(name, *args, **kwargs):
            if name == wal.name:
                attempted.append(True)
                raise PermissionError("Disposable read-only WAL access failure")
            descriptor = original_open(name, *args, **kwargs)
            descriptors.append(descriptor)
            return descriptor
        with self.blocked(), patch("codex_source.os.open", side_effect=open_file):
            rows, error = self.source._thread_rows(0)
        self.assertTrue(attempted)
        self.assertGreaterEqual(len(descriptors), 1)
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)
        self.assert_descriptors_closed(descriptors)
        self.assertEqual(self.wal_metadata(wal), before)
        self.assertEqual(self.database.read_bytes(), self.original_bytes)
        self.assertEqual({path.name for path in self.root.iterdir()}, {"state_5.sqlite", wal.name})
        self.assert_clean()

    def test_initially_absent_wal_cannot_appear_during_copy(self):
        self.copy_mutation(self.empty_wal)
        self.assertEqual(self.database.read_bytes(), self.original_bytes)

    def test_empty_wal_mutations_during_copy_are_rejected(self):
        wal = self.root / "state_5.sqlite-wal"
        for change in ("remove", "replace", "append", "grow-truncate", "mode", "journal"):
            with self.subTest(change=change):
                self.fresh_reader()
                wal.touch(mode=0o600)
                original = wal.stat()
                def mutate():
                    if change == "remove":
                        wal.unlink()
                    elif change == "replace":
                        replacement = self.root / "new-wal"
                        replacement.touch(mode=0o600)
                        os.replace(replacement, wal)
                    elif change == "append":
                        wal.write_bytes(b"writer started")
                    elif change == "grow-truncate":
                        wal.write_bytes(b"writer started")
                        wal.write_bytes(b"")
                        os.utime(wal, ns=(original.st_atime_ns, original.st_mtime_ns))
                    elif change == "mode":
                        wal.chmod(0o644)
                    else:
                        (self.root / "state_5.sqlite-journal").touch()
                self.copy_mutation(mutate)
                self.assertEqual(self.database.read_bytes(), self.original_bytes)
                wal.unlink(missing_ok=True)
                (self.root / "state_5.sqlite-journal").unlink(missing_ok=True)

    def test_empty_wal_mutations_after_copy_during_query_are_rejected(self):
        wal = self.root / "state_5.sqlite-wal"
        for change in ("remove", "replace", "grow-truncate", "journal"):
            with self.subTest(change=change):
                self.fresh_reader()
                wal.touch(mode=0o600)
                original = wal.stat()
                with self.blocked(), self.assertRaises(ValueError):
                    with self.source._index_connection(self.database) as connection:
                        self.assertEqual(connection.execute("SELECT count(*) FROM threads").fetchone()[0], 2)
                        if change == "remove":
                            wal.unlink()
                        elif change == "replace":
                            replacement = self.root / "new-wal"
                            replacement.touch(mode=0o600)
                            os.replace(replacement, wal)
                        elif change == "grow-truncate":
                            wal.write_bytes(b"writer started")
                            wal.write_bytes(b"")
                            os.utime(wal, ns=(original.st_atime_ns, original.st_mtime_ns))
                        else:
                            (self.root / "state_5.sqlite-journal").touch()
                self.assert_clean()
                self.assertEqual(self.database.read_bytes(), self.original_bytes)
                wal.unlink(missing_ok=True)
                (self.root / "state_5.sqlite-journal").unlink(missing_ok=True)

    def test_copy_retry_does_not_rebaseline_changed_empty_wal(self):
        wal = self.empty_wal()
        original = wal.stat()
        def mutate():
            with self.database.open("r+b") as writer:
                writer.seek(60)
                writer.write((1).to_bytes(4, "big"))
            wal.write_bytes(b"writer started")
            wal.write_bytes(b"")
            os.utime(wal, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.copy_mutation(mutate)
        self.assertEqual(wal.stat().st_size, 0)
        self.assertEqual(wal.stat().st_mtime_ns, original.st_mtime_ns)
        self.assertNotEqual(wal.stat().st_ctime_ns, original.st_ctime_ns)

    def test_empty_wal_normal_and_query_error_close_all_descriptors(self):
        wal = self.empty_wal()
        before = self.wal_metadata(wal)
        for fail_query in (False, True):
            with self.subTest(query_error=fail_query):
                self.fresh_reader()
                count = len(os.listdir("/dev/fd"))
                descriptors, tracker = self.tracked_opens()
                try:
                    with self.blocked(), tracker:
                        with self.source._index_connection(self.database) as connection:
                            connection.execute("SELECT nonexistent_column FROM threads" if fail_query else "SELECT 1")
                except sqlite3.Error:
                    self.assertTrue(fail_query)
                else:
                    self.assertFalse(fail_query)
                self.assertGreaterEqual(len(descriptors), 3)
                if not fail_query:
                    self.assertIsNotNone(self.source._cached_index)
                    self.source.close()
                self.assert_descriptors_closed(descriptors)
                self.assertEqual(len(os.listdir("/dev/fd")), count)
                self.assertEqual(self.wal_metadata(wal), before)
                self.assertEqual(self.database.read_bytes(), self.original_bytes)
                self.assert_clean()

    def test_wrong_failed_filename_never_opens_or_copies(self):
        with patch("codex_source.sqlite3.connect", side_effect=AssertionError("No other database may be opened")):
            with self.assertRaises(ValueError):
                with self.source._index_connection(self.root / "other.sqlite"):
                    self.fail("An unapproved index was yielded")
        self.assert_clean()

    def test_query_error_closes_all_source_descriptors(self):
        original_open = os.open
        descriptors = []
        def open_file(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            descriptors.append(descriptor)
            return descriptor
        with self.blocked(), patch("codex_source.os.open", side_effect=open_file), self.assertRaises(sqlite3.Error):
            with self.source._index_connection(self.database) as connection:
                connection.execute("SELECT nonexistent_column FROM threads")
        self.assertGreaterEqual(len(descriptors), 2)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assert_clean()

    def test_sigkill_after_copy_leaves_no_named_database_data(self):
        self.assert_sigkill_after_copy()

    def test_sigkill_with_empty_wal_preserves_source_and_sidecar(self):
        wal = self.empty_wal()
        before = self.wal_metadata(wal)
        self.assert_sigkill_after_copy()
        self.assertEqual(self.wal_metadata(wal), before)
        self.assertEqual(wal.read_bytes(), b"")

    def assert_sigkill_after_copy(self):
        ready = self.private.parent / "ready.txt"
        repository = Path(__file__).resolve().parents[1]
        program = """from pathlib import Path
import os,sys,time
sys.path.insert(0,sys.argv[1])
from codex_source import _private_index_snapshot
with _private_index_snapshot(Path(sys.argv[2])) as snapshot:
    Path(sys.argv[4]).write_text("memory" if snapshot.connection.execute("PRAGMA database_list").fetchone()[2] == "" else "unexpected-file")
    time.sleep(30)
"""
        process = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c", program,
                                    str(repository), str(self.root), str(self.private), str(ready)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(ready.exists(), "The disposable child did not finish copying")
            self.assertEqual(ready.read_text(), "memory")
            self.assert_clean()
            process.kill()
            process.wait(timeout=3)
            self.assertEqual(process.returncode, -9)
            self.assert_clean()
            self.assertEqual(self.database.read_bytes(), self.original_bytes)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            process.stderr.close()
