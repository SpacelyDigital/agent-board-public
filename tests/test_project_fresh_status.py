from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from board_store import BoardStore
from server import Monitor


class FreshProjectStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = BoardStore(Path(self.temp.name) / "board.sqlite3")
        self.at = self.store.activated_at + 1

    def row(self, status, event, updated, **fields):
        return dict(provider="claude", source_kind="project", id="project:fixture:thread:fixture",
                    title="Project task", status=status, event_id=event, updated_at=updated,
                    confirmed_live=True, **fields)

    def test_fresh_resume_with_older_message_time_replaces_attention_once(self):
        self.store.apply([self.row("needs_you", "old-prompt", self.at + 10)], self.at + 11)
        before = self.store.snapshot()["sessions"][0]
        self.store.review(before["key"], before["event_id"])
        self.store.rename(before["key"], "Owner's title")
        self.store.apply([self.row("cooking", "resumed-work", self.at + 5,
                                  project_status_observed_at=self.at + 12)], self.at + 13)
        current = self.store.get(before["key"])
        self.assertEqual(current["status"], "cooking")
        self.assertEqual(current["updated_at"], self.at + 10)
        self.assertEqual(current["title"], "Owner's title")
        self.assertEqual(current["reviewed_event"], before["event_id"])
        self.assertEqual(self.store.snapshot()["counts"]["needs_you"], 0)
        self.assertEqual(len(self.store.snapshot()["sessions"]), 1)
        self.store.apply([self.row("needs_you", "new-permission", self.at + 6,
                                  project_status_observed_at=self.at + 14)], self.at + 15)
        pending = self.store.get(before["key"])
        self.assertEqual(pending["status"], "needs_you")
        self.assertTrue(pending["notification_eligible"])
        self.store.apply([self.row("ready", "new-reply", self.at + 16,
                                  project_status_observed_at=self.at + 17)], self.at + 18)
        self.assertEqual(self.store.get(before["key"])["status"], "ready")

    def test_older_ready_stale_receipt_cached_receipt_and_nonlive_reads_cannot_override(self):
        cases = [dict(status="ready", project_status_observed_at=self.at + 12),
                 dict(status="cooking"),
                 dict(status="cooking", project_status_observed_at=self.at - 30),
                 dict(status="cooking", project_status_observed_at=self.at + 10),
                 dict(status="cooking", project_status_observed_at=self.at + 15),
                 dict(status="cooking", project_status_observed_at=float("nan")),
                 dict(status="cooking", project_status_observed_at=self.at + 12, confirmed_live=False),
                 dict(status="cooking", project_status_observed_at=self.at + 12, metadata_only=True),
                 dict(status="cooking", project_status_observed_at=self.at + 12, source_kind="local")]
        for index, fields in enumerate(cases):
            with self.subTest(fields=fields):
                ident = "project:fixture:" + str(index)
                initial = self.row("needs_you", "old-prompt", self.at + 10)
                initial["id"] = ident
                self.store.apply([initial], self.at + 11)
                candidate = dict(initial, status="cooking", event_id="candidate", updated_at=self.at + 5)
                candidate.update(fields)
                self.store.apply([candidate], self.at + 13)
                self.assertEqual(self.store.get("claude:" + ident)["status"], "needs_you")

    def test_local_history_retains_older_observation_protection_even_with_forged_receipt(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                row = dict(provider=provider, id="local-fixture", title="Local task",
                           status="needs_you", event_id="current", updated_at=self.at + 10)
                self.store.apply([row], self.at + 11)
                self.store.apply([dict(row, status="cooking", event_id="older", updated_at=self.at + 5,
                                      confirmed_live=True, project_status_observed_at=self.at + 12)], self.at + 13)
                self.assertEqual(self.store.get(provider + ":local-fixture")["status"], "needs_you")

    def test_poll_wait_subtracts_fetch_duration_without_overlapping_requests(self):
        class Stop:
            done = False
            delay = None
            def is_set(self): return self.done
            def wait(self, delay): self.delay, self.done = delay, True
        for duration, expected in ((6.36, 8.64), (20, 0)):
            with self.subTest(duration=duration):
                monitor = object.__new__(Monitor)
                monitor.project_source = object()
                monitor.stop = Stop()
                calls = []
                monitor.scan_projects = lambda: calls.append("scan")
                with patch("server.time.monotonic", side_effect=(100, 100 + duration)):
                    monitor.run_projects()
                self.assertEqual(calls, ["scan"])
                self.assertAlmostEqual(monitor.stop.delay, expected)


if __name__ == "__main__":
    unittest.main()
