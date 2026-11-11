"""Reproduce Codex stale-ready and source-scope failures with synthetic files."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import tracemalloc
import unittest
from unittest.mock import patch

from codex_source import CodexSource
from local_quota import parse_codex_observation


class CodexHistoryVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.now = time.time()
        self.identifier = "11111111-1111-4111-8111-111111111111"
        self.path = self.root / "sessions" / time.strftime("%Y/%m/%d", time.gmtime(self.now)) / "rollout-fixture.jsonl"
        self.path.parent.mkdir(parents=True)
        self.database = self.root / "state_5.sqlite"
        with sqlite3.connect(self.database) as database:
            database.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER, title TEXT)")
            database.execute("INSERT INTO threads VALUES (?,?,?,?)", (self.identifier, str(self.path), int(self.now), "Disposable Codex fixture"))
        self.source = CodexSource(self.root, confine_paths=True)
        self.append({"type": "session_meta", "payload": {"id": self.identifier, "thread_source": "user", "cwd": "/fixture"}})

    def append(self, *records):
        with self.path.open("ab") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False).encode() + b"\n")

    def event(self, kind, turn="turn-1", timestamp=None, **fields):
        return {"type": "event_msg", "timestamp": timestamp or self.now,
                "payload": {"type": kind, "turn_id": turn, **fields}}

    def scan(self, source=None):
        observations, health = (source or self.source).scan(self.now + 1, self.now - 3600)
        self.assertTrue(health["available"])
        self.assertEqual(len(observations), 1)
        return observations[0]

    def drain(self, source=None, limit=1200):
        source = source or self.source
        for _ in range(limit):
            observation = self.scan(source)
            self.assertLessEqual(sum(cursor.buffer_size for cursor in source._cursors.values()), 16 * 1024 * 1024)
            if all(cursor.phase == "idle" for cursor in source._cursors.values()):
                return observation
        self.fail("The bounded Codex history verification did not finish")

    def ready(self):
        self.append(self.event("task_started", timestamp=self.now - 5), self.event("task_complete", timestamp=self.now - 1))
        result = self.drain()
        self.assertEqual(result["status"], "ready")
        return result

    def test_scan_never_opens_symlinked_index_through_archive_exclusion(self):
        outside = self.root / "unselected.sqlite"
        self.database.replace(outside)
        self.database.symlink_to(outside)
        with patch("codex_source.sqlite3.connect", side_effect=AssertionError("Unselected index must not be opened")):
            observations, health = self.source.scan(self.now, self.now - 60)
        self.assertTrue(all(row["status"] == "disconnected" for row in observations))

    def test_partial_new_turn_cannot_reuse_confirmed_ready(self):
        previous = self.ready()
        record = json.dumps(self.event("task_started", "turn-2")).encode()
        with self.path.open("ab") as stream:
            stream.write(record[:-1])
        incomplete = self.scan()
        self.assertEqual(incomplete["status"], "disconnected")
        self.assertTrue(incomplete["history_checking"])
        self.assertFalse(incomplete["history_verified"])
        self.assertNotIn("history_revision", incomplete)
        with self.path.open("ab") as stream:
            stream.write(record[-1:] + b"\n")
        cooking = self.drain()
        self.assertEqual(cooking["status"], "cooking")
        self.assertEqual(cooking["turn_id"], "turn-2")
        self.assertNotEqual(cooking["event_id"], previous["event_id"])

    def test_unindexed_duplicate_identity_never_alternates_verified_status(self):
        self.ready()
        self.database.unlink()
        duplicate = self.path.with_name("rollout-conflicting-copy.jsonl")
        duplicate.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": self.identifier, "thread_source": "user"}}) + "\n" +
            json.dumps(self.event("task_started", "other-turn")) + "\n")
        event_ids = set()
        for _ in range(6):
            observation = self.scan()
            self.assertEqual(observation["status"], "disconnected")
            self.assertTrue(observation["history_checking"])
            self.assertFalse(observation["history_verified"])
            self.assertNotIn("history_revision", observation)
            self.assertNotIn("local_rate_limits", observation)
            self.assertEqual(observation["preview"], "")
            event_ids.add(observation["event_id"])
        self.assertEqual(len(event_ids), 1)
        duplicate.unlink()
        restored = self.drain()
        self.assertEqual(restored["status"], "ready")
        self.assertTrue(restored["history_verified"])

    def test_distinct_unindexed_identities_retain_independent_status(self):
        self.ready()
        self.database.unlink()
        other_id = "22222222-2222-4222-8222-222222222222"
        other = self.path.with_name("rollout-other-chat.jsonl")
        other.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": other_id, "thread_source": "user"}}) + "\n" +
            json.dumps(self.event("task_started", "other-turn")) + "\n")
        for _ in range(4):
            observations, health = self.source.scan(self.now + 1, self.now - 3600)
            self.assertTrue(health["available"])
            self.assertEqual({row["id"]: row["status"] for row in observations},
                             {self.identifier: "ready", other_id: "cooking"})
            self.assertTrue(all(row["history_verified"] for row in observations))

    def test_same_inode_rewrite_growth_and_cold_revision_invalidate_stale_ready(self):
        previous = self.ready()
        inode = self.path.stat().st_ino
        self.path.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.identifier}}) + "\n")
        self.append(self.event("task_started", "replacement", timestamp=self.now - 50),
                    {"type": "turn_context", "payload": {"padding": "x" * 1500000}})
        self.assertEqual(self.path.stat().st_ino, inode)
        rewritten = self.drain()
        self.assertEqual(rewritten["status"], "cooking")
        self.assertNotEqual(rewritten["history_revision"]["generation"], previous["history_revision"]["generation"])
        cold = CodexSource(self.root, confine_paths=True)
        cold.revision_lookup = lambda identifier: previous["history_revision"]
        cold_result = self.drain(cold)
        self.assertEqual(cold_result["history_revision"], rewritten["history_revision"])

    def test_identical_atomic_replacement_keeps_generation_warm_and_cold(self):
        previous = self.ready()
        replacement = self.path.with_suffix('.replacement')
        replacement.write_bytes(self.path.read_bytes())
        replacement.replace(self.path)
        current = self.drain()
        self.assertNotEqual(current['history_revision']['file_id'], previous['history_revision']['file_id'])
        self.assertEqual(current['history_revision']['generation'], previous['history_revision']['generation'])
        self.assertEqual(current['event_id'], previous['event_id'])
        cold = CodexSource(self.root, confine_paths=True)
        cold.revision_lookup = lambda identifier: previous['history_revision']
        self.assertEqual(self.drain(cold)['history_revision'], current['history_revision'])

    def test_replacement_with_verified_identical_prefix_and_append_keeps_generation(self):
        previous = self.ready()
        replacement = self.path.with_suffix('.replacement')
        replacement.write_bytes(self.path.read_bytes())
        replacement.replace(self.path)
        self.append(self.event('task_complete', 'older-turn', timestamp=self.now - 50))
        current = self.drain()
        self.assertNotEqual(current['history_revision']['sha256'], previous['history_revision']['sha256'])
        self.assertEqual(current['history_revision']['generation'], previous['history_revision']['generation'])
        cold = CodexSource(self.root, confine_paths=True)
        cold.revision_lookup = lambda identifier: previous['history_revision']
        self.assertEqual(self.drain(cold)['history_revision'], current['history_revision'])

    def test_more_than_tail_limit_of_new_data_cannot_hide_new_turn(self):
        self.ready()
        self.append(self.event("task_started", "turn-2"), {"type": "response_item", "timestamp": self.now,
                    "payload": {"type": "function_call_output", "call_id": "fixture-tool", "output": "x" * (10 * 1024 * 1024)}})
        with patch("claude_source.TRANSCRIPT_POLL_BYTES", 256 * 1024):
            pending = self.scan()
            self.assertTrue(pending["history_checking"])
            self.assertNotEqual(pending["status"], "ready")
            complete = self.drain()
        self.assertEqual(complete["status"], "cooking")
        self.assertEqual(complete["turn_id"], "turn-2")
        self.append(self.event("task_complete", "turn-2"))
        self.assertEqual(self.drain()["status"], "ready")

    def test_large_metadata_image_and_tool_records_are_bounded_and_preserve_identity(self):
        with self.path.open("wb") as stream:
            stream.write(b'{"type":"session_meta","payload":{"base_instructions":"')
            for _ in range(1024):
                stream.write(b"A" * 65536)
            stream.write(('\",\"id\":\"' + self.identifier + '\",\"thread_source\":\"user\"}}\n').encode())
        self.append(self.event("task_started"), {"type": "response_item", "timestamp": self.now,
                    "payload": {"type": "function_call_output", "output": "PRIVATE-TOOL" * 1000000}},
                    self.event("task_complete", last_agent_message="Finished the synthetic large-history check."))
        tracemalloc.start()
        try:
            warm = self.drain()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 16 * 1024 * 1024)
        self.assertEqual(warm["status"], "ready")
        self.assertEqual(warm["id"], self.identifier)
        self.assertNotIn("PRIVATE-TOOL", json.dumps(warm))
        cold = self.drain(CodexSource(self.root, confine_paths=True))
        self.assertEqual(cold["history_revision"], warm["history_revision"])

    def test_canonical_first_metadata_survives_fork_history_and_rollback_clears_ready(self):
        self.append({"type": "session_meta", "payload": {"id": "copied-parent", "thread_source": "subagent"}},
                    self.event("task_started"), self.event("task_complete"))
        self.assertEqual(self.drain()["status"], "ready")
        self.append({"type": "event_msg", "timestamp": self.now + .1,
                     "payload": {"type": "thread_rolled_back", "num_turns": 1}})
        rolled_back = self.drain()
        self.assertEqual(rolled_back["status"], "disconnected")
        self.assertIn("history", rolled_back["detail"])
        self.append(self.event("task_started", "after-rollback", self.now + .2),
                    self.event("task_complete", "after-rollback", self.now + .3))
        self.assertEqual(self.drain()["status"], "ready")

    def test_nonindexed_copy_cannot_replace_unreadable_indexed_rollout(self):
        self.ready()
        copy = self.path.with_name("rollout-old-copy.jsonl")
        copy.write_bytes(self.path.read_bytes())
        self.path.unlink()
        observation = self.scan()
        self.assertEqual(observation["status"], "disconnected")
        self.assertIn("could not be read", observation["detail"])

    def test_missing_canonical_metadata_cannot_certify_an_indexed_chat(self):
        self.path.write_text(json.dumps(self.event("task_complete")) + "\n")
        observation = self.drain()
        self.assertEqual(observation["status"], "disconnected")
        self.assertTrue(observation["history_checking"])
        self.assertFalse(observation["history_verified"])

    def test_corrupt_complete_records_cannot_grant_ready_and_valid_replacement_recovers(self):
        self.ready()
        for bad in (b'not-json', b'{"type":"event_msg","type":"session_meta"}',
                    b'{"type":"turn_context","payload":{"hidden":NaN}}'):
            with self.subTest(record=bad):
                original = self.path.read_bytes()
                with self.path.open("ab") as stream:
                    stream.write(bad + b"\n")
                self.append(self.event("task_complete"))
                corrupt = self.drain()
                self.assertTrue(corrupt["history_checking"])
                self.assertEqual(corrupt["status"], "disconnected")
                self.path.write_bytes(original)
                self.assertEqual(self.drain()["status"], "ready")

    def test_consumer_exception_after_state_mutation_cannot_commit_ready_on_later_poll(self):
        original = self.path.read_bytes()
        with self.path.open("ab") as stream:
            stream.write(json.dumps(self.event("task_complete", turn="\ud800")).encode() + b"\n")
        for _ in range(5):
            observed = self.scan()
            self.assertEqual(observed["status"], "disconnected")
            self.assertTrue(observed["history_checking"])
            self.assertFalse(observed["history_verified"])
        self.path.write_bytes(original)
        self.append(self.event("task_started", "valid"), self.event("task_complete", "valid"))
        self.assertEqual(self.drain()["status"], "ready")

    def test_local_rate_limit_projection_retains_only_verified_safe_fields(self):
        raw = {"limit_id": "codex", "primary": {"used_percent": 12.5, "window_minutes": 10080,
                "resets_at": int(self.now + 86400), "email": "PRIVATE-ACCOUNT"},
               "secondary": {"used_percent": 90, "window_minutes": 300, "resets_at": int(self.now + 900)},
               "credits": {"balance": "PRIVATE-CREDITS"}, "plan_type": "PRIVATE-PLAN"}
        self.append(self.event("token_count", rate_limits=raw), self.event("task_complete"))
        observation = self.drain()
        self.assertEqual(observation["local_rate_limits"]["primary"]["window_minutes"], 10080)
        self.assertNotIn("PRIVATE", json.dumps(observation))
        with self.path.open("ab") as stream:
            stream.write(b'{"type":"response_item","payload":')
        checking = self.scan()
        self.assertTrue(checking["history_checking"])
        self.assertNotIn("local_rate_limits", checking)

    def test_invalid_present_quota_bucket_cannot_become_legacy_default(self):
        for identifier in ("x" * 129, 17, True, ["private-model"]):
            with self.subTest(identifier=type(identifier).__name__):
                self.path.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.identifier}}) + "\n")
                self.append(self.event("token_count", rate_limits={"limit_id": identifier,
                    "primary": {"used_percent": 10, "window_minutes": 10080, "resets_at": int(self.now + 600)}}))
                observed = self.drain(CodexSource(self.root, confine_paths=True))
                self.assertEqual(observed["local_rate_limits"]["limit_id"], "")

    def test_verified_large_rollout_projects_credits_and_newer_absence_clears_them(self):
        limits = {"limit_id": "codex", "credits": {"balance": "-1.125", "has_credits": False,
                                                   "unlimited": False, "account_id": "PRIVATE-ACCOUNT"}}
        # This takes the streaming projection path rather than normal json.loads.
        self.append(self.event("token_count", rate_limits=limits, unrelated="x" * (1024 * 1024)))
        observed = self.drain()
        self.assertTrue(observed["history_verified"])
        self.assertNotIn("PRIVATE-ACCOUNT", json.dumps(observed))
        reading = parse_codex_observation(observed["local_rate_limits"], observed["local_rate_limits_at"], now=self.now + 1)
        self.assertTrue(reading["available"])
        self.assertEqual(reading["windows"], [])
        self.assertEqual(reading["credits"], {"amount": -1.125, "unit": "credits", "observed_at": self.now})

        self.append(self.event("token_count", timestamp=self.now + 1, rate_limits={
            "limit_id": "codex", "primary": {"used_percent": 0, "window_minutes": 10080,
                                               "resets_at": int(self.now + 600)}}))
        observed = self.drain()
        reading = parse_codex_observation(observed["local_rate_limits"], observed["local_rate_limits_at"], now=self.now + 1)
        self.assertEqual(reading["windows"][0]["remaining_percent"], 100)
        self.assertNotIn("credits", reading)


if __name__ == "__main__":
    unittest.main()
