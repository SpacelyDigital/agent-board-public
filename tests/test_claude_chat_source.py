import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error

from claude_chat_source import (ClaudeChatAccountReader, FRESH_SECONDS, MAX_CHATS,
    MAX_METADATA_BYTES, _credentials, _fetch_snapshot, _read_json,
    credential_revision, helper_main, parse_conversations, read_account_chats)
from privacy_network import ProviderRequestError, RejectRedirects

CHAT = "11111111-1111-4111-8111-111111111111"
LEAF = "22222222-2222-4222-8222-222222222222"
ACCOUNT = "33333333-3333-4333-8333-333333333333"
ORG = "44444444-4444-4444-8444-444444444444"
SCOPE = "a" * 64
NOW = 1800000000.0


def row(**changes):
    return {"uuid": CHAT, "name": "  Fixture   conversation ", "platform": "CLAUDE_AI",
            "is_archived": False, "is_temporary": False, "created_at": NOW - 100,
            "updated_at": NOW - 5, "last_settled_at": NOW - 5,
            "current_leaf_message_uuid": LEAF, "live_status": None,
            "live_status_started_at": None, "needs_input": None,
            "needs_input_kind": None, "workspace_session_id": ORG, **changes}


def snapshot(**changes):
    return {"account_scope": SCOPE, "conversations": parse_conversations([row(**changes)], NOW)}


class MetadataParserTests(unittest.TestCase):
    def test_keeps_only_metadata_and_retains_regular_workspace_chat(self):
        data = row(summary="PRIVATE SUMMARY", settings={"tools": [{"description": "PRIVATE TOOL"}]},
                   latest_assistant_output_at=NOW - 1, latest_assistant_output_kind="artifact")
        result = parse_conversations([data], NOW)[CHAT]
        self.assertEqual(result["title"], "Fixture conversation")
        self.assertEqual(result["workspace_session_id"], ORG)
        self.assertEqual(result["last_settled_at"], NOW - 5)
        encoded = json.dumps(result)
        for excluded in ("PRIVATE", "summary", "settings", "latest_assistant_output"):
            self.assertNotIn(excluded, encoded)

    def test_observed_running_settled_and_rename_markers(self):
        before = parse_conversations([row()], NOW)[CHAT]
        active = parse_conversations([row(live_status="running", live_status_started_at=NOW - 2,
                                          updated_at=NOW - 2)], NOW)[CHAT]
        completed = parse_conversations([row(last_settled_at=NOW - 1, updated_at=NOW - 1,
                                              current_leaf_message_uuid=ACCOUNT)], NOW)[CHAT]
        renamed = parse_conversations([row(name="Renamed", updated_at=NOW,
            last_settled_at=NOW - 1, current_leaf_message_uuid=ACCOUNT)], NOW)[CHAT]
        self.assertEqual(active["live_status"], "running")
        self.assertEqual(active["last_settled_at"], before["last_settled_at"])
        self.assertEqual(active["current_leaf_message_uuid"], before["current_leaf_message_uuid"])
        self.assertIsNone(completed["live_status"])
        self.assertNotEqual(completed["last_settled_at"], before["last_settled_at"])
        self.assertNotEqual(completed["current_leaf_message_uuid"], before["current_leaf_message_uuid"])
        self.assertEqual(renamed["last_settled_at"], completed["last_settled_at"])
        self.assertEqual(renamed["current_leaf_message_uuid"], completed["current_leaf_message_uuid"])

    def test_unknown_status_and_explicit_input_are_preserved_not_guessed(self):
        result = parse_conversations([row(live_status="future_status", needs_input=True,
                                         needs_input_kind="permission")], NOW)[CHAT]
        self.assertEqual(result["live_status"], "future_status")
        self.assertIs(result["needs_input"], True)
        self.assertEqual(result["needs_input_kind"], "permission")
        self.assertEqual(parse_conversations([row(workspace_session_id="cse_example123")], NOW)[CHAT]["workspace_session_id"], "cse_example123")

    def test_archived_temporary_nonchat_and_missing_status_are_excluded(self):
        for changes in ({"is_archived": True}, {"is_temporary": True}, {"platform": "CODE"},
                        {"is_archived": None}, {"is_temporary": None}, {"updated_at": NOW + 99}):
            self.assertEqual(parse_conversations([row(**changes)], NOW), {})
        data = row()
        del data["live_status"]
        self.assertEqual(parse_conversations([data], NOW), {})

    def test_rejects_transcripts_duplicate_ids_bad_status_and_unbounded_lists(self):
        for bad in ({}, [None], [row()] * (MAX_CHATS + 1), [row(), row()],
                    [row(uuid="../secret")], [row(messages=[{"text": "PRIVATE"}])],
                    [row(needs_input="false")], [row(live_status={"private": "body"})]):
            with self.subTest(shape=type(bad).__name__), self.assertRaises(ProviderRequestError) as failure:
                parse_conversations(bad, NOW)
            self.assertNotIn("PRIVATE", str(failure.exception))


