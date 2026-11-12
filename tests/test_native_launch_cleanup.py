"""Verify native crash cleanup never treats source/checkpoint files as owned."""

import json
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from sandbox_sources import activate_source_bookmarks
from server import NativeLaunchFiles


class NativeLaunchCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.support = Path(self.temporary.name).resolve() / "support"
        self.data = self.support / "data"
        self.data.mkdir(parents=True, mode=0o700)
        self.instance = "11111111-1111-4111-8111-111111111111"
        self.token = "a" * 64
        self.token_path = self.data / (".collector-access-" + self.instance)
        self.token_path.write_text(self.token)
        self.token_path.chmod(0o600)
        self.manifest = self.data / (".source-bookmarks-" + self.instance + ".json")
        self.roots = {provider: self.support / "sources-unavailable" / provider
                      for provider in ("codex", "claude", "claude_desktop")}
        for folder in self.roots.values():
            folder.mkdir(parents=True)
        self.manifest.write_text(json.dumps({"version": 1, "sources": {
            provider: {"path": str(root)} for provider, root in self.roots.items()}}))
        self.manifest.chmod(0o600)
        self.observation = {}
        activate_source_bookmarks(self.manifest, self.roots, self.support, observation=self.observation)

    def capture(self, **changes):
        values = dict(data_dir=self.data, instance_id=self.instance, token_path=self.token_path,
                      token=self.token, manifest_path=self.manifest, parent_lifetime=True,
                      manifest_fingerprint=self.observation["fingerprint"])
        values.update(changes)
        capture = NativeLaunchFiles(**values)
        self.addCleanup(capture.cleanup)
        return capture

    def test_only_matching_validated_launch_files_are_removed(self):
        old = self.data / ".collector-access-22222222-2222-4222-8222-222222222222"
        old.write_text(self.token)
        old.chmod(0o600)
        checkpoint = self.data / "checkpoint.json"
        checkpoint.write_text("Preserve this checkpoint.")
        capture = self.capture()
        self.assertEqual(set(capture.cleanup()), {self.token_path.name, self.manifest.name})
        self.assertEqual(capture.cleanup(), [])
        self.assertEqual(old.read_text(), self.token)
        self.assertEqual(checkpoint.read_text(), "Preserve this checkpoint.")

    def test_modified_file_is_preserved(self):
        capture = self.capture()
        self.manifest.write_text("A modified file is no longer this launch's validated manifest.")
        self.assertEqual(capture.cleanup(), [self.token_path.name])
        self.assertTrue(self.manifest.exists())

    def test_replaced_inode_with_identical_bytes_is_preserved(self):
        capture = self.capture()
        replacement = self.data / "replacement"
        replacement.write_bytes(self.token_path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, self.token_path)
        self.assertEqual(capture.cleanup(), [self.manifest.name])
        self.assertTrue(self.token_path.exists())

    def test_unvalidated_manifest_is_preserved(self):
        capture = self.capture(manifest_fingerprint=None)
        self.assertEqual(capture.cleanup(), [self.token_path.name])
        self.assertTrue(self.manifest.exists())

    def test_symlink_shared_token_and_wrong_token_do_not_authorize_cleanup(self):
        for mode in ("symlink", "hardlink", "wrong-token"):
            with self.subTest(mode=mode):
                target = self.data / ("preserve-" + mode)
                target.write_text(self.token)
                target.chmod(0o600)
                self.token_path.unlink()
                if mode == "symlink":
                    self.token_path.symlink_to(target)
                elif mode == "hardlink":
                    os.link(target, self.token_path)
                else:
                    self.token_path.write_text("b" * 64)
                    self.token_path.chmod(0o600)
                self.assertEqual(self.capture().cleanup(), [])
                self.assertEqual(target.read_text(), self.token)
                self.assertTrue(self.manifest.exists())

    def test_modified_or_shared_manifest_is_preserved_after_capture(self):
        capture = self.capture()
        linked = self.data / "preserved-manifest-link"
        os.link(self.manifest, linked)
        self.assertEqual(capture.cleanup(), [self.token_path.name])
        self.assertTrue(self.manifest.exists())
        self.assertTrue(linked.exists())

    def test_moved_data_directory_becomes_a_preserved_checkpoint(self):
        capture = self.capture()
        checkpoint = self.support / "checkpoint-data"
        self.data.rename(checkpoint)
        self.data.mkdir(mode=0o700)
        self.token_path.write_text("replacement-directory-canary")
        self.assertEqual(capture.cleanup(), [])
        self.assertTrue((checkpoint / self.token_path.name).exists())
        self.assertEqual(self.token_path.read_text(), "replacement-directory-canary")

    def test_invalid_identity_or_owner_lifetime_never_authorizes_deletion(self):
        for changed in ({"instance_id": "not-native"}, {"parent_lifetime": False},
                        {"token_path": self.manifest}, {"token": "wrong"}):
            with self.subTest(changed=changed):
                self.assertEqual(self.capture(**changed).cleanup(), [])
                self.assertTrue(self.token_path.exists())
                self.assertTrue(self.manifest.exists())

    def test_public_permissions_never_authorize_deletion(self):
        self.data.chmod(0o755)
        self.assertEqual(self.capture().cleanup(), [])
        self.data.chmod(0o700)
        self.token_path.chmod(0o644)
        self.assertEqual(self.capture().cleanup(), [])
        self.assertTrue(self.token_path.exists())

    def test_already_closed_native_parent_clears_only_its_private_token(self):
        entry = Path(__file__).resolve().parents[1] / "server.py"
        result = subprocess.run([sys.executable, str(entry), "--data-dir", str(self.data),
                                 "--access-token-file", str(self.token_path), "--instance-id", self.instance,
                                 "--parent-lifetime-stdin", "--codex-root", str(self.roots["codex"]),
                                 "--claude-root", str(self.roots["claude"]), "--no-usage", "--no-projects"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.token_path.exists())
        self.assertTrue(self.manifest.exists(), "An unvalidated manifest must be preserved.")
        self.assertFalse((self.data / "board.sqlite3").exists())

    def marker(self):
        marker = self.data / (".collector-launch-" + self.instance + ".lock")
        marker.touch(mode=0o600)
        return marker

    def test_collector_holds_shared_lease_after_owner_exit_and_removes_orphan_marker(self):
        marker = self.marker()
        owner = os.open(marker, os.O_RDONLY)
        fcntl.flock(owner, fcntl.LOCK_SH | fcntl.LOCK_NB)
        capture = self.capture()
        os.close(owner)
        contender = os.open(marker, os.O_RDONLY)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender)
        self.assertEqual(set(capture.cleanup()), {self.token_path.name, self.manifest.name})
        self.assertFalse(marker.exists())

    def test_live_owner_keeps_marker_until_its_own_cleanup(self):
        marker = self.marker()
        owner = os.open(marker, os.O_RDONLY)
        try:
            fcntl.flock(owner, fcntl.LOCK_SH | fcntl.LOCK_NB)
            capture = self.capture()
            capture.cleanup()
            self.assertTrue(marker.exists())
        finally:
            os.close(owner)

    def test_shared_or_substituted_marker_cannot_authorize_cleanup(self):
        marker = self.marker()
        linked = self.data / "keep-launch-marker"
        os.link(marker, linked)
        self.assertEqual(self.capture().cleanup(), [])
        self.assertTrue(self.token_path.exists())
        self.assertTrue(self.manifest.exists())
        marker.unlink()
        marker.symlink_to(linked)
        self.assertEqual(self.capture().cleanup(), [])
        self.assertTrue(self.token_path.exists())

    def test_manifest_is_captured_only_after_verification(self):
        marker = self.marker()
        capture = self.capture(manifest_path=None, manifest_fingerprint=None)
        self.assertNotIn(self.manifest.name, capture.entries)
        capture.capture_manifest(self.manifest, self.observation["fingerprint"])
        self.assertEqual(set(capture.cleanup()), {self.token_path.name, self.manifest.name})
        self.assertFalse(marker.exists())

    def test_unverified_manifest_keeps_its_marker_for_later_safe_sweep(self):
        marker = self.marker()
        capture = self.capture(manifest_fingerprint=None)
        self.assertEqual(capture.cleanup(), [self.token_path.name])
        self.assertTrue(marker.exists())
        self.assertTrue(self.manifest.exists())


if __name__ == "__main__":
    unittest.main()
