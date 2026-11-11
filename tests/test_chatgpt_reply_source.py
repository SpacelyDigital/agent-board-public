import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error

from chatgpt_reply_source import MAX_RESPONSE_BYTES, parse_last_reply, read_last_reply
from chatgpt_status import ChatGPTStatusReader
from privacy_network import ProviderRequestError, RejectRedirects


IDENTIFIER = '11111111-1111-4111-8111-111111111111'
MESSAGE = '22222222-2222-4222-8222-222222222222'


def response_fixture():
    return {'conversation_id': IDENTIFIER, 'current_node': MESSAGE,
            'is_archived': False, 'is_temporary_chat': False,
            'page_info': {'has_previous_page': True, 'has_next_page': False},
            'messages': [{'id': MESSAGE, 'author': {'role': 'assistant'},
                          'status': 'finished_successfully', 'end_turn': True,
                          'channel': 'final', 'recipient': 'all',
                          'create_time': 980, 'update_time': 990,
                          'metadata': {'finish_details': {'type': 'stop'}},
                          'content': {'parts': ['private fixture content']}}]}


class LastReplyParserTests(unittest.TestCase):
    def test_exact_leaf_only_returns_allowlisted_hashed_marker(self):
        result = parse_last_reply(response_fixture(), IDENTIFIER, now=1000)
        self.assertEqual(result, {'reply_id': hashlib.sha256(MESSAGE.encode()).hexdigest(),
                                  'created_at': 980, 'updated_at': 990})
        self.assertNotIn('private', json.dumps(result))
        self.assertNotIn(MESSAGE, json.dumps(result))

    def test_older_page_is_not_required_but_incomplete_current_page_is_rejected(self):
        data = response_fixture()
        data['page_info']['has_next_page'] = True
        with self.assertRaises(ProviderRequestError):
            parse_last_reply(data, IDENTIFIER, now=1000)

    def test_new_user_turn_prevents_old_assistant_ready(self):
        data = response_fixture()
        data['messages'].append({'id': 'new-user', 'author': {'role': 'user'}, 'content': {}})
        data['current_node'] = 'new-user'
        self.assertIsNone(parse_last_reply(data, IDENTIFIER, now=1000))

    def test_nonterminal_hidden_tool_and_partial_replies_are_not_ready(self):
        changes = [('author', {'role': 'tool'}), ('status', 'in_progress'), ('status', 'finished_partial'),
                   ('end_turn', False), ('end_turn', 1), ('channel', 'analysis'), ('recipient', 'browser')]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                data = response_fixture(); data['messages'][0][key] = value
                self.assertIsNone(parse_last_reply(data, IDENTIFIER, now=1000))
        for key in ['is_error', 'is_visually_hidden_from_conversation', 'is_merged', 'is_fragment', 'continuation_id']:
            with self.subTest(key=key):
                data = response_fixture(); data['messages'][0]['metadata'][key] = True
                self.assertIsNone(parse_last_reply(data, IDENTIFIER, now=1000))
        data = response_fixture(); data['messages'][0]['metadata']['finish_details']['type'] = 'content_filter'
        self.assertIsNone(parse_last_reply(data, IDENTIFIER, now=1000))
        data = response_fixture(); data['context_truncation_continuation'] = 'continuation'
        self.assertIsNone(parse_last_reply(data, IDENTIFIER, now=1000))

    def test_ambiguous_or_mismatched_current_leaf_is_rejected(self):
        changes = [('conversation_id', MESSAGE), ('current_node', 'missing'), ('current_node', '../unsafe'),
                   ('is_archived', True), ('is_temporary_chat', True), ('mapping', {}),
                   ('messages', []), ('messages', response_fixture()['messages'] * 2)]
        for key, value in changes:
            with self.subTest(key=key):
                data = response_fixture(); data[key] = value
                with self.assertRaises(ProviderRequestError):
                    parse_last_reply(data, IDENTIFIER, now=1000)

    def test_future_missing_invalid_and_reversed_times_are_rejected(self):
        for key, value in [('create_time', None), ('update_time', float('nan')), ('update_time', True),
                           ('update_time', 1003), ('update_time', 970), ('create_time', -1)]:
            with self.subTest(key=key, value=value):
                data = response_fixture(); data['messages'][0][key] = value
                with self.assertRaises(ProviderRequestError):
                    parse_last_reply(data, IDENTIFIER, now=1000)

    def test_nullable_update_time_preserves_terminal_evidence_without_inventing_finish_time(self):
        data = response_fixture(); data['messages'][0]['update_time'] = None
        self.assertEqual(parse_last_reply(data, IDENTIFIER, now=1000),
                         {'reply_id': hashlib.sha256(MESSAGE.encode()).hexdigest(),
                          'created_at': 980, 'updated_at': None})
        for key, value in [('status', 'in_progress'), ('author', {'role': 'tool'}),
                           ('metadata', {'is_visually_hidden_from_conversation': True})]:
            changed = response_fixture(); changed['messages'][0]['update_time'] = None
            changed['messages'][0][key] = value
            self.assertIsNone(parse_last_reply(changed, IDENTIFIER, now=1000))


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class LastReplyTransportTests(unittest.TestCase):
    def test_fixed_origin_bounded_latest_turn_no_proxies_or_redirects(self):
        class Opener:
            def open(inner, request, timeout):
                self.assertEqual(request.full_url,
                    'https://chatgpt.com/backend-api/conversations/' + IDENTIFIER + '?num_turns=1')
                self.assertEqual(request.method, 'GET')
                self.assertEqual(request.get_header('Authorization'), 'Bearer fixture-token')
                self.assertEqual(request.get_header('Chatgpt-account-id'), 'fixture-account')
                self.assertEqual(timeout, 8)
                return Response(json.dumps(response_fixture()).encode())
        with patch('urllib.request.build_opener', return_value=Opener()) as build:
            result = read_last_reply(IDENTIFIER, 'fixture-token', 'fixture-account')
        self.assertEqual(set(result), {'reply_id', 'created_at', 'updated_at'})
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        redirect = next(handler for handler in handlers if isinstance(handler, RejectRedirects))
        with self.assertRaises(ProviderRequestError):
            redirect.redirect_request(None, None, 302, '', {}, 'https://other.example/')

    def test_request_injection_is_rejected_before_network(self):
        with patch('urllib.request.build_opener') as build:
            for identifier in ['../auth', IDENTIFIER + '?num_turns=100', 'https://other.example']:
                with self.assertRaises(ProviderRequestError):
                    read_last_reply(identifier, 'fixture-token', None)
            for token in ['', 'bad\nheader', 'a' * 16385]:
                with self.assertRaises(ProviderRequestError):
                    read_last_reply(IDENTIFIER, token, None)
            for timeout in [True, 0, 11, float('nan')]:
                with self.assertRaises(ProviderRequestError):
                    read_last_reply(IDENTIFIER, 'fixture-token', None, timeout=timeout)
            build.assert_not_called()

    def test_errors_never_contain_body_or_credentials(self):
        for body in [b'private fixture body', b'[]', b'x' * (MAX_RESPONSE_BYTES + 1)]:
            with patch('urllib.request.OpenerDirector.open', return_value=Response(body)):
                with self.assertRaises(ProviderRequestError) as failure:
                    read_last_reply(IDENTIFIER, 'private-token', None)
                self.assertNotIn('private', str(failure.exception))
        with patch('urllib.request.OpenerDirector.open', side_effect=urllib.error.HTTPError(
                'https://chatgpt.com', 401, 'private body', {}, io.BytesIO(b'private body'))):
            with self.assertRaises(ProviderRequestError) as failure:
                read_last_reply(IDENTIFIER, 'private-token', None)
            self.assertEqual(failure.exception.status, 401)
            self.assertNotIn('private', str(failure.exception))


class LastReplyQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.write_auth('fixture-token', 'fixture-account')
        self.now = 1000.0
        self.status = 'COMPLETE'
        self.calls = []
        self.reply = parse_last_reply(response_fixture(), IDENTIFIER, now=self.now)
        self.reader = ChatGPTStatusReader(self.root, clock=lambda: self.now,
            request=lambda *args: self.status, metadata_request=lambda *args: {},
            reply_request=self.read_reply)

    def tearDown(self):
        self.reader.close()
        self.temporary.cleanup()

    def write_auth(self, token, account):
        (self.root / 'auth.json').write_text(json.dumps({'tokens': {'access_token': token, 'account_id': account}}))

    def read_reply(self, *args):
        self.calls.append(args)
        if isinstance(self.reply, Exception): raise self.reply
        return self.reply

    def scan(self, revision=990):
        return self.reader.scan([IDENTIFIER], reply_candidates={IDENTIFIER: revision})

    def await_idle(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.reader._lock:
                idle = self.reader._workers == 0
            if idle: return
            time.sleep(.005)
        self.fail('Worker did not finish')

    def test_no_candidate_never_requests_message_turn(self):
        self.reader.scan([IDENTIFIER]); self.await_idle()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.reader.completed_replies(), {})

    def test_nullable_update_time_survives_allowlisted_queue_record(self):
        self.reply['updated_at'] = None
        self.scan(); self.await_idle()
        self.assertIsNone(self.reader.completed_replies()[IDENTIFIER]['updated_at'])

    def test_completed_revision_is_read_once_and_receipt_expires(self):
        self.scan(); self.await_idle()
        reading = self.reader.completed_replies()[IDENTIFIER]
        self.assertEqual(reading['revision'], 990)
        self.assertEqual(reading['account_scope'], hashlib.sha256(b'fixture-account').hexdigest())
        self.assertTrue(reading['fresh'])
        self.reader.ack_completed_replies({IDENTIFIER: reading})
        for increment in [1, 12, 20]:
            self.now += increment; self.scan(); self.await_idle()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.reader.completed_replies(), {})

    def test_unacknowledged_marker_refreshes_after_transient_consumer_failure(self):
        self.scan(); self.await_idle()
        self.now += 16
        self.assertEqual(self.reader.completed_replies(), {})
        self.now += 15; self.scan(); self.await_idle()
        self.assertEqual(len(self.calls), 2)
        reading = self.reader.completed_replies()[IDENTIFIER]
        self.reader.ack_completed_replies({IDENTIFIER: reading})
        self.now += 31; self.scan(); self.await_idle()
        self.assertEqual(len(self.calls), 2)

    def test_disappearing_candidate_reappears_without_losing_unacknowledged_marker(self):
        self.scan(); self.await_idle()
        self.reader.scan([IDENTIFIER], reply_candidates={}); self.await_idle()
        self.now += 40
        self.scan(); self.await_idle()
        self.assertEqual(len(self.calls), 2)
        self.assertIn(IDENTIFIER, self.reader.completed_replies())

    def test_old_receipt_cannot_ack_a_new_revision_or_account(self):
        self.scan(); self.await_idle()
        old = self.reader.completed_replies()
        self.now += 1; self.scan(revision=1001); self.await_idle()
        self.reader.ack_completed_replies(old)
        self.assertNotIn(IDENTIFIER, self.reader._reply_acks)
        fresh = self.reader.completed_replies()
        fresh[IDENTIFIER]['account_scope'] = '0' * 64
        self.reader.ack_completed_replies(fresh)
        self.assertNotIn(IDENTIFIER, self.reader._reply_acks)
        self.reader.ack_completed_replies(self.reader.completed_replies())
        self.assertIn(IDENTIFIER, self.reader._reply_acks)

    def test_nonterminal_marker_stops_after_three_attempts_until_revision_changes(self):
        self.reply = None
        for _ in range(5):
            self.scan(); self.await_idle(); self.now += 31
        self.assertEqual(len(self.calls), 3)
        self.scan(revision=self.now - 1); self.await_idle()
        self.assertEqual(len(self.calls), 4)

    def test_forbidden_single_chat_does_not_back_off_account(self):
        self.reply = ProviderRequestError('http', 403)
        self.scan(); self.await_idle()
        self.assertEqual(self.reader._backoff_until, 0)
        self.assertEqual(self.reader.completed_replies(), {})

    def test_changed_revision_accelerates_and_invalidates_old_marker(self):
        self.scan(); self.await_idle()
        self.now += 1
        self.scan(revision=1001); self.await_idle()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.reader.completed_replies()[IDENTIFIER]['revision'], 1001)

    def test_failure_and_nonterminal_retry_same_revision_after_backoff(self):
        for failure in [ProviderRequestError('http', 500), None]:
            self.reply = failure
            self.scan(); self.await_idle()
            attempts = len(self.calls)
            self.assertEqual(self.reader.completed_replies(), {})
            self.now += 12; self.scan(); self.await_idle()
            self.assertEqual(len(self.calls), attempts)
            self.now += 19
            self.reply = parse_last_reply(response_fixture(), IDENTIFIER, now=self.now)
            self.scan(); self.await_idle()
            self.assertEqual(len(self.calls), attempts + 1)
            self.assertIn(IDENTIFIER, self.reader.completed_replies())
            # Begin another eligible revision for the second case.
            with self.reader._lock:
                self.reader._reply_revisions.clear(); self.reader._reply_next_poll.clear()

    def test_new_observed_stream_forces_same_revision_marker_refresh(self):
        self.scan(); self.await_idle()
        self.reader.ack_completed_replies(self.reader.completed_replies())
        self.status = 'IS_STREAMING'; self.now += 13
        self.scan(); self.await_idle()
        self.assertEqual(self.reader.completed_replies(), {})
        self.status = 'COMPLETE'; self.now += 5
        self.scan(); self.await_idle()
        self.assertEqual(len(self.calls), 2)

    def test_changed_account_cannot_publish_inflight_old_account_result(self):
        gate = threading.Event(); started = threading.Event()
        def blocked(*args):
            started.set(); gate.wait(2)
            return self.reply
        self.reader._reply_request = blocked
        self.scan(); self.assertTrue(started.wait(1))
        self.write_auth('new-token', 'new-account')
        self.reader.scan([])
        gate.set(); self.await_idle()
        self.assertEqual(self.reader.completed_replies(), {})
        self.assertEqual(self.reader.account_identity(), hashlib.sha256(b'new-account').hexdigest())

    def test_close_discards_inflight_reply(self):
        gate = threading.Event(); started = threading.Event()
        def blocked(*args):
            started.set(); gate.wait(2)
            return self.reply
        self.reader._reply_request = blocked
        self.scan(); self.assertTrue(started.wait(1)); self.reader.close()
        gate.set(); self.await_idle()
        self.assertEqual(self.reader.completed_replies(), {})


if __name__ == '__main__':
    unittest.main()
