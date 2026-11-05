"""Exercise checkpointed index fallback without granting source writes."""

import os
from pathlib import Path
import sqlite3
import tempfile
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
        self.source = CodexSource(self.root, confine_paths=True, snapshot_parent=self.private)

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

    def test_selected_root_alias_allows_the_direct_index(self):
        alias = self.root.parent / "selected-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        connection = self.original_connect(self.database)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.close()
        source = CodexSource(alias, confine_paths=True, snapshot_parent=self.private)
        rows, error = source._thread_rows(0)
        self.assertIsNone(error)
        self.assertEqual([row["id"] for row in rows], ["live"])
        self.assertEqual(source._excluded_ids(), {"archived", "child"})
        self.assert_clean()

    def test_selected_root_alias_allows_only_the_checkpointed_fallback(self):
        alias = self.root.parent / "selected-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        source = CodexSource(alias, confine_paths=True, snapshot_parent=self.private)
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
        source = CodexSource(alias, confine_paths=True, snapshot_parent=self.private)
        with patch("codex_source.sqlite3.connect", side_effect=AssertionError("A child symlink must not be opened")):
            rows, error = source._thread_rows(0)
            excluded = source._excluded_ids()
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)
        self.assertEqual(excluded, set())
        self.assertEqual(outside.read_bytes(), self.original_bytes)
        self.assert_clean()

    def test_private_directory_file_modes_and_query_only(self):
        with self.blocked(), self.source._index_connection(self.database) as connection:
            self.assertEqual(self.private.stat().st_mode & 0o777, 0o700)
            filename = connection.execute("PRAGMA database_list").fetchone()[2]
            descriptor = int(filename.rsplit("/", 1)[1])
            metadata = os.fstat(descriptor)
            self.assertEqual(metadata.st_mode & 0o777, 0o600)
            self.assertEqual(metadata.st_nlink, 0)
            self.assert_clean()
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM threads")
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

    def test_cooperative_deadline_after_partial_copy_closes_unlinked_data(self):
        connection = self.original_connect(self.database)
        connection.execute("CREATE TABLE padding (value BLOB)")
        connection.execute("INSERT INTO padding VALUES (?)", (b"x" * (2 * 1024 * 1024),))
        connection.commit()
        connection.close()
        before = self.database.read_bytes()
        original_write = os.write
        written = []
        def write(descriptor, data):
            written.append(len(data))
            return original_write(descriptor, data)
        with self.blocked(), patch("codex_source.time.monotonic", side_effect=[0, 0, 0, 2]), \
                patch("codex_source.os.write", side_effect=write):
            rows, error = self.source._thread_rows(0)
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)
        self.assertGreater(sum(written), 0)
        self.assertLess(sum(written), len(before))
        self.assertEqual(self.database.read_bytes(), before)
        self.assert_clean()

    def test_source_symlink_hardlink_and_fifo_are_rejected(self):
        original = self.root / "original.sqlite"
        self.database.rename(original)
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
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
        with self.blocked(sqlite3.SQLITE_CORRUPT), patch("codex_source._private_index_copy", side_effect=AssertionError("No fallback")):
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
        for source in (CodexSource(self.root, confine_paths=False, snapshot_parent=self.private),
                       CodexSource(self.root, confine_paths=True)):
            with self.blocked():
                rows, error = source._thread_rows(0)
            self.assertEqual(rows, [])
            self.assertIsNotNone(error)
            self.assert_clean()

    def test_nonprivate_or_source_nested_storage_is_rejected(self):
        self.private.chmod(0o755)
        nested = self.root / "scratch"
        nested.mkdir(mode=0o700)
        for parent in (self.private, nested):
            before = list(parent.iterdir())
            source = CodexSource(self.root, confine_paths=True, snapshot_parent=parent)
            with self.blocked():
                rows, error = source._thread_rows(0)
            self.assertEqual(rows, [])
            self.assertIsNotNone(error)
            self.assertEqual(list(parent.iterdir()), before)

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
        self.assertTrue(direct.closed)
        self.assert_clean()

    def test_every_copy_write_targets_an_unlinked_private_file(self):
        original_write = os.write
        writes = []
        def write(descriptor, data):
            self.assertEqual(os.fstat(descriptor).st_nlink, 0)
            self.assertEqual(os.fstat(descriptor).st_mode & 0o777, 0o600)
            self.assert_clean()
            writes.append(len(data))
            return original_write(descriptor, data)
        with self.blocked(), patch("codex_source.os.write", side_effect=write):
            rows, error = self.source._thread_rows(0)
        self.assertIsNone(error)
        self.assertEqual(len(rows), 1)
        self.assertEqual(sum(writes), len(self.original_bytes))
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

    def test_scratch_name_replacement_preserves_foreign_file_and_links(self):
        outside = self.private.parent / "unrelated.bin"
        outside.write_bytes(b"Keep this unrelated file")
        outside.chmod(0o600)
        original_stat = os.stat
        for kind in ("file", "symlink", "hardlink"):
            replaced = []
            def metadata(name, *args, **kwargs):
                directory = kwargs.get("dir_fd")
                if isinstance(name, str) and name.startswith(".codex-index-") and directory is not None and not replaced:
                    replaced.append(name)
                    os.unlink(name, dir_fd=directory)
                    if kind == "symlink":
                        os.symlink(outside, name, dir_fd=directory)
                    elif kind == "hardlink":
                        os.link(outside, name, dst_dir_fd=directory)
                    else:
                        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
                        os.write(descriptor, b"Keep this replacement")
                        os.close(descriptor)
                return original_stat(name, *args, **kwargs)
            with self.subTest(kind=kind), self.blocked(), patch("codex_source.os.stat", side_effect=metadata):
                rows, error = self.source._thread_rows(0)
            self.assertEqual(rows, [])
            self.assertIsNotNone(error)
            self.assertEqual(outside.read_bytes(), b"Keep this unrelated file")
            replacement = self.private / replaced[0]
            self.assertTrue(replacement.exists())
            replacement.unlink()
        self.assert_clean()

    def test_parent_path_replacement_cannot_redirect_copy_or_cleanup(self):
        foreign = self.private.parent / "foreign"
        foreign.mkdir(mode=0o700)
        (foreign / "notes.txt").write_bytes(b"Keep this unrelated file")
        retired = self.private.parent / "retired-private"
        original_open = os.open
        replaced = []
        def open_file(name, *args, **kwargs):
            if isinstance(name, str) and name.startswith(".codex-index-") and not replaced:
                replaced.append(True)
                self.private.rename(retired)
                self.private.symlink_to(foreign, target_is_directory=True)
            return original_open(name, *args, **kwargs)
        try:
            with self.blocked(), patch("codex_source.os.open", side_effect=open_file):
                rows, error = self.source._thread_rows(0)
            self.assertIsNone(error)
            self.assertEqual(len(rows), 1)
            self.assertEqual(list(retired.iterdir()), [])
            self.assertEqual({path.name for path in foreign.iterdir()}, {"notes.txt"})
            self.assertEqual((foreign / "notes.txt").read_bytes(), b"Keep this unrelated file")
        finally:
            if self.private.is_symlink():
                self.private.unlink()
                retired.rename(self.private)
        self.assert_clean()

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
                self.assertTrue(connection.execute("PRAGMA database_list").fetchone()[2].startswith("/dev/fd/"))
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

    def test_empty_wal_open_permission_error_closes_root_and_storage(self):
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
        self.assertGreaterEqual(len(descriptors), 2)
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
                self.assertGreaterEqual(len(descriptors), 5)
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

    def test_query_error_closes_all_private_and_source_descriptors(self):
        original_open = os.open
        descriptors = []
        def open_file(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            descriptors.append(descriptor)
            return descriptor
        with self.blocked(), patch("codex_source.os.open", side_effect=open_file), self.assertRaises(sqlite3.Error):
            with self.source._index_connection(self.database) as connection:
                connection.execute("SELECT nonexistent_column FROM threads")
        self.assertGreaterEqual(len(descriptors), 4)
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
from codex_source import _private_index_copy
with _private_index_copy(Path(sys.argv[2]),Path(sys.argv[3])) as descriptor:
    Path(sys.argv[4]).write_text(str(os.fstat(descriptor).st_nlink))
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
            self.assertEqual(ready.read_text(), "0")
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
