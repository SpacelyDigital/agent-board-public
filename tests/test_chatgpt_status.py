import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import uuid

from chatgpt_status import ChatGPTStatusReader, _read_credentials, read_stream_status, read_conversation_metadata
from privacy_network import ProviderRequestError, RejectRedirects


class StreamStatusTransportTests(unittest.TestCase):
    identifier = '11111111-1111-4111-8111-111111111111'

    def test_metadata_only_exact_origin_and_redirect_rejection(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        class Opener:
            def open(inner, request, timeout):
                self.assertEqual(request.full_url,
                    'https://chatgpt.com/backend-api/conversation/' + self.identifier + '/stream_status')
                self.assertEqual(request.method, 'GET')
                self.assertEqual(request.get_header('Authorization'), 'Bearer fixture-token')
                self.assertEqual(request.get_header('Chatgpt-account-id'), 'fixture-account')
                self.assertLessEqual(timeout, 10)
                return Response(b'{"status":"COMPLETE"}')
        with patch('urllib.request.build_opener', return_value=Opener()) as build:
            self.assertEqual(read_stream_status(self.identifier, 'fixture-token', 'fixture-account'), 'COMPLETE')
        handlers = build.call_args.args
        self.assertTrue(any(isinstance(handler, RejectRedirects) for handler in handlers))
        self.assertEqual(handlers[0].proxies, {})
        with self.assertRaises(ProviderRequestError):
            next(handler for handler in handlers if isinstance(handler, RejectRedirects)).redirect_request(
                None, None, 302, '', {}, 'https://other.example/')

    def test_rejects_untrusted_ids_headers_and_remote_body(self):
        for identifier in ['../auth', self.identifier + '/extra', 'https://other.example', 'x' * 36]:
            with self.assertRaises(ProviderRequestError):
                read_stream_status(identifier, 'fixture', None)
        for token in ['', 'abc\nsecret', 'abc\rsecret']:
            with self.assertRaises(ProviderRequestError):
                read_stream_status(self.identifier, token, None)
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        for body in [b'{"status":"not-a-status","secret":"private"}', b'[]', b'x' * 16385]:
            with patch('urllib.request.OpenerDirector.open', return_value=Response(body)):
                with self.assertRaises(ProviderRequestError) as failure:
                    read_stream_status(self.identifier, 'fixture', None)
                self.assertNotIn('private', str(failure.exception))
        with patch('urllib.request.OpenerDirector.open', side_effect=urllib.error.HTTPError(
            'https://chatgpt.com', 401, 'private body', {}, io.BytesIO(b'private body'))):
            with self.assertRaises(ProviderRequestError) as failure:
                read_stream_status(self.identifier, 'fixture', None)
            self.assertEqual(failure.exception.status, 401)
            self.assertNotIn('private', str(failure.exception))

    def test_discovery_reads_unexpanded_metadata_and_retains_only_allowlisted_fields(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        item = {'id': self.identifier, 'title': '  Browser   chat  ',
                'create_time': '2026-01-01T00:00:00Z', 'update_time': '2026-01-02T00:00:00+00:00',
                'is_archived': False, 'mapping': None, 'async_status': 3,
                'latest_assistant_turn_created_at': None, 'snippet': 'must not be retained',
                'unrelated': 'must not be retained'}
        body = json.dumps({'items': [item, {**item, 'id': str(uuid.uuid4()), 'is_archived': True},
            {**item, 'id': str(uuid.uuid4()), 'is_temporary_chat': True}]}).encode()
        with patch('urllib.request.OpenerDirector.open', return_value=Response(body)) as opened:
            result = read_conversation_metadata('fixture-token', 'fixture-account')
        url = opened.call_args.args[0].full_url
        self.assertEqual(url, 'https://chatgpt.com/backend-api/conversations?offset=0&limit=100&order=updated&expand=false&hide_snorlax=false&is_archived=false')
        self.assertEqual(set(result), {self.identifier})
        self.assertEqual(result[self.identifier], {'title': 'Browser chat',
            'source_created_at': 1767225600.0, 'source_updated_at': 1767312000.0, 'async_status': 3})
        for body in [{'items': [{**item, 'mapping': {'private': 'message'}}]}, {'items': [item] * 101},
                     {'items': [item, item]}, {'items': None}]:
            with patch('urllib.request.OpenerDirector.open', return_value=Response(json.dumps(body).encode())):
                with self.assertRaises(ProviderRequestError):
                    read_conversation_metadata('fixture-token', None)


class StatusQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.write_auth()
        self.readers = []

    def tearDown(self):
        for reader in self.readers: reader.close()
        self.temporary.cleanup()

    def write_auth(self, token='fixture-token', account='fixture-account'):
        (self.root / 'auth.json').write_text(json.dumps({'tokens': {'access_token': token, 'account_id': account}}))

    def reader(self, **kwargs):
        kwargs.setdefault('metadata_request', lambda token, account: {})
        reader = ChatGPTStatusReader(self.root, **kwargs)
        self.readers.append(reader)
        return reader

    def await_condition(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if condition(): return
            time.sleep(.005)
        self.fail('Background status worker did not finish')

    def test_full_batch_is_nonblocking_and_concurrency_is_bounded(self):
        gate = threading.Event()
        seen = []
        active = 0
        peak = 0
        lock = threading.Lock()
        def request(identifier, token, account):
            nonlocal active, peak
            with lock:
                active += 1; peak = max(peak, active)
            gate.wait(2)
            with lock:
                seen.append(identifier); active -= 1
            return 'COMPLETE'
        ids = [str(uuid.uuid4()) for _ in range(43)]
        reader = self.reader(request=request)
        started = time.monotonic()
        self.assertEqual(reader.scan(ids), {})
        self.assertLess(time.monotonic() - started, .25)
        self.await_condition(lambda: active == 2)
        self.assertEqual(seen, [])
        gate.set()
        self.await_condition(lambda: len(seen) == 43)
        self.assertEqual(set(seen), set(ids))
        self.assertEqual(peak, 2)
        self.assertEqual(len(reader.scan(ids)), 43)

    def test_stream_transition_freshness_and_auth_change(self):
        current_time = [1000.0]
        result = ['IS_STREAMING']
        called = []
        def request(identifier, token, account):
            called.append(token)
            return result[0]
        identifier = str(uuid.uuid4())
        reader = self.reader(request=request, clock=lambda: current_time[0])
        reader.scan([identifier])
        self.await_condition(lambda: identifier in reader._cache)
        self.assertEqual(reader.scan([identifier])[identifier]['status'], 'IS_STREAMING')
        current_time[0] += 5
        result[0] = 'COMPLETE'
        reader.scan([identifier])
        self.await_condition(lambda: reader._cache[identifier]['status'] == 'COMPLETE')
        self.assertEqual(reader.scan([identifier])[identifier]['observed_at'], 1005)
        # The caller gets stale evidence explicitly, not an invented completion.
        with reader._lock:
            reader._next_poll[identifier] = 2000
        current_time[0] += 16
        self.assertFalse(reader.scan([identifier])[identifier]['fresh'])
        self.write_auth(token='different-token', account='different-account')
        self.assertEqual(reader.scan([identifier]), {})
        self.await_condition(lambda: called[-1] == 'different-token')

    def test_auth_failure_backs_off_queue_and_credentials_are_never_synthesized(self):
        now = [1000.0]
        called = []
        def request(identifier, token, account):
            called.append(identifier)
            raise ProviderRequestError('http', 401)
        ids = [str(uuid.uuid4()) for _ in range(20)]
        reader = self.reader(request=request, clock=lambda: now[0])
        reader.scan(ids)
        self.await_condition(lambda: reader._workers == 0)
        self.assertLessEqual(len(called), 2)
        results = reader.scan(ids)
        self.assertTrue(all(v['status'] == 'UNAVAILABLE' and v['http_status'] == 401 for v in results.values()))
        self.assertLessEqual(len(called), 2)
        (self.root / 'auth.json').unlink()
        self.assertIsNone(_read_credentials(self.root))
        self.assertTrue(all(not value['fresh'] and value['error'] == 'credentials' for value in reader.scan(ids).values()))

    def test_inaccessible_conversation_does_not_back_off_other_chats(self):
        ids = [str(uuid.uuid4()) for _ in range(12)]
        called = []
        def request(identifier, token, account):
            called.append(identifier)
            if identifier == ids[0]:
                raise ProviderRequestError('http', 403)
            return 'IS_STREAMING'
        reader = self.reader(request=request)
        reader.scan(ids)
        self.await_condition(lambda: len(called) == len(ids))
        readings = reader.scan(ids)
        self.assertEqual(readings[ids[0]]['http_status'], 403)
        self.assertTrue(all(readings[identifier]['status'] == 'IS_STREAMING' for identifier in ids[1:]))
        self.assertEqual(reader._backoff_until, 0)
        self.assertIsNotNone(reader.current_account_identity())

    def test_current_ownership_requires_present_credentials_but_retains_lifecycle_scope(self):
        reader = self.reader()
        reader.scan([])
        self.await_condition(lambda: reader._workers == 0)
        original = reader.account_identity()
        self.assertEqual(reader.current_account_identity(), original)
        (self.root / 'auth.json').unlink()
        reader.scan([])
        self.assertIsNone(reader.current_account_identity())
        self.assertEqual(reader.account_identity(), original)
        self.write_auth()
        reader.scan([])
        self.await_condition(lambda: reader._workers == 0)
        self.assertEqual(reader.current_account_identity(), original)
        reader.close()
        self.assertIsNone(reader.current_account_identity())
        self.assertEqual(reader.account_identity(), original)

    def test_account_auth_rejection_fences_ownership_until_a_valid_current_read(self):
        for status in (401, 403):
            with self.subTest(status=status):
                now = [1000.0]
                reject = [True]
                def metadata(*_):
                    if reject[0]:
                        raise ProviderRequestError('http', status)
                    return {}
                reader = self.reader(metadata_request=metadata, clock=lambda: now[0])
                reader.scan([])
                self.await_condition(lambda: reader._workers == 0)
                scope = reader.account_identity()
                self.assertIsNone(reader.current_account_identity())
                now[0] += 31
                reject[0] = False
                reader.scan([])
                self.await_condition(lambda: reader._workers == 0)
                self.assertEqual(reader.current_account_identity(), scope)
                reader.close()

    def test_late_success_before_auth_rejection_cannot_restore_ownership(self):
        now = [1000.0]
        entered = threading.Event()
        release = threading.Event()
        def metadata(*_):
            entered.set()
            release.wait(2)
            return {}
        def request(*_):
            raise ProviderRequestError('http', 401)
        reader = self.reader(request=request, metadata_request=metadata, clock=lambda: now[0])
        self.addCleanup(release.set)
        reader.scan([str(uuid.uuid4())])
        self.assertTrue(entered.wait(1))
        self.await_condition(lambda: reader._credential_rejected)
        scope = reader.account_identity()
        release.set()
        self.await_condition(lambda: reader._workers == 0)
        self.assertIsNone(reader.current_account_identity())
        self.assertEqual(reader.account_identity(), scope)
        now[0] += 31
        reader.scan([])
        self.await_condition(lambda: reader._workers == 0)
        self.assertEqual(reader.current_account_identity(), scope)

    def test_reply_auth_rejection_is_scoped_to_current_credentials(self):
        now = [1000.0]
        def reply(*_):
            raise ProviderRequestError('http', 401)
        identifier = str(uuid.uuid4())
        reader = self.reader(request=lambda *_: 'COMPLETE', reply_request=reply, clock=lambda: now[0])
        reader.scan([identifier], reply_candidates={identifier: now[0]})
        self.await_condition(lambda: reader._workers == 0)
        scope = reader.account_identity()
        self.assertIsNone(reader.current_account_identity())
        self.write_auth('refreshed-token')
        reader.scan([])
        self.await_condition(lambda: reader._workers == 0)
        self.assertEqual(reader.current_account_identity(), scope)

    def test_missing_file_cannot_clear_rejection_of_identical_returning_credentials(self):
        rejected = [True]
        gate = threading.Event()
        def metadata(*_):
            if rejected[0]:
                raise ProviderRequestError('http', 401)
            gate.wait(2)
            return {}
        reader = self.reader(metadata_request=metadata)
        self.addCleanup(gate.set)
        reader.scan([])
        self.await_condition(lambda: reader._workers == 0)
        scope = reader.account_identity()
        self.assertIsNone(reader.current_account_identity())
        (self.root / 'auth.json').unlink()
        reader.scan([])
        self.write_auth()
        rejected[0] = False
        reader.scan([])
        self.assertIsNone(reader.current_account_identity())
        gate.set()
        self.await_condition(lambda: reader._workers == 0)
        self.assertEqual(reader.current_account_identity(), scope)

    def test_same_account_token_refresh_preserves_account_scope(self):
        reader = self.reader()
        reader.scan([])
        first = reader.account_identity()
        self.write_auth(token='refreshed-token')
        reader.scan([])
        self.assertEqual(reader.account_identity(), first)
        self.write_auth(token='new-account-token', account='different-account')
        reader.scan([])
        self.assertNotEqual(reader.account_identity(), first)

    def test_fresh_activity_rechecks_complete_chat_ahead_of_over_100_cold_chats(self):
        now = [1000.0]
        active = str(uuid.uuid4())
        cold = [str(uuid.uuid4()) for _ in range(120)]
        metadata = {}
        calls = []
        gate = threading.Event()
        def request(identifier, token, account):
            calls.append(identifier)
            if identifier in cold:
                gate.wait(2)
            return 'COMPLETE' if now[0] == 1000 else 'IS_STREAMING' if identifier == active else 'COMPLETE'
        reader = self.reader(request=request, metadata_request=lambda *_: dict(metadata), clock=lambda: now[0])
        reader.scan([active])
        self.await_condition(lambda: reader._workers == 0)
        self.assertEqual(reader._cache[active]['status'], 'COMPLETE')
        now[0] = 1010
        metadata[active] = {'title': 'Active chat', 'source_created_at': 900,
                            'source_updated_at': 999, 'async_status': 3}
        reader.scan([active, *cold])
        self.await_condition(lambda: active in reader.discovered_chats())
        # The fresh provider hint promotes an exact-ID read before its normal
        # COMPLETE poll deadline, without changing the observed status itself.
        self.assertEqual(reader.scan([active, *cold])[active]['status'], 'COMPLETE')
        gate.set()
        self.await_condition(lambda: calls.count(active) == 2)
        self.assertLessEqual(calls.index(active, 1), 3)
        self.await_condition(lambda: reader._workers == 0)
        self.assertEqual(reader._cache[active]['status'], 'IS_STREAMING')
        self.assertEqual(set(calls), {active, *cold})

    def test_aged_history_gets_reserved_slot_among_active_priorities(self):
        now = [1000.0]
        live = [str(uuid.uuid4()) for _ in range(12)]
        cold = [str(uuid.uuid4()) for _ in range(8)]
        calls = []
        gate = threading.Event()
        reader = self.reader(request=lambda identifier, *_: (calls.append(identifier), gate.wait(2), 'COMPLETE')[-1],
                             clock=lambda: now[0])
        # Establish the account, then model known active polls and old queued
        # history without sending any network requests.
        reader.scan([])
        self.await_condition(lambda: reader._workers == 0)
        with reader._lock:
            for identifier in live:
                reader._cache[identifier] = {'status': 'IS_STREAMING', 'observed_at': 996, 'error': None, 'http_status': None}
                reader._last_attempt[identifier] = 996
            reader._first_requested.update({identifier: 960 for identifier in cold})
        reader.scan([*live, *cold])
        self.await_condition(lambda: len(calls) == 2)
        gate.set()
        self.await_condition(lambda: reader._workers == 0)
        self.assertTrue(all(identifier in live for identifier in calls[:2]))
        # Two workers may enter the request callback in either order, but the
        # reserved old chat must start alongside the first four dispatches.
        self.assertLessEqual(calls.index(cold[0]), 4)
        self.assertEqual(set(calls), set(live + cold))

    def test_stale_activity_hint_does_not_bypass_complete_poll_interval(self):
        now = [1000.0]
        identifier = str(uuid.uuid4())
        calls = []
        reader = self.reader(request=lambda ident, *_: (calls.append(ident), 'COMPLETE')[-1], clock=lambda: now[0])
        reader.scan([identifier])
        self.await_condition(lambda: reader._workers == 0)
        with reader._lock:
            reader._discovered = {identifier: {'async_status': 3, 'source_updated_at': 1000}}
            reader._discovered_at = 960
            reader._next_poll['conversation-metadata'] = 2000
        now[0] = 1001
        reader.scan([identifier])
        self.assertEqual(calls, [identifier])

    def test_account_switch_discards_inflight_reply(self):
        gate = threading.Event()
        entered = threading.Event()
        identifier = str(uuid.uuid4())
        def request(identifier, token, account):
            if account == 'fixture-account':
                entered.set()
                gate.wait(2)
                return 'IS_STREAMING'
            return 'COMPLETE'
        reader = self.reader(request=request)
        reader.scan([identifier])
        self.assertTrue(entered.wait(1))
        self.write_auth(token='new-token', account='new-account')
        reader.scan([identifier])
        self.await_condition(lambda: reader._cache.get(identifier, {}).get('status') == 'COMPLETE')
        gate.set()
        self.await_condition(lambda: reader._workers == 0)
        self.assertEqual(reader.scan([identifier])[identifier]['status'], 'COMPLETE')

    def test_rejects_auth_symlink_and_invalid_token(self):
        (self.root / 'auth.json').rename(self.root / 'other.json')
        (self.root / 'auth.json').symlink_to(self.root / 'other.json')
        self.assertIsNone(_read_credentials(self.root))
        (self.root / 'auth.json').unlink()
        self.write_auth(token='bad\nheader')
        self.assertIsNone(_read_credentials(self.root))

    def test_discovers_browser_chat_without_local_ids_and_expires_metadata(self):
        now = [1000.0]
        identifier = str(uuid.uuid4())
        title = ['New browser chat']
        calls = []
        def metadata_request(token, account):
            calls.append(now[0])
            return {identifier: {'title': title[0], 'source_created_at': 990, 'source_updated_at': 999}}
        reader = self.reader(metadata_request=metadata_request, clock=lambda: now[0])
        self.assertEqual(reader.scan([]), {})
        self.await_condition(lambda: reader.discovery_available())
        self.assertEqual(reader.discovered_chats()[identifier]['title'], 'New browser chat')
        copy = reader.discovered_chats()
        copy[identifier]['title'] = 'Do not mutate reader'
        self.assertEqual(reader.discovered_chats()[identifier]['title'], 'New browser chat')
        now[0] += 9
        reader.scan([])
        self.assertEqual(len(calls), 1)
        now[0] += 2
        title[0] = 'Renamed browser chat'
        reader.scan([])
        self.await_condition(lambda: reader.discovered_chats()[identifier]['title'] == title[0])
        now[0] += 31
        self.assertFalse(reader.discovery_available())
        self.assertEqual(reader.discovered_chats(), {})
        self.write_auth('new-token', 'new-account')
        reader.scan([])
        # Account changes clear old evidence before the next response arrives.
        self.assertNotEqual(reader._fingerprint, None)

    def test_empty_remote_account_is_successful_discovery_and_credentials_clear_it(self):
        reader = self.reader()
        reader.scan([])
        self.await_condition(lambda: reader.discovery_available())
        self.assertEqual(reader.discovered_chats(), {})
        (self.root / 'auth.json').unlink()
        reader.scan([])
        self.assertFalse(reader.discovery_available())
        self.assertEqual(reader.discovered_chats(), {})

    def test_account_switch_discards_inflight_discovery(self):
        gate = threading.Event()
        entered = threading.Event()
        old_id, new_id = str(uuid.uuid4()), str(uuid.uuid4())
        def metadata_request(token, account):
            if account == 'fixture-account':
                entered.set()
                gate.wait(2)
                return {old_id: {'title': 'Old account', 'source_created_at': 1, 'source_updated_at': 2}}
            return {new_id: {'title': 'New account', 'source_created_at': 1, 'source_updated_at': 2}}
        reader = self.reader(metadata_request=metadata_request)
        reader.scan([])
        self.assertTrue(entered.wait(1))
        self.write_auth(token='new-token', account='new-account')
        reader.scan([])
        self.await_condition(lambda: new_id in reader.discovered_chats())
        gate.set()
        self.await_condition(lambda: reader._workers == 0)
        self.assertEqual(set(reader.discovered_chats()), {new_id})


if __name__ == '__main__':
    unittest.main()