class TransportTests(unittest.TestCase):
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()

    def test_exact_get_without_proxy_redirect_or_cloudflare_cookies(self):
        route = "/api/organizations/" + ORG + "/chat_conversations?limit=100&is_archived=false"
        with patch("urllib.request.OpenerDirector.open", return_value=self.Response(b"[]")) as opened:
            self.assertEqual(_read_json(route, ORG, "fixture-session", max_bytes=1024), [])
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://claude.ai" + route)
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.get_header("Cookie"), "sessionKey=fixture-session; lastActiveOrg=" + ORG)
        self.assertLessEqual(opened.call_args.kwargs["timeout"], 5)
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value = self.Response(b"{}")
            _read_json("/api/account", ORG, "fixture", max_bytes=1024)
        self.assertEqual(build.call_args.args[0].proxies, {})
        redirect = next(handler for handler in build.call_args.args if isinstance(handler, RejectRedirects))
        with self.assertRaises(ProviderRequestError):
            redirect.redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid")

    def test_rejects_routes_headers_and_size_changes(self):
        for route in ("https://elsewhere.invalid", "/api/organizations/" + ORG + "/chat_conversations/" + CHAT,
                      "/api/account?extra=true", "/api/account#fragment"):
            with self.assertRaises(ProviderRequestError):
                _read_json(route, ORG, "fixture", max_bytes=100)
        for cookie in ("", "fixture\nsecret", "fixture;other=value"):
            with self.assertRaises(ProviderRequestError):
                _read_json("/api/account", ORG, cookie, max_bytes=100)
        with patch("urllib.request.OpenerDirector.open", return_value=self.Response(b"x" * 101)):
            with self.assertRaises(ProviderRequestError):
                _read_json("/api/account", ORG, "fixture", max_bytes=100)
        with self.assertRaises(ProviderRequestError):
            _read_json("/api/account", ORG, "fixture", max_bytes=MAX_METADATA_BYTES + 1)

    def test_rate_limit_and_errors_never_include_remote_body(self):
        error = urllib.error.HTTPError("https://claude.ai", 429, "PRIVATE", {"Retry-After": "600"}, io.BytesIO(b"PRIVATE"))
        with patch("urllib.request.OpenerDirector.open", side_effect=error):
            with self.assertRaises(ProviderRequestError) as failure:
                _read_json("/api/account", ORG, "fixture", max_bytes=100)
        self.assertEqual(failure.exception.status, 429)
        self.assertEqual(failure.exception.retry_after, 600)
        self.assertNotIn("PRIVATE", str(failure.exception))

    def test_account_verification_precedes_chat_read_and_detects_rotation(self):
        with patch("claude_chat_source.credential_revision", return_value="revision"), \
             patch("claude_chat_source._credentials", return_value=(ACCOUNT, ORG, "fixture")), \
             patch("claude_chat_source._read_json", side_effect=[{"uuid": ACCOUNT}, [row()]]) as read:
            result = read_account_chats(now=NOW)
        self.assertEqual(result["account_scope"], hashlib.sha256((ACCOUNT + "\0" + ORG).encode()).hexdigest())
        self.assertEqual(read.call_args_list[0].args[0], "/api/account")
        with patch("claude_chat_source.credential_revision", return_value="revision"), \
             patch("claude_chat_source._credentials", return_value=(ACCOUNT, ORG, "fixture")), \
             patch("claude_chat_source._read_json", return_value={"uuid": ORG}) as read:
            with self.assertRaises(ProviderRequestError) as failure:
                read_account_chats(now=NOW)
        self.assertEqual(failure.exception.reason, "account_mismatch")
        self.assertEqual(read.call_count, 1)
        with patch("claude_chat_source.credential_revision", side_effect=["before", "after"]), \
             patch("claude_chat_source._credentials", return_value=(ACCOUNT, ORG, "fixture")), \
             patch("claude_chat_source._read_json", side_effect=[{"uuid": ACCOUNT}, [row()]]):
            with self.assertRaises(ProviderRequestError): read_account_chats(now=NOW)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        (self.folder / "config.json").write_text(json.dumps({"lastKnownAccountUuid": ACCOUNT}))
        with sqlite3.connect(self.folder / "Cookies") as connection:
            connection.execute("CREATE TABLE cookies(name TEXT,host_key TEXT,path TEXT,value TEXT,encrypted_value BLOB,expires_utc INTEGER)")
            connection.execute("INSERT INTO cookies VALUES ('sessionKey','.claude.ai','/','fixture-secret',X'',?)",
                (int((NOW + 1000 + 11644473600) * 1e6),))
            connection.execute("INSERT INTO cookies VALUES ('lastActiveOrg','.claude.ai','/',?,X'',0)", (ORG,))

    def test_local_revision_never_reads_keychain_and_changes_with_account_cookie_or_org(self):
        with patch("claude_chat_source._keychain_password", side_effect=AssertionError("no Keychain")):
            before = credential_revision(self.folder)
            self.assertRegex(before, r"^[0-9a-f]{64}$")
            with sqlite3.connect(self.folder / "Cookies") as connection:
                connection.execute("UPDATE cookies SET value='rotated-secret' WHERE name='sessionKey'")
            after = credential_revision(self.folder)
            self.assertNotEqual(before, after)
            (self.folder / "config.json").write_text(json.dumps({"lastKnownAccountUuid": ORG}))
            self.assertNotEqual(after, credential_revision(self.folder))

    def test_existing_session_only_expiry_and_header_validation(self):
        with patch("claude_chat_source._keychain_password", return_value=b"fixture") as keychain:
            result = _credentials(self.folder, NOW)
        self.assertEqual(result, (ACCOUNT, ORG, "fixture-secret"))
        keychain.assert_called_once_with("Claude Safe Storage")
        with patch("claude_chat_source._keychain_password", return_value=b"fixture"):
            with self.assertRaises(ProviderRequestError): _credentials(self.folder, NOW + 1001)
            with sqlite3.connect(self.folder / "Cookies") as connection:
                connection.execute("UPDATE cookies SET value='secret; injected=true' WHERE name='sessionKey'")
            with self.assertRaises(ProviderRequestError): _credentials(self.folder, NOW)

    def test_host_binding_removed_only_in_memory(self):
        with sqlite3.connect(self.folder / "Cookies") as connection:
            connection.execute("UPDATE cookies SET value='',encrypted_value=X'01' WHERE name='sessionKey'")
        with patch("claude_chat_source._keychain_password", return_value=b"fixture"), \
             patch("claude_chat_source._decrypt_chromium", return_value=hashlib.sha256(b".claude.ai").digest() + b"decrypted-fixture"):
            self.assertEqual(_credentials(self.folder, NOW)[2], "decrypted-fixture")


