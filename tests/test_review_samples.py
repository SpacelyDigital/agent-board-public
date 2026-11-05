"""Standalone reviewer samples never adopt existing provider data."""

from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from server import Monitor


TOOL = Path(__file__).resolve().parents[1] / "tools/review_samples.py"
SPEC = importlib.util.spec_from_file_location("agent_board_review_samples", TOOL)
samples = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(samples)


class ReviewSampleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()

    def hashes(self):
        return {str(path.relative_to(self.home)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.home.rglob("*") if path.is_file() and not path.is_symlink()}

    def initialize(self):
        return samples.initialize(self.home)

    def test_init_refuses_every_existing_provider_root_without_other_writes(self):
        for relative in samples.ROOTS:
            for kind in ("directory", "file", "symlink", "dangling-symlink"):
                with self.subTest(root=relative, kind=kind), tempfile.TemporaryDirectory() as temporary:
                    home = Path(temporary).resolve()
                    root = home / relative
                    root.parent.mkdir(parents=True, exist_ok=True)
                    outside = home / "unselected-fixture"
                    outside.write_text("Unselected synthetic content must be preserved.")
                    if kind == "directory":
                        root.mkdir()
                        (root / "existing.txt").write_text("Existing synthetic content.")
                    elif kind == "file":
                        root.write_text("Existing synthetic file.")
                    else:
                        root.symlink_to(outside if kind == "symlink" else home / "missing-fixture")
                    before = sorted(str(path.relative_to(home)) for path in home.rglob("*"))
                    with self.assertRaises(samples.SampleError):
                        samples.initialize(home)
                    self.assertEqual(before, sorted(str(path.relative_to(home)) for path in home.rglob("*")))
                    self.assertEqual(outside.read_text(), "Unselected synthetic content must be preserved.")
                    self.assertFalse((home / samples.CONTROL).exists())

    def test_symlinked_parent_is_rejected_before_any_creation(self):
        outside = self.home / "outside"
        outside.mkdir()
        (self.home / "Library").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.initialize()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.home / ".codex").exists())
        self.assertFalse((self.home / samples.CONTROL).exists())

    def test_database_paths_and_ids_belong_to_the_generated_home(self):
        result = self.initialize()
        header = json.loads((self.home / samples.CONTROL).read_bytes().splitlines()[0])
        for identifier in header["ids"].values():
            self.assertEqual(str(uuid.UUID(identifier)), identifier)
        with sqlite3.connect(self.home / ".codex/state_5.sqlite") as connection:
            row = connection.execute("SELECT id,rollout_path,updated_at FROM threads").fetchone()
        self.assertEqual(row[0], header["ids"]["codex"])
        self.assertTrue(Path(row[1]).is_relative_to(self.home / ".codex/sessions"))
        self.assertTrue(Path(row[1]).is_file())
        self.assertLess(abs(row[2] - time.time()), 5)
        self.assertEqual(set(result["folders"]), {str(self.home / relative) for relative in samples.ROOTS})
        self.assertEqual(result["provider_requests"], 0)
        before = self.hashes()
        with self.assertRaises(samples.SampleError):
            self.initialize()
        self.assertEqual(self.hashes(), before)

    def test_real_readers_exercise_lifecycle_quota_review_and_restart(self):
        self.initialize()
        grants = dict.fromkeys(("codex", "claude", "claude_desktop"), True)

        def monitor(selected=grants):
            return Monitor(self.home / "board-data", self.home / ".codex", self.home / ".claude",
                           claude_desktop_root=self.home / "Library/Application Support/Claude",
                           sandbox=True, source_grants=selected, store_profile=True)

        board = monitor()
        # Match the review instructions: after granting folders, create fresh
        # activity rather than importing a Claude event predating activation.
        samples.advance(self.home, "cooking")
        identities = None
        for action, expected in ((None, "cooking"), ("needs-you", "needs_you"),
                                 ("cooking", "cooking"), ("ready", "ready")):
            if action:
                samples.advance(self.home, action)
            before = samples.status(self.home)
            for _ in range(20):
                board.scan()
                state = board.snapshot()
                if len(state["sessions"]) == 2 and all(not item["history_checking"] for item in state["sessions"]):
                    break
            self.assertEqual(len(state["sessions"]), 2)
            self.assertEqual({item["status"] for item in state["sessions"]}, {expected})
            current = {item["provider"]: item["id"] for item in state["sessions"]}
            self.assertEqual(len(set(current.values())), 2)
            if identities is not None:
                self.assertEqual(current, identities)
            identities = current
            self.assertEqual(samples.status(self.home), before)
        quotas = {item["id"]: item for item in state["usage"]}
        self.assertEqual(quotas["codex"]["windows"][0]["remaining_percent"], 73)
        self.assertEqual([item["remaining_percent"] for item in quotas["claude"]["windows"]], [82, 91])
        self.assertTrue(all(item["preview"] == "The synthetic review sample is ready." for item in state["sessions"]))
        reviewed = next(item for item in state["sessions"] if item["provider"] == "codex")
        board.store.review(reviewed["key"], reviewed["event_id"])
        restarted = monitor()
        restarted.scan()
        persisted = restarted.snapshot()
        self.assertEqual(persisted["counts"]["ready"], 1)
        self.assertEqual(len(persisted["history"]), 1)
        self.assertEqual(persisted["history"][0]["reviewed_event"], reviewed["event_id"])
        revoked = monitor(dict.fromkeys(grants, False))
        revoked.scan()
        self.assertTrue(all(not item["available"] for item in revoked.snapshot()["sources"]))
        self.assertTrue(revoked.snapshot()["sessions"][0]["source_unavailable"])
        samples.status(self.home)  # Readers must not alter any generated input.

    def test_unrelated_provider_file_stops_actions_without_reading_or_modifying_it(self):
        self.initialize()
        foreign = self.home / ".codex/not-a-review-sample.txt"
        foreign.write_text("Unrelated synthetic content.")
        before = self.hashes()
        with self.assertRaises(samples.SampleError):
            samples.advance(self.home, "needs-you")
        self.assertEqual(self.hashes(), before)

    def test_external_edit_or_partial_journal_is_preserved_and_not_repaired(self):
        self.initialize()
        marker = self.home / samples.CONTROL
        before = self.hashes()
        with marker.open("ab") as stream:
            stream.write(b'{"phase":')
        changed = self.hashes()
        with self.assertRaises(samples.SampleError):
            samples.advance(self.home, "needs-you")
        self.assertEqual(self.hashes(), changed)
        self.assertEqual({path: digest for path, digest in before.items() if path != samples.CONTROL},
                         {path: digest for path, digest in changed.items() if path != samples.CONTROL})

    def test_replaced_provider_root_is_never_followed(self):
        self.initialize()
        original = self.home / ".codex"
        original.rename(self.home / "preserved-codex-samples")
        outside = self.home / "outside"
        outside.mkdir()
        original.symlink_to(outside, target_is_directory=True)
        before = self.hashes()
        with self.assertRaises((OSError, samples.SampleError)):
            samples.advance(self.home, "needs-you")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.hashes(), before)

    def test_cli_uses_current_home_without_network_or_credential_prompts(self):
        output = io.StringIO()
        with patch.object(samples.Path, "home", return_value=self.home), \
                patch("builtins.input", side_effect=AssertionError("No credential input is allowed")), \
                patch("socket.socket", side_effect=AssertionError("No network is allowed")), redirect_stdout(output):
            samples.main(["init"])
        self.assertTrue(json.loads(output.getvalue())["synthetic"])
        before = self.hashes()
        with patch.object(samples.os, "geteuid", return_value=0), self.assertRaises(samples.SampleError):
            samples.advance(self.home, "needs-you")
        self.assertEqual(self.hashes(), before)


if __name__ == "__main__":
    unittest.main()
