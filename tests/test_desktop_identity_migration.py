import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from board_store import BoardStore


class DesktopIdentityMigrationTests(unittest.TestCase):
    def test_legacy_windows_are_preserved_without_merging_real_same_named_chats(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "board.sqlite3"
            store = BoardStore(path)
            at = store.activated_at + 1
            windows = ["desktop_chat:window:" + digit * 32 for digit in "abc"]
            rows = [dict(provider="chatgpt", source_kind="desktop_chat", id=ident,
                         title="Same title", status="ready", updated_at=at,
                         event_id="reply", can_open=False) for ident in windows]
            real_id = "desktop_chat:11111111-1111-4111-8111-111111111111"
            rows += [dict(rows[0], id=real_id, can_open=True),
                     dict(provider="codex", id="real-code", title="Same title",
                          status="ready", updated_at=at, event_id="reply", can_open=True)]
            store.apply(rows, now=at)
            reviewed_key = "chatgpt:" + windows[0]
            snoozed_key = "chatgpt:" + windows[1]
            store.review(reviewed_key, store.get(reviewed_key)["event_id"])
            store.snooze(snoozed_key, store.get(snoozed_key)["event_id"], now=at)
            store.rename(snoozed_key, "Owner's title")
            store.move("chatgpt:" + windows[2], "board")
            with store.connect() as db:
                db.execute("DELETE FROM metadata WHERE key='chatgpt_provider_identity_v2'")
                before = {row["key"]: dict(row) for row in db.execute("SELECT * FROM sessions")}
                preferences = [tuple(row) for row in db.execute("SELECT * FROM session_preferences ORDER BY key")]
            repaired = BoardStore(path)
            snapshot = repaired.snapshot()
            self.assertEqual({row["key"] for row in snapshot["sessions"]},
                             {"chatgpt:" + real_id, "codex:real-code"})
            self.assertEqual(snapshot["background"], [])
            self.assertEqual(snapshot["history"], [])
            self.assertEqual(sum(snapshot["counts"].values()), 2)
            self.assertTrue(all(repaired.get("chatgpt:" + ident)["status"] == "disconnected"
                                and not repaired.get("chatgpt:" + ident)["notification_eligible"]
                                for ident in windows))
            self.assertEqual(repaired.get(snoozed_key)["title"], "Owner's title")
            with repaired.connect() as db:
                after = {row["key"]: dict(row) for row in db.execute("SELECT * FROM sessions")}
                self.assertEqual(preferences, [tuple(row) for row in db.execute("SELECT * FROM session_preferences ORDER BY key")])
            self.assertEqual(before.keys(), after.keys())
            for key in before:
                self.assertEqual({k: v for k, v in before[key].items() if k != "extra"},
                                 {k: v for k, v in after[key].items() if k != "extra"})
            backups = list(Path(folder).glob("recovery/*/board-before-chat-identity-repair.sqlite3"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(backups[0]) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 5)
                self.assertFalse(any(json.loads(row[0]).get("legacy_identity_unverified")
                                     for row in db.execute("SELECT extra FROM sessions")))
            BoardStore(path)
            self.assertEqual(len(list(Path(folder).glob("recovery/*/board-before-chat-identity-repair.sqlite3"))), 1)
            # Placement and review actions do not turn quarantined observations
            # back into duplicate cards. Their raw rows remain recoverable.
            for ident in windows:
                repaired.move("chatgpt:" + ident, "board")
                repaired.restore("chatgpt:" + ident)
            for group in ("sessions", "history", "background"):
                self.assertTrue(all(not item.get("legacy_identity_unverified")
                                    for item in repaired.snapshot()[group]))

    def test_new_verified_reader_observations_are_not_retired_on_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "board.sqlite3"
            store = BoardStore(path)
            store.apply([dict(provider="chatgpt", source_kind="desktop_chat",
                              id="desktop_chat:window:" + "d" * 32, title="Current chat",
                              status="ready", event_id="new", updated_at=store.activated_at + 1)])
            self.assertEqual(len(BoardStore(path).snapshot()["sessions"]), 1)


if __name__ == "__main__":
    unittest.main()
