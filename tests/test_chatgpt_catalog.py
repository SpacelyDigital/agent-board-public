import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from board_store import BoardStore
from chatgpt_source import ChatGPTSource


NOW = 1794382000.0
CHAT_A = "11111111-1111-4111-8111-111111111111"
CHAT_B = "22222222-2222-4222-8222-222222222222"


class ChatGPTCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "catalog.db"
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE local_thread_catalog (
                host_id TEXT, thread_id TEXT, display_title TEXT, source_created_at REAL,
                source_updated_at REAL, source_recency_at REAL, source_kind TEXT,
                missing_candidate INTEGER, chatgpt_async_status INTEGER,
                observation_sequence INTEGER, conversation_origin TEXT,
                PRIMARY KEY(host_id,thread_id))""")
        self.source = ChatGPTSource(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def add(self, ident=CHAT_A, title="Saved conversation", status=None, updated=NOW - 30,
            provider="chatgpt", host="local", missing=0, origin=None, sequence=1):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO local_thread_catalog VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (host, ident, title, NOW - 1000, updated, updated, provider,
                        missing, status, sequence, origin))

    def test_exact_provider_and_ids_with_same_titles_are_distinct(self):
        self.add(title="Same title", origin="tpp")
        self.add(CHAT_B, title="Same title")
        self.add("33333333-3333-4333-8333-333333333333", title="Same title", provider="codex")
        self.add("window:" + "a" * 32)
        observations, health = self.source.scan(NOW, NOW - 3600)
        self.assertTrue(health["available"])
        self.assertEqual(observations, [])
        chats = self.source.open_chats(NOW)
        self.assertEqual(set(chats), {"desktop_chat:" + CHAT_A, "desktop_chat:" + CHAT_B})
        for ident, item in chats.items():
            self.assertTrue(item["catalog_only"])
            self.assertTrue(item["can_open"])
            self.assertFalse(item["notification_eligible"])
            self.assertEqual(item["open_url"], "https://chatgpt.com/c/" + ident.split(":", 1)[1])

    def test_cached_statuses_never_manufacture_activity_or_completions(self):
        for state in (None, 1, 2, 3, 4, 5, 6, 7, 999):
            with self.subTest(state=state):
                self.add(status=state)
                observations, _ = self.source.scan(NOW, NOW - 3600)
                self.assertEqual(observations, [])
                self.assertEqual(self.source.open_chats(NOW)["desktop_chat:" + CHAT_A]["status"], "disconnected")
        self.assertEqual(ChatGPTSource(self.path).scan(NOW, NOW - 3600)[0], [])

    def test_existing_title_link_refresh_preserves_reviewed_lifecycle(self):
        store = BoardStore(self.root / "board.sqlite3")
        ident = "desktop_chat:" + CHAT_A
        at = store.activated_at + 1
        store.apply([dict(provider="chatgpt", source_kind="desktop_chat", id=ident,
                          title="Old title", status="ready", updated_at=at, event_id="real-reply")], now=at)
        item = store.get("chatgpt:" + ident)
        store.review(item["key"], item["event_id"])
        self.add(title="Updated title", updated=NOW - 30)
        self.source.tracked_ids = {ident}
        observations, _ = self.source.scan(NOW, NOW - 3600)
        store.apply(observations, now=at + 1)
        after = store.get(item["key"])
        self.assertEqual(after["title"], "Updated title")
        self.assertTrue(after["can_open"])
        self.assertEqual(after["status"], "ready")
        self.assertEqual(after["event_id"], item["event_id"])
        self.assertEqual(after["reviewed_event"], item["event_id"])
        self.assertFalse(after["notification_eligible"])

    def test_same_conversation_across_hosts_is_one_identity(self):
        self.add(status=3, sequence=1)
        self.add(status=4, host="remote", sequence=2)
        self.source.scan(NOW, NOW - 3600)
        self.assertEqual(len(self.source.latest_metadata), 1)
        self.assertIsNone(self.source.latest_metadata["desktop_chat:" + CHAT_A]["async_status"])
        self.add(host="remote", title="New title", updated=NOW - 5, status=None, sequence=3)
        self.source.scan(NOW + 1, NOW - 3600)
        self.assertEqual(self.source.latest_metadata["desktop_chat:" + CHAT_A]["title"], "New title")

    def test_missing_and_invalid_rows_are_not_navigation_targets(self):
        self.add(missing=1)
        self.add(CHAT_B, updated=NOW + 60)
        self.add("not-a-chat-id")
        self.source.scan(NOW, NOW - 3600)
        self.assertEqual(self.source.latest_metadata, {})

    def test_read_is_readonly_and_does_not_create_missing_catalog(self):
        self.add()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.source.scan(NOW, NOW - 3600)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        missing = self.root / "missing.db"
        self.assertFalse(ChatGPTSource(missing).scan(NOW, NOW - 3600)[1]["available"])
        self.assertFalse(missing.exists())

    def test_failed_or_expired_metadata_is_not_openable_as_current_catalog(self):
        self.add()
        self.source.scan(NOW, NOW - 3600)
        self.assertTrue(self.source.open_chats(NOW + 14))
        self.assertFalse(self.source.open_chats(NOW + 16))
        self.path.unlink()
        self.assertFalse(self.source.scan(NOW + 1, NOW - 3600)[1]["available"])
        self.assertFalse(self.source.latest_metadata)

    def test_bounded_reads_and_recent_candidates(self):
        self.add(updated=NOW - 20)
        self.add(CHAT_B, updated=NOW - 5)
        self.source.scan(NOW, NOW - 3600)
        self.assertEqual(self.source.recent_chats(1)[0]["id"], "desktop_chat:" + CHAT_B)
        with self.assertRaises(ValueError):
            self.source.recent_chats(5000)
        with patch("chatgpt_source.MAX_ROWS", 1):
            self.assertFalse(self.source.scan(NOW, NOW - 3600)[1]["available"])


if __name__ == "__main__":
    unittest.main()
