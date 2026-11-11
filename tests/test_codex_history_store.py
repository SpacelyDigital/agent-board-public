"""A replaced Codex history must not preserve a newer cached Ready reply."""

from pathlib import Path
import tempfile
import unittest

from board_store import BoardStore


class CodexHistoryStoreTests(unittest.TestCase):
    def test_same_bytes_with_legacy_generation_change_cannot_realert_old_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BoardStore(Path(directory) / 'board.sqlite3')
            at = store.activated_at
            revision = {'file_id': 'a' * 64, 'size': 500, 'sha256': 'b' * 64, 'generation': 'c' * 64}
            prior = dict(provider='codex', id='identical-replacement', title='Fixture',
                         status='disconnected', event_id='reviewed-placeholder', updated_at=at + 100,
                         history_verified=True, history_revision=revision)
            store.apply([prior], now=at + 101)
            before = store.get('codex:identical-replacement')
            store.review(before['key'], before['event_id'])
            replacement = dict(revision, file_id='d' * 64, generation='e' * 64)
            store.apply([dict(prior, status='ready', event_id='older-completion', updated_at=at + 1,
                              history_revision=replacement)], now=at + 102)
            after = store.get(before['key'])
            self.assertEqual(after['status'], 'disconnected')
            self.assertEqual(after['event_id'], before['event_id'])
            self.assertEqual(after['reviewed_event'], before['event_id'])
            self.assertFalse(after['notification_eligible'])

    def test_verified_replacement_supersedes_reviewed_reply_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.sqlite3"
            store = BoardStore(path)
            at = store.activated_at
            first = {"file_id": "a" * 64, "size": 500, "sha256": "b" * 64,
                     "generation": "c" * 64}
            old = dict(provider="codex", id="replacement-fixture", title="Local fixture",
                       status="ready", event_id="first-complete", updated_at=at + 100,
                       history_verified=True, history_revision=first)
            store.apply([old], now=at + 101)
            key = "codex:replacement-fixture"
            reply = store.get(key)
            store.review(key, reply["event_id"])
            restarted = BoardStore(path)
            self.assertEqual(restarted.transcript_revision("replacement-fixture", "codex"), first)
            self.assertIsNone(restarted.transcript_revision("replacement-fixture"))
            restarted.apply([dict(old, history_checking=True, history_verified=False,
                                  detail="Checking replaced local history.")], now=at + 102)
            self.assertEqual(restarted.get(key)["event_id"], reply["event_id"])
            self.assertTrue(restarted.get(key)["history_checking"])
            replacement = dict(first, sha256="d" * 64, generation="e" * 64)
            restarted.apply([dict(old, status="cooking", event_id="replacement-start",
                                  updated_at=at + 1, history_revision=replacement)], now=at + 103)
            current = restarted.get(key)
            self.assertEqual(current["status"], "cooking")
            self.assertNotEqual(current["event_id"], reply["event_id"])
            self.assertFalse(current["history_checking"])
            self.assertEqual(restarted.snapshot()["counts"]["cooking"], 1)
            self.assertEqual(restarted.transcript_revision("replacement-fixture", "codex"), replacement)


if __name__ == "__main__":
    unittest.main()
