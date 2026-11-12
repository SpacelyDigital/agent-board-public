"""Authorized folder discovery rejects redirected subtrees before listing."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from claude_source import ClaudeSource
from source_access import source_glob


class SourceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.selected = self.root / "selected"
        self.selected.mkdir()

    def test_project_directory_symlink_is_not_enumerated_and_good_siblings_survive(self):
        good = self.selected / "projects/good/session.jsonl"
        good.parent.mkdir(parents=True)
        good.write_text("{}\n")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.jsonl").write_text("private fixture")
        redirected = self.selected / "projects/redirected"
        redirected.symlink_to(outside, target_is_directory=True)
        actual_scandir = os.scandir
        visited = []

        def scan(path):
            visited.append(Path(path))
            self.assertNotEqual(Path(path), redirected)
            self.assertNotEqual(Path(path), outside)
            return actual_scandir(path)

        errors = []
        with patch("source_access.os.scandir", side_effect=scan):
            found = list(source_glob(self.selected, "projects/*/*.jsonl", errors))
        self.assertEqual(found, [good])
        self.assertTrue(errors)
        self.assertIn(good.parent, visited)

    def test_derived_desktop_sessions_symlink_does_not_become_a_new_authorized_root(self):
        # Retain the legacy caller boundary even though the Store picker now
        # grants the sessions directory itself.
        support = self.selected / "Claude"
        support.mkdir()
        outside = self.root / "outside-sessions"
        metadata = outside / "account/org/local_fixture.json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps(dict(sessionId="local_fixture", cliSessionId="private-fixture", title="Outside scope")))
        sessions = support / "claude-code-sessions"
        sessions.symlink_to(outside, target_is_directory=True)
        source = ClaudeSource(self.selected, desktop_root=sessions, desktop_scope_root=support,
                              confine_paths=True, allow_commands=False)
        with patch("source_access.os.scandir", side_effect=AssertionError("Redirected root must not be listed")), \
                patch.object(Path, "open", side_effect=AssertionError("Outside metadata must not be read")):
            self.assertEqual(source._desktop_sessions(), {})
        self.assertFalse(source._desktop_index_complete)

    def test_narrow_desktop_grant_keeps_valid_metadata_and_rejects_linked_subtrees(self):
        sessions = self.selected / "claude-code-sessions"
        good = sessions / "account/org/local_fixture.json"
        good.parent.mkdir(parents=True)
        good.write_text(json.dumps(dict(sessionId="local_fixture", cliSessionId="allowed-fixture",
                                        title="Inside sessions scope")))
        outside = self.root / "outside-sessions"
        metadata = outside / "org/local_private.json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps(dict(sessionId="local_private", cliSessionId="private-fixture",
                                            title="Outside scope")))
        redirected = sessions / "redirected-account"
        redirected.symlink_to(outside, target_is_directory=True)
        source = ClaudeSource(self.selected, desktop_root=sessions, desktop_scope_root=sessions,
                              confine_paths=True, allow_commands=False)
        actual_scandir, actual_open = os.scandir, Path.open

        def scan(path):
            self.assertNotIn(Path(path), (outside, redirected))
            return actual_scandir(path)

        def read(path, *args, **kwargs):
            self.assertTrue(Path(path).is_relative_to(sessions))
            self.assertFalse(Path(path).is_relative_to(redirected))
            return actual_open(path, *args, **kwargs)

        with patch("source_access.os.scandir", side_effect=scan), patch.object(Path, "open", new=read):
            self.assertEqual(set(source._desktop_sessions()), {"allowed-fixture"})
        self.assertFalse(source._desktop_index_complete)

    def test_user_selected_root_alias_remains_allowed_but_child_links_and_special_files_do_not(self):
        alias = self.root / "selected-alias"
        alias.symlink_to(self.selected, target_is_directory=True)
        good = self.selected / "regular.json"
        good.write_text("{}")
        (self.selected / "linked.json").symlink_to(good)
        (self.selected / "directory.json").mkdir()
        os.mkfifo(self.selected / "pipe.json")
        errors = []
        self.assertEqual(list(source_glob(alias, "*.json", errors)), [alias / "regular.json"])
        self.assertEqual(len(errors), 3)

    def test_discovery_pattern_cannot_expand_outside_selected_root(self):
        for pattern in ("../*.json", "/tmp/*.json", "**/*.json"):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                list(source_glob(self.selected, pattern))


if __name__ == "__main__":
    unittest.main()