class CachedReaderTests(unittest.TestCase):
    def make_reader(self, fetcher):
        self.now = NOW
        self.revision = "one"
        reader = ClaudeChatAccountReader(fetcher=fetcher, revision_reader=lambda: self.revision, clock=lambda: self.now)
        self.addCleanup(reader.close)
        return reader

    def settle(self, reader):
        for _ in range(100):
            with reader._lock: busy = reader._busy
            if not busy: return
            time.sleep(.005)
        self.fail("reader worker did not settle")

    def test_nonblocking_snapshot_freshness_copies_and_active_cadence(self):
        started, release = threading.Event(), threading.Event()
        def fetch():
            started.set()
            release.wait(2)
            return snapshot(live_status="running", live_status_started_at=NOW - 1)
        reader = self.make_reader(fetch)
        before = time.monotonic()
        first = reader.scan(self.now)
        self.assertLess(time.monotonic() - before, .2)
        self.assertFalse(first["available"])
        self.assertTrue(started.wait(1))
        release.set(); self.settle(reader)
        current = reader.scan(self.now)
        self.assertTrue(current["fresh"])
        self.assertEqual(current["retry_at"], NOW + 5)
        current["conversations"][CHAT]["title"] = "mutated"
        self.assertNotEqual(reader.scan(self.now)["conversations"][CHAT]["title"], "mutated")
        # Prevent another fetch so this checks expiration of the last receipt.
        reader._next_at = NOW + 1000
        self.now += FRESH_SECONDS + .01
        self.assertEqual(reader.scan(self.now)["conversations"], {})
        self.assertFalse(reader.scan(self.now)["available"])

    def test_empty_account_is_available(self):
        reader = self.make_reader(lambda: {"account_scope": SCOPE, "conversations": {}})
        reader.scan(self.now); self.settle(reader)
        current = reader.scan(self.now)
        self.assertTrue(current["available"])
        self.assertEqual(current["conversations"], {})
        self.assertEqual(current["retry_at"], NOW + 10)

    def test_failed_worker_start_releases_slot_and_retries_without_restart(self):
        calls = []
        reader = self.make_reader(lambda: (calls.append(True), snapshot())[1])
        with patch("claude_chat_source.threading.Thread.start", side_effect=RuntimeError("PRIVATE resource error")) as start:
            result = reader.scan(self.now)
            self.assertFalse(reader._busy)
            self.assertFalse(result["available"])
            self.assertEqual(result["error_code"], "unavailable")
            self.assertNotIn("PRIVATE", json.dumps(result))
            self.now += 1
            reader.scan(self.now)
            self.assertEqual(start.call_count, 1)
        self.assertFalse(calls)
        self.now += 30
        reader.scan(self.now); self.settle(reader)
        self.assertEqual(len(calls), 1)
        self.assertTrue(reader.scan(self.now)["available"])

    def test_account_change_clears_rows_and_discards_inflight_old_result(self):
        release = threading.Event()
        reader = self.make_reader(lambda: (release.wait(2), snapshot())[1])
        reader.scan(self.now)
        self.revision = "two"
        self.assertEqual(reader.scan(self.now)["conversations"], {})
        release.set(); self.settle(reader)
        self.assertIsNone(reader._at)
        reader.scan(self.now); self.settle(reader)
        self.assertTrue(reader.scan(self.now)["fresh"])
        self.revision = None
        self.assertFalse(reader.scan(self.now)["available"])
        self.assertEqual(reader.scan(self.now)["conversations"], {})

    def test_429_retry_after_floor_survives_success_and_other_account_rotation(self):
        responses = iter([snapshot(), {"error_code": "rate_limited", "retry_after": 600}, snapshot()])
        reader = self.make_reader(lambda: next(responses))
        reader.scan(self.now); self.settle(reader)
        self.now += 10
        reader.scan(self.now); self.settle(reader)
        self.assertEqual(reader._next_at, self.now + 600)
        self.now += 600
        reader.scan(self.now); self.settle(reader)
        self.assertEqual(reader._next_at, self.now + 600)
        self.assertTrue(reader.scan(self.now)["fresh"])
        self.now += 16
        self.assertFalse(reader.scan(self.now)["fresh"])

    def test_auth_failures_invalidate_fresh_cache_without_retry_loop(self):
        responses = iter([snapshot(), {"error_code": "authentication"}])
        reader = self.make_reader(lambda: next(responses))
        reader.scan(self.now); self.settle(reader)
        self.now += 10
        reader.scan(self.now); self.settle(reader)
        result = reader.scan(self.now)
        self.assertFalse(result["fresh"])
        self.assertEqual(result["conversations"], {})
        self.assertEqual(result["error_code"], "authentication")
        self.assertEqual(result["retry_at"], self.now + 300)

    def test_close_discards_late_response(self):
        release = threading.Event()
        reader = self.make_reader(lambda: (release.wait(2), snapshot())[1])
        reader.scan(self.now)
        reader.close()
        release.set(); self.settle(reader)
        self.assertFalse(reader.scan(self.now)["available"])
        self.assertEqual(reader.scan(self.now)["conversations"], {})

    def test_helper_errors_are_fixed_and_bounded(self):
        output = io.StringIO()
        with patch("claude_chat_source.read_account_chats", side_effect=RuntimeError("PRIVATE")), contextlib.redirect_stdout(output):
            helper_main()
        self.assertEqual(json.loads(output.getvalue()), {"error_code": "unavailable"})
        with patch("claude_chat_source.read_process", side_effect=ValueError("PRIVATE")) as process:
            self.assertEqual(_fetch_snapshot(), {"error_code": "unavailable"})
        self.assertIn("--read-claude-chats", process.call_args.args[0])
        self.assertLessEqual(process.call_args.kwargs["timeout"], 15)
        self.assertLessEqual(process.call_args.kwargs["max_output"], 512 * 1024)

    def test_unexpected_local_revision_failure_cannot_wedge_worker(self):
        reader = self.make_reader(lambda: snapshot())
        calls = iter(["one", RuntimeError("PRIVATE"), "one", "one", "one"])
        def revision():
            result = next(calls)
            if isinstance(result, Exception): raise result
            return result
        reader._revision_reader = revision
        reader.scan(self.now); self.settle(reader)
        self.assertFalse(reader._busy)
        self.assertIsNone(reader._at)
        reader.scan(self.now); self.settle(reader)
        self.assertTrue(reader.scan(self.now)["fresh"])
        reader._revision_reader = lambda: (_ for _ in ()).throw(RuntimeError("PRIVATE"))
        self.assertFalse(reader.scan(self.now)["fresh"])

    def test_malformed_helper_error_and_huge_retry_hint_are_bounded(self):
        reader = self.make_reader(lambda: {"error_code": {"private": "body"}, "retry_after": 10 ** 1000})
        reader.scan(self.now); self.settle(reader)
        result = reader.scan(self.now)
        self.assertEqual(result["error_code"], "invalid_response")
        self.assertEqual(result["retry_at"], self.now + 30)


if __name__ == "__main__":
    unittest.main()
