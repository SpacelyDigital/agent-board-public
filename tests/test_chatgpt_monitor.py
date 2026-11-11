from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from board_store import BoardStore
from chatgpt_monitor import ChatGPTConversationSource
from chatgpt_source import ChatGPTSource
from chatgpt_status import ChatGPTStatusReader


CHAT_A = "11111111-1111-4111-8111-111111111111"
CHAT_B = "22222222-2222-4222-8222-222222222222"


class StatusReader:
    def __init__(self):
        self.readings = {}
        self.requested = []
        self.discovery = {}
        self.discovery_ok = False
        self.calls = []
        self.account = "fixture-account-a"

    def account_identity(self):
        return self.account

    def scan(self, identifiers, now):
        self.calls.append("scan")
        self.requested = list(identifiers)
        return self.readings

    def discovered_chats(self, now):
        self.calls.append("discovered_chats")
        return self.discovery

    def discovery_available(self, now):
        return self.discovery_ok

    def discover(self, ident, title, updated):
        self.discovery[ident] = dict(title=title, source_created_at=updated - 100,
                                    source_updated_at=updated)
        self.discovery_ok = True

    def set(self, state, now, ident=CHAT_A, fresh=True):
        self.readings[ident] = dict(status=state, observed_at=now, fresh=fresh)


class ChatGPTMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = BoardStore(self.root / "board.sqlite3")
        self.now = self.store.activated_at + 1
        self.path = self.root / "catalog.db"
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE local_thread_catalog (
                host_id TEXT,thread_id TEXT,display_title TEXT,source_created_at REAL,
                source_updated_at REAL,source_recency_at REAL,source_kind TEXT,
                missing_candidate INTEGER,chatgpt_async_status INTEGER,
                observation_sequence INTEGER,PRIMARY KEY(host_id,thread_id))""")
        self.add()
        self.reader = StatusReader()
        self.source = ChatGPTConversationSource(ChatGPTSource(self.path), self.reader)

    def tearDown(self):
        self.temp.cleanup()

    def add(self, ident=CHAT_A, title="Same title"):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO local_thread_catalog VALUES (?,?,?,?,?,?,?,?,?,?)",
                       ("local",ident,title,self.now-100,self.now-1,self.now-1,"chatgpt",0,None,1))

    def item(self, ident=CHAT_A):
        return self.store.get("chatgpt:desktop_chat:" + ident)

    def scan(self, offset=0):
        return self.source.scan(self.store, self.now + offset)

    def test_initial_complete_is_saved_metadata_without_attention(self):
        self.reader.set("COMPLETE", self.now)
        self.scan()
        self.assertIsNone(self.item())
        self.assertFalse(self.store.snapshot()["sessions"])
        self.assertTrue(self.source.open_chats(self.now)["desktop_chat:" + CHAT_A]["catalog_only"])
        self.assertFalse(self.source.cooking)

    def test_stream_complete_review_poll_and_restart_do_not_repeat_alert(self):
        self.reader.set("IS_STREAMING", self.now)
        self.scan()
        active = self.item()
        self.assertEqual(active["status"], "cooking")
        self.reader.set("COMPLETE", self.now + 2)
        self.scan(2)
        ready = self.item()
        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["notification_eligible"])
        self.store.review(ready["key"], ready["event_id"])
        self.scan(3)
        self.reader.set("COMPLETE", self.now + 4)
        self.scan(4)
        self.source = ChatGPTConversationSource(ChatGPTSource(self.path), self.reader)
        self.store = BoardStore(self.root / "board.sqlite3")
        self.scan(5)
        reviewed = self.item()
        self.assertEqual(reviewed["event_id"], ready["event_id"])
        self.assertEqual(reviewed["reviewed_event"], ready["event_id"])
        self.assertFalse(reviewed["notification_eligible"])
        self.assertFalse(self.store.snapshot()["sessions"])
        self.reader.set("IS_STREAMING", self.now + 6)
        self.scan(6)
        self.reader.set("COMPLETE", self.now + 8)
        self.scan(8)
        self.assertNotEqual(self.item()["event_id"], ready["event_id"])
        self.assertTrue(self.item()["notification_eligible"])

    def test_title_changes_keep_turn_and_same_titles_keep_distinct_ids(self):
        self.add(CHAT_B)
        self.reader.set("IS_STREAMING", self.now)
        self.reader.set("IS_STREAMING", self.now, CHAT_B)
        self.scan()
        before = self.item()
        self.add(title="Renamed by owner")
        self.scan(1)
        self.assertEqual(self.item()["title"], "Renamed by owner")
        self.assertEqual(self.item()["event_id"], before["event_id"])
        self.assertEqual(len(self.store.snapshot()["sessions"]), 2)
        self.assertEqual(set(self.reader.requested), {CHAT_A, CHAT_B})

    def test_unknown_stale_and_missing_readings_do_not_complete_pending_work(self):
        self.reader.set("IS_STREAMING", self.now)
        self.scan()
        event = self.item()["event_id"]
        for offset, state, fresh in [(1, "UNAVAILABLE", True), (2, "COMPLETE", False),
                                     (3, "UNRECOGNIZED", True)]:
            self.reader.set(state, self.now + offset, fresh=fresh)
            self.scan(offset)
            self.assertEqual(self.item()["status"], "cooking")
            self.assertEqual(self.item()["event_id"], event)
            self.assertFalse(self.source.cooking)
            self.assertFalse(self.item()["notification_eligible"])
        self.reader.readings = {}
        self.scan(20)
        self.assertEqual(self.item()["status"], "cooking")
        self.reader.set("COMPLETE", self.now + 21)
        self.scan(21)
        self.assertEqual(self.item()["status"], "ready")

    def test_restart_recovers_exact_persisted_stream_without_new_event(self):
        self.reader.set("IS_STREAMING", self.now)
        self.scan()
        turn = self.item()["turn_id"]
        self.source = ChatGPTConversationSource(ChatGPTSource(self.path), self.reader)
        self.store = BoardStore(self.root / "board.sqlite3")
        self.reader.set("COMPLETE", self.now + 30)
        self.scan(30)
        self.assertEqual(self.item()["status"], "ready")
        self.assertEqual(self.item()["turn_id"], turn)

    def test_null_or_unrelated_turn_is_not_adopted_as_a_provider_stream(self):
        for turn in (None, "desktop-chat:other-turn", "chatgpt-stream:" + CHAT_B + ":1"):
            with self.subTest(turn=turn):
                self.store.apply([dict(provider="chatgpt",source_kind="desktop_chat",
                    id="desktop_chat:"+CHAT_A,title="Saved",status="cooking",
                    event_id=str(turn),turn_id=turn,started_at=self.now,updated_at=self.now)])
                self.source = ChatGPTConversationSource(ChatGPTSource(self.path), self.reader)
                self.reader.set("COMPLETE", self.now + 1)
                self.scan(1)
                self.assertEqual(self.item()["status"], "cooking")

    def test_old_complete_or_stream_cannot_reverse_latest_reading(self):
        self.reader.set("IS_STREAMING", self.now)
        self.scan()
        self.reader.set("COMPLETE", self.now - 1)
        self.scan(1)
        self.assertEqual(self.item()["status"], "cooking")
        self.reader.set("COMPLETE", self.now + 2)
        self.scan(2)
        ready = self.item()
        self.reader.set("IS_STREAMING", self.now)
        self.scan(3)
        self.assertEqual(self.item()["event_id"], ready["event_id"])
        self.assertFalse(self.source.cooking)
        self.assertFalse(self.source.pending)

    def test_failure_requires_an_observed_stream_and_is_one_attention_event(self):
        self.reader.set("FAILURE", self.now)
        self.scan()
        self.assertIsNone(self.item())
        self.reader.set("IS_STREAMING", self.now + 1)
        self.scan(1)
        self.reader.set("FAILURE", self.now + 2)
        self.scan(2)
        failure = self.item()
        self.assertEqual(failure["status"], "needs_you")
        self.store.review(failure["key"], failure["event_id"])
        self.reader.set("FAILURE", self.now + 3)
        self.scan(3)
        self.assertEqual(self.item()["event_id"], failure["event_id"])
        self.assertFalse(self.item()["notification_eligible"])

    def test_browser_chat_is_discovered_without_local_catalog_and_then_monitored(self):
        self.path.unlink()
        self.reader.discover(CHAT_B, "New browser chat", self.now)
        health = self.scan()
        self.assertTrue(health["available"])
        self.assertEqual(self.reader.requested, [])
        self.assertEqual(self.reader.calls[:2], ["scan", "discovered_chats"])
        chats = self.source.open_chats(self.now)
        self.assertEqual(set(chats), {"desktop_chat:" + CHAT_B})
        self.assertTrue(chats["desktop_chat:" + CHAT_B]["can_open"])
        self.assertIsNone(self.item(CHAT_B))
        self.reader.set("IS_STREAMING", self.now + 1, CHAT_B)
        self.scan(1)
        self.assertEqual(self.reader.requested, [CHAT_B])
        self.assertEqual(self.item(CHAT_B)["status"], "cooking")
        self.reader.set("COMPLETE", self.now + 3, CHAT_B)
        self.scan(3)
        self.assertEqual(self.item(CHAT_B)["status"], "ready")
        self.assertEqual(len(self.store.snapshot()["sessions"]), 1)

    def test_remote_and_local_same_id_are_one_card_and_newer_metadata_keeps_review(self):
        self.reader.discover(CHAT_A, "Server title", self.now)
        self.reader.discover(CHAT_B, "Server title", self.now)
        self.reader.set("IS_STREAMING", self.now)
        self.scan()
        self.reader.set("COMPLETE", self.now + 1)
        self.scan(1)
        ready = self.item()
        self.store.review(ready["key"], ready["event_id"])
        self.reader.discover(CHAT_A, "Renamed remotely", self.now + 2)
        self.reader.set("COMPLETE", self.now + 2)
        self.scan(2)
        self.assertEqual(self.item()["title"], "Renamed remotely")
        self.assertEqual(self.item()["event_id"], ready["event_id"])
        self.assertEqual(self.item()["reviewed_event"], ready["event_id"])
        self.assertFalse(self.item()["notification_eligible"])
        self.assertEqual(len(self.source.open_chats(self.now + 2)), 2)

    def test_successful_empty_remote_discovery_is_available_without_local_database(self):
        self.path.unlink()
        self.reader.discovery_ok = True
        self.assertTrue(self.scan()["available"])
        self.assertEqual(self.source.open_chats(self.now), {})
        self.reader.discovery_ok = False
        self.assertFalse(self.scan(1)["available"])

    def test_metadata_change_without_stream_is_not_completion(self):
        self.reader.discover(CHAT_B, "First title", self.now)
        self.reader.set("COMPLETE", self.now, CHAT_B)
        self.scan()
        self.reader.discover(CHAT_B, "Edited title", self.now + 1)
        self.reader.set("COMPLETE", self.now + 1, CHAT_B)
        self.scan(1)
        self.assertIsNone(self.item(CHAT_B))
        self.assertFalse(self.store.snapshot()["sessions"])

    def test_discovery_outage_preserves_pending_id_without_manufacturing_completion(self):
        self.path.unlink()
        self.reader.discover(CHAT_B, "Browser reply", self.now)
        self.reader.set("IS_STREAMING", self.now, CHAT_B)
        self.scan()
        self.reader.discovery = {}
        self.reader.discovery_ok = False
        self.reader.readings = {}
        self.scan(1)
        self.assertEqual(self.item(CHAT_B)["status"], "cooking")
        self.assertEqual(self.reader.requested, [CHAT_B])
        self.reader.set("COMPLETE", self.now + 2, CHAT_B)
        self.scan(2)
        self.assertEqual(self.item(CHAT_B)["status"], "ready")

    def test_invalid_discovered_identity_or_timestamp_never_becomes_a_link(self):
        self.reader.discover("window:" + "a" * 32, "Invalid identity", self.now)
        self.reader.discover(CHAT_B, "Future entry", self.now + 60)
        self.scan()
        self.assertEqual(set(self.source.open_chats(self.now)), {"desktop_chat:" + CHAT_A})

    def test_async_reader_discovers_browser_chat_and_keeps_turn_across_same_account_token_refresh(self):
        self.path.unlink()
        clock = [self.now]
        response = ["IS_STREAMING"]
        metadata = {CHAT_B: dict(title="Browser workflow", source_created_at=self.now - 10,
                                source_updated_at=self.now)}
        reader = ChatGPTStatusReader(self.root, request=lambda *_: response[0],
                                    metadata_request=lambda *_: metadata, clock=lambda: clock[0])
        self.source = ChatGPTConversationSource(ChatGPTSource(self.path), reader)
        credentials = ['fixture-token', 'fixture-account']
        try:
            with patch("chatgpt_status._read_credentials", side_effect=lambda _: tuple(credentials)):
                for _ in range(100):
                    self.source.scan(self.store, clock[0])
                    if self.item(CHAT_B) is not None:
                        break
                    time.sleep(0.005)
                active = self.item(CHAT_B)
                self.assertIsNotNone(active)
                self.assertEqual(active["status"], "cooking")
                response[0] = "COMPLETE"
                credentials[0] = 'refreshed-token'
                clock[0] += 5
                for _ in range(100):
                    self.source.scan(self.store, clock[0])
                    if self.item(CHAT_B)["status"] == "ready":
                        break
                    time.sleep(0.005)
                ready = self.item(CHAT_B)
                self.assertEqual(ready["status"], "ready")
                self.assertEqual(ready["turn_id"], active["turn_id"])
                self.assertEqual(len(self.store.snapshot()["sessions"]), 1)
        finally:
            reader.close()

    def test_account_change_fences_pending_and_persisted_stream_recovery(self):
        self.reader.set('IS_STREAMING', self.now)
        self.scan()
        old_turn = self.item()['turn_id']
        self.reader.account = 'fixture-account-b'
        self.reader.set('COMPLETE', self.now + 2)
        self.scan(2)
        self.assertEqual(self.item()['status'], 'cooking')
        self.assertEqual(self.item()['turn_id'], old_turn)
        self.assertFalse(self.source.cooking)
        self.assertFalse(self.source.pending)
        self.source = ChatGPTConversationSource(ChatGPTSource(self.path), self.reader)
        self.reader.set('COMPLETE', self.now + 3)
        self.scan(3)
        self.assertEqual(self.item()['status'], 'cooking')
        self.assertFalse(self.source.pending)
        self.reader.set('IS_STREAMING', self.now + 4)
        self.scan(4)
        self.assertNotEqual(self.item()['turn_id'], old_turn)
        self.reader.set('COMPLETE', self.now + 5)
        self.scan(5)
        self.assertEqual(self.item()['status'], 'ready')


if __name__ == "__main__":
    unittest.main()
