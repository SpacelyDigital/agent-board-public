"""Exercise the isolated sample board through its authenticated local interface.

These tests use no provider accounts or transcript fixtures. They establish the
sample HTTP/store contract, not native UI or actual-provider compatibility.
"""

from contextlib import ExitStack
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

from board_store import BoardStore
from sandbox_sources import activate_source_bookmarks
from server import BoardHTTPServer, Handler, Monitor


class SampleBoardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data = self.root / "data"
        self.roots = [self.root / name for name in ("codex", "claude", "desktop")]
        for directory in self.roots:
            directory.mkdir()
            (directory / "untouched.txt").write_text("This is not provider data.\n")
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        self.readers = [self.guards.enter_context(patch(
            "server." + name, side_effect=AssertionError(name + " constructed in sample mode")))
            for name in ("CodexSource", "ClaudeSource", "ClaudeLocalQuota", "UsageSource", "ClaudeProjectSource")]
        self.server = None

    def monitor(self, **overrides):
        options = {"sandbox": True, "store_profile": True, "sample_mode": True,
                   "source_grants": dict.fromkeys(("codex", "claude", "claude_desktop"), False)}
        options.update(overrides)
        return Monitor(self.data, self.roots[0], self.roots[1],
                       claude_desktop_root=self.roots[2], **options)

    def serve(self, monitor):
        monitor.scan()  # The native entry scans once before accepting HTTP requests.
        if self.server is not None:
            self.server.monitor = monitor
            return
        self.server = BoardHTTPServer(("127.0.0.1", 0), Handler)
        self.server.monitor = monitor
        self.server.access_token = "c" * 64
        thread = threading.Thread(target=self.server.serve_forever,
                                  kwargs={"poll_interval": .01}, daemon=True)
        thread.start()

        def close():
            self.server.shutdown()
            self.server.server_close()
            thread.join(2)
            self.assertFalse(thread.is_alive())

        self.addCleanup(close)

    def request(self, method, path, body=None, *, access=True, csrf=True, origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if access:
            headers["X-Agent-Board-Access"] = self.server.access_token
        if csrf:
            headers["X-Agent-Board-Token"] = self.server.monitor.token
        if origin is not None:
            headers["Origin"] = origin
        try:
            encoded = None if body is None else json.dumps(body)
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def state(self):
        status, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200, body)
        return body

    def advance(self, action):
        status, body = self.request("POST", "/api/samples/advance", {"action": action})
        self.assertEqual(status, 200, body)
        return self.state()

    def action(self, card, action, body=None):
        return self.request("POST", "/api/sessions/" + quote(card["key"], safe="") + "/" + action,
                            {"event_id": card["event_id"]} if body is None else body)

    def cards(self, state):
        cards = state["sessions"] + state["history"] + state["background"]
        self.assertEqual(len(cards), 2)
        self.assertEqual(len({card["key"] for card in cards}), 2)
        self.assertEqual({card["provider"] for card in cards}, {"claude", "codex"})
        return {card["provider"]: card for card in cards}

    def source_bytes(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for directory in self.roots for path in directory.rglob("*") if path.is_file()}

    def test_sample_mode_requires_store_sandbox_and_no_source_grants(self):
        invalid = [{"sandbox": False}, {"store_profile": False}, {"source_grants": None},
                   {"usage_enabled": True}, {"projects_enabled": True}]
        for provider in ("codex", "claude", "claude_desktop"):
            grants = dict.fromkeys(("codex", "claude", "claude_desktop"), False)
            grants[provider] = True
            invalid.append({"source_grants": grants})
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.monitor(**options)
        for missing in range(3):
            roots = list(self.roots)
            roots[missing] = None
            with self.subTest(missing_explicit_root=missing), self.assertRaises(ValueError):
                Monitor(self.data, roots[0], roots[1], claude_desktop_root=roots[2],
                        sandbox=True, store_profile=True, sample_mode=True,
                        source_grants=dict.fromkeys(("codex", "claude", "claude_desktop"), False))
        self.assertFalse(self.data.exists(), "Invalid sample mode must fail before creating a database")
        for reader in self.readers:
            reader.assert_not_called()

    def test_no_provider_readers_or_opens_and_explicitly_sample_usage(self):
        before = self.source_bytes()
        board = self.monitor()
        for _ in range(3):
            board.scan()
            board.scan_projects()
            board.run_usage()
        self.serve(board)
        state = self.state()
        self.assertTrue(state["sample_mode"])
        self.assertEqual(state["counts"]["cooking"], 2)
        self.assertEqual(board.store.path, self.data / "sample-board/board.sqlite3")
        self.assertFalse((self.data / "board.sqlite3").exists())
        cards = self.cards(state)
        for card in cards.values():
            self.assertTrue(card["sample"])
            self.assertFalse(card["can_open"])
            self.assertFalse(card.get("open_url"))
            self.assertFalse(card["source_unavailable"], "Samples must not look like stale provider observations")
            with patch("server.chat_url", side_effect=AssertionError("Sample chat URL requested")), \
                    patch("server.subprocess.run", side_effect=AssertionError("Sample provider app opened")):
                status, body = self.action(card, "open")
            self.assertEqual(status, 409, body)
            self.assertNotIn("native_open_url", body)
        self.assertEqual({item["id"] for item in state["usage"]}, {"claude", "codex"})
        for item in state["usage"]:
            self.assertEqual(item["source"], "sample")
            self.assertTrue(item["sample"])
            self.assertTrue(item["available"])
            self.assertTrue(item["windows"])
        self.assertEqual(len(state["sources"]), 1)
        self.assertTrue(all(item.get("sample") is True for item in state["sources"]))
        self.assertEqual(before, self.source_bytes())
        for reader in self.readers:
            reader.assert_not_called()

    def test_http_lifecycle_clears_old_attention_and_rejects_stale_review(self):
        self.serve(self.monitor())
        initial = self.cards(self.state())
        waiting = self.advance("needs_you")
        self.assertEqual(waiting["counts"]["needs_you"], 2)
        old = self.cards(waiting)
        self.assertTrue(all(not card["notification_eligible"] for card in old.values()))
        self.assertTrue(all(card["notification_eligible"] for card in self.server.monitor.store.snapshot()["sessions"]))
        resumed = self.advance("cooking")
        self.assertEqual(resumed["counts"]["needs_you"], 0)
        self.assertEqual(resumed["counts"]["cooking"], 2)
        for provider, card in self.cards(resumed).items():
            self.assertEqual(card["key"], initial[provider]["key"])
            self.assertNotEqual(card["event_id"], old[provider]["event_id"])
            self.assertFalse(card["notification_eligible"])
            status, body = self.action(old[provider], "review")
            self.assertEqual(status, 409, body)
        ready = self.advance("ready")
        self.assertEqual(ready["counts"]["ready"], 2)
        self.assertEqual(ready["counts"]["cooking"], 0)
        for provider, card in self.cards(ready).items():
            self.assertEqual(card["key"], initial[provider]["key"])
            self.assertFalse(card["notification_eligible"], "Synthetic samples must not send native alerts")
            self.assertTrue(card.get("preview"))
        for _ in range(3):
            self.server.monitor.scan()
        self.assertEqual({key: (card["status"], card["event_id"]) for key, card in self.cards(self.state()).items()},
                         {key: (card["status"], card["event_id"]) for key, card in self.cards(ready).items()})

    def test_review_rename_snooze_and_pending_reply_survive_restart(self):
        self.serve(self.monitor())
        ready = self.cards(self.advance("ready"))
        codex, claude = ready["codex"], ready["claude"]
        self.assertEqual(self.action(codex, "review")[0], 200)
        self.assertEqual(self.action(claude, "rename", {"title": "My sample follow-up"})[0], 200)
        self.assertEqual(self.action(claude, "snooze")[0], 200)
        before = self.state()
        for _ in range(3):
            self.server.monitor.scan()
        self.serve(self.monitor())
        self.server.monitor.scan()
        resumed = self.state()
        self.assertEqual(resumed["counts"]["ready"], 1)
        self.assertEqual(len(resumed["history"]), 1)
        restored = self.cards(resumed)
        self.assertEqual(restored["codex"]["reviewed_event"], codex["event_id"])
        self.assertFalse(restored["codex"]["notification_eligible"])
        self.assertEqual(restored["claude"]["title"], "My sample follow-up")
        self.assertGreater(restored["claude"]["snoozed_until"], time.time())
        self.assertFalse(restored["claude"]["notification_eligible"])
        self.assertEqual({key: card["event_id"] for key, card in self.cards(before).items()},
                         {key: card["event_id"] for key, card in restored.items()})
        self.assertEqual(self.action(restored["claude"], "snooze", {"event_id": claude["event_id"], "minutes": 0})[0], 200)
        raw = {card["provider"]: card for card in self.server.monitor.store.snapshot()["sessions"]}
        self.assertTrue(raw["claude"]["notification_eligible"])
        self.assertFalse(self.cards(self.state())["claude"]["notification_eligible"])

    def test_reset_is_confined_to_sample_state_and_live_mode_does_not_seed_samples(self):
        live = BoardStore(self.data / "board.sqlite3")
        now = time.time() + .01
        live.apply([{"provider": "codex", "id": "synthetic-live-board-placeholder", "status": "ready",
                     "title": "Preserve this separate board", "updated_at": now, "event_id": "live-placeholder-reply"}], now)
        card = live.snapshot()["sessions"][0]
        live.rename(card["key"], "Saved outside sample mode")
        live.review(card["key"], card["event_id"])
        before = live.snapshot()
        database_before = hashlib.sha256(live.path.read_bytes()).hexdigest()
        source_before = self.source_bytes()
        self.serve(self.monitor())
        ready = self.cards(self.advance("ready"))
        self.assertEqual(self.action(ready["codex"], "review")[0], 200)
        self.assertEqual(self.action(ready["claude"], "rename", {"title": "Reset only this sample"})[0], 200)
        reset = self.advance("reset")
        self.assertEqual(reset["counts"]["cooking"], 2)
        self.assertEqual(reset["history"], [])
        self.assertTrue(all(card.get("title_override") is None for card in self.cards(reset).values()))
        self.assertEqual(live.snapshot(), before)
        self.assertEqual(hashlib.sha256(live.path.read_bytes()).hexdigest(), database_before)
        self.assertEqual(source_before, self.source_bytes())

        self.serve(self.monitor(sample_mode=False))
        self.server.monitor.scan()
        state = self.state()
        self.assertFalse(state.get("sample_mode", False))
        self.assertEqual({item["id"] for item in state["history"]}, {"synthetic-live-board-placeholder"})
        self.assertEqual(state["sessions"], [])
        self.assertEqual(self.request("POST", "/api/samples/advance", {"action": "ready"})[0], 404)
        self.assertEqual(live.snapshot(), before)
        self.assertEqual(source_before, self.source_bytes())

    def test_sample_storage_cannot_follow_a_link_to_the_live_database(self):
        live = BoardStore(self.root / "separate-live/board.sqlite3")
        before = hashlib.sha256(live.path.read_bytes()).hexdigest()
        self.data.mkdir()
        (self.data / "sample-board").symlink_to(live.path.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.monitor()
        self.assertEqual(hashlib.sha256(live.path.read_bytes()).hexdigest(), before)
        self.assertEqual(live.snapshot()["sessions"], [])

        (self.data / "sample-board").unlink()
        self.data.rmdir()
        self.data.symlink_to(live.path.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.monitor()
        self.assertFalse((live.path.parent / "sample-board").exists())
        self.assertEqual(hashlib.sha256(live.path.read_bytes()).hexdigest(), before)

    def test_sample_actions_require_authentication_csrf_origin_and_exact_body(self):
        self.serve(self.monitor())
        initial = self.cards(self.state())
        for options in ({"access": False}, {"csrf": False}, {"origin": "https://example.invalid"}):
            with self.subTest(options=options):
                self.assertEqual(self.request("POST", "/api/samples/advance", {"action": "ready"}, **options)[0], 403)
        for body in ({}, {"action": "unknown"}, {"action": "ready", "provider": "codex"},
                     {"action": None}, ["ready"]):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/samples/advance", body)[0], 400)
        after = self.cards(self.state())
        self.assertEqual({key: (card["status"], card["event_id"]) for key, card in initial.items()},
                         {key: (card["status"], card["event_id"]) for key, card in after.items()})

    def test_reopen_recovers_reset_interrupted_before_new_cards_are_written(self):
        board = self.monitor()
        self.serve(board)
        ready = self.cards(self.advance("ready"))
        self.assertEqual(self.action(ready["codex"], "review")[0], 200)
        before = self.source_bytes()
        with patch.object(board.store, "apply", side_effect=sqlite3.OperationalError("Synthetic sample write interruption")):
            status, body = self.request("POST", "/api/samples/advance", {"action": "reset"})
        self.assertEqual(status, 503, body)
        self.assertEqual(board.store.snapshot()["sessions"] + board.store.snapshot()["history"], [])
        self.serve(self.monitor())
        recovered = self.state()
        self.assertEqual(recovered["counts"]["cooking"], 2)
        self.assertEqual(recovered["history"], [])
        self.cards(recovered)
        self.assertEqual(before, self.source_bytes())

    def test_sample_launch_rejects_every_bookmark_before_native_scope_activation(self):
        support = self.root / "sample-native-support"
        (support / "data").mkdir(parents=True)
        roots = {name: support / "sources-unavailable" / name
                 for name in ("codex", "claude", "claude_desktop")}
        for directory in roots.values():
            directory.mkdir(parents=True)
        manifest = support / "data/source-bookmarks.json"

        def write(bookmarked=None, value="c3ludGhldGlj"):
            sources = {provider: {"path": str(path)} for provider, path in roots.items()}
            if bookmarked:
                sources[bookmarked]["bookmark"] = value
            manifest.write_text(json.dumps({"version": 1, "sources": sources}))
            manifest.chmod(0o600)

        with patch("sandbox_sources._foundation", side_effect=AssertionError("Sample mode activated a native scope")) as native:
            write()
            self.assertEqual(activate_source_bookmarks(manifest, roots, support, require_unselected=True),
                             dict.fromkeys(roots, False))
            for provider in roots:
                for value in ("c3ludGhldGlj", "", None):
                    with self.subTest(provider=provider, value=value), self.assertRaises(ValueError):
                        write(provider, value)
                        activate_source_bookmarks(manifest, roots, support, require_unselected=True)
            native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
