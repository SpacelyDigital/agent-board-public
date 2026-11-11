from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from board_store import BoardStore
from claude_chat_monitor import ClaudeChatConversationSource
from claude_chat_source import ClaudeChatAccountReader


CHAT_A = '11111111-1111-4111-8111-111111111111'
CHAT_B = '22222222-2222-4222-8222-222222222222'
LEAF_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
LEAF_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
LEAF_C = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
SCOPE_A = 'a' * 64
SCOPE_B = 'b' * 64


class Reader:
    def __init__(self, now):
        self.snapshot = dict(available=True, fresh=True, observed_at=now, account_scope=SCOPE_A, conversations={})
        self.closed = False

    def scan(self, now):
        return deepcopy(self.snapshot)

    def close(self):
        self.closed = True


class ClaudeChatMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = BoardStore(self.root / 'board.sqlite3')
        self.now = self.store.activated_at + 2
        self.reader = Reader(self.now)
        self.path = self.root / 'claude-chat-baseline.json'
        self.source = ClaudeChatConversationSource(self.reader, self.path)

    def tearDown(self):
        self.source.close()
        self.temp.cleanup()

    def row(self, **changes):
        return dict(title='Same title', created_at=self.now - 1000, updated_at=self.now - 100,
                    last_settled_at=self.now - 100, current_leaf_message_uuid=LEAF_A,
                    live_status=None, live_status_started_at=None, needs_input=None,
                    needs_input_kind=None, **changes)

    def set_row(self, ident=CHAT_A, **changes):
        row = self.row()
        row.update(changes)
        self.reader.snapshot['conversations'][ident] = row

    def scan(self, offset=0):
        self.reader.snapshot['observed_at'] = self.now + offset
        return self.source.scan(self.store, self.now + offset)

    def item(self, ident=CHAT_A):
        return self.store.get('claude:desktop_chat:' + ident)

    def restart(self):
        self.source.close()
        self.source = ClaudeChatConversationSource(self.reader, self.path)
        self.store = BoardStore(self.root / 'board.sqlite3')

    def test_initial_history_is_metadata_and_same_titles_remain_distinct(self):
        self.set_row()
        self.set_row(CHAT_B, workspace_session_id='cse_fixture_workspace', project_uuid=LEAF_C)
        self.assertTrue(self.scan()['available'])
        self.assertFalse(self.store.snapshot()['sessions'])
        chats = self.source.open_chats(self.now)
        self.assertEqual(len(chats), 2)
        self.assertTrue(all(row['catalog_only'] and not row['notification_eligible'] for row in chats.values()))
        self.assertEqual(chats['desktop_chat:' + CHAT_B]['open_url'], 'https://claude.ai/chat/' + CHAT_B)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        checkpoint = self.path.read_text()
        self.assertNotIn('Same title', checkpoint)
        self.assertNotIn('workspace', checkpoint)

    def test_stream_settlement_review_restart_and_rename_keep_one_event(self):
        self.set_row(live_status='running', live_status_started_at=self.now - 1, updated_at=self.now - 1)
        self.scan()
        active = self.item()
        self.assertEqual(active['status'], 'cooking')
        self.set_row(last_settled_at=self.now + 2, current_leaf_message_uuid=LEAF_B, updated_at=self.now + 2)
        self.scan(3)
        ready = self.item()
        self.assertEqual(ready['status'], 'ready')
        self.assertEqual(ready['turn_id'], active['turn_id'])
        self.store.review(ready['key'], ready['event_id'])
        self.restart()
        self.set_row(title='Renamed in the other surface', last_settled_at=self.now + 2,
                     current_leaf_message_uuid=LEAF_B, updated_at=self.now + 4)
        self.scan(5)
        after = self.item()
        self.assertEqual(after['title'], 'Renamed in the other surface')
        self.assertEqual(after['event_id'], ready['event_id'])
        self.assertEqual(after['reviewed_event'], ready['event_id'])
        self.assertFalse(after['notification_eligible'])
        self.assertFalse(self.store.snapshot()['sessions'])

    def test_fast_reply_without_observed_running_is_caught_after_baseline(self):
        self.set_row()
        self.scan()
        self.set_row(last_settled_at=self.now + 1, current_leaf_message_uuid=LEAF_B, updated_at=self.now + 1)
        self.scan(2)
        ready = self.item()
        self.assertEqual(ready['status'], 'ready')
        self.assertTrue(ready['notification_eligible'])
        self.scan(3)
        self.assertEqual(self.item()['event_id'], ready['event_id'])

    def test_whole_second_completion_supersedes_fractional_cooking_time(self):
        second = int(self.now) + 1
        self.set_row(live_status='running', live_status_started_at=second,
                     updated_at=second + .8)
        self.scan(second + .9 - self.now)
        active = self.item()
        self.assertEqual(active['status'], 'cooking')
        self.set_row(last_settled_at=second, current_leaf_message_uuid=LEAF_B,
                     updated_at=second + .95)
        self.scan(second + 1.1 - self.now)
        ready = self.item()
        self.assertEqual(ready['status'], 'ready')
        self.assertEqual(ready['updated_at'], active['updated_at'])
        self.assertEqual(ready['turn_id'], active['turn_id'])
        self.assertNotEqual(ready['event_id'], active['event_id'])
        self.store.review(ready['key'], ready['event_id'])
        self.restart()
        self.scan(second + 2 - self.now)
        self.assertEqual(self.item()['event_id'], ready['event_id'])
        self.assertFalse(self.item()['notification_eligible'])

    def test_fresh_same_second_resume_clears_attention_without_rewinding_time(self):
        second = int(self.now) + 1
        self.set_row(live_status='running', live_status_started_at=second,
                     updated_at=second + .8, needs_input=True, needs_input_kind='permission')
        self.scan(second + .9 - self.now)
        attention = self.item()
        self.assertEqual(attention['status'], 'needs_you')
        # The provider confirms the same live turn resumed; its coarse start
        # and stale message time cannot keep the resolved permission on screen.
        self.set_row(live_status='running', live_status_started_at=second,
                     updated_at=second + .2, needs_input=False)
        self.scan(second + 1 - self.now)
        active = self.item()
        self.assertEqual(active['status'], 'cooking')
        self.assertEqual(active['updated_at'], attention['updated_at'])
        self.assertFalse(active['notification_eligible'])
        self.set_row(last_settled_at=second, current_leaf_message_uuid=LEAF_B,
                     updated_at=second + .95)
        self.scan(second + 1.1 - self.now)
        self.assertEqual(self.item()['status'], 'ready')
        self.set_row(live_status='running', live_status_started_at=second,
                     last_settled_at=second, current_leaf_message_uuid=LEAF_B,
                     updated_at=second + .3)
        self.scan(second + 1.2 - self.now)
        self.assertEqual(self.item()['status'], 'cooking')
        self.assertEqual(self.item()['updated_at'], attention['updated_at'])
        self.assertFalse(self.item()['notification_eligible'])

    def test_timestamp_clamp_cannot_override_other_account_or_newer_receipt(self):
        second = int(self.now) + 1
        self.set_row(live_status='running', live_status_started_at=second,
                     updated_at=second + .8, needs_input=True)
        self.scan(second + .9 - self.now)
        prior = self.item()
        self.reader.snapshot['account_scope'] = SCOPE_B
        self.set_row(live_status='running', live_status_started_at=second,
                     updated_at=second + .2)
        self.scan(second + 1 - self.now)
        self.assertEqual(self.item()['event_id'], prior['event_id'])
        self.assertEqual(self.item()['status'], 'needs_you')
        self.reader.snapshot['account_scope'] = SCOPE_A
        self.reader.snapshot['observed_at'] = second + .85
        self.source.scan(self.store, second + 1.1)
        self.assertEqual(self.item()['event_id'], prior['event_id'])
        self.assertEqual(self.item()['status'], 'needs_you')

    def test_new_browser_chat_completed_between_polls_is_caught_without_desktop_index(self):
        self.scan()
        self.set_row(CHAT_B, created_at=self.now + 1, last_settled_at=self.now + 2,
                     current_leaf_message_uuid=LEAF_B, updated_at=self.now + 2)
        self.scan(3)
        self.assertEqual(self.item(CHAT_B)['status'], 'ready')
        self.assertEqual(len(self.store.snapshot()['sessions']), 1)

    def test_first_reply_in_new_stream_with_no_previous_leaf_can_complete(self):
        self.set_row(created_at=self.now, last_settled_at=None, current_leaf_message_uuid=None,
                     live_status='running', live_status_started_at=self.now, updated_at=self.now)
        self.scan()
        self.set_row(created_at=self.now, last_settled_at=self.now + 2,
                     current_leaf_message_uuid=LEAF_A, updated_at=self.now + 2)
        self.scan(3)
        self.assertEqual(self.item()['status'], 'ready')

    def test_title_update_single_marker_or_artifact_change_is_not_completion(self):
        self.set_row()
        self.scan()
        changes = [dict(title='Changed title', updated_at=self.now + 1),
                   dict(current_leaf_message_uuid=LEAF_B),
                   dict(last_settled_at=self.now + 2),
                   dict(latest_assistant_output_at=self.now + 3, latest_assistant_output_kind='artifact',
                        last_settled_at=self.now - 100, current_leaf_message_uuid=LEAF_A)]
        for offset, fields in enumerate(changes, 1):
            self.set_row(**fields)
            self.scan(offset)
            self.assertIsNone(self.item())

    def test_outage_and_unknown_state_do_not_complete_then_fresh_settlement_recovers(self):
        self.set_row(live_status='running', live_status_started_at=self.now)
        self.scan()
        for offset, options in [(1, dict(available=False)), (2, dict(fresh=False))]:
            self.reader.snapshot.update(options)
            self.set_row(last_settled_at=self.now + offset, current_leaf_message_uuid=LEAF_B)
            self.assertFalse(self.scan(offset)['available'])
            self.assertEqual(self.item()['status'], 'cooking')
            self.assertFalse(self.source.cooking)
            self.reader.snapshot.update(available=True, fresh=True)
        self.set_row(live_status='future-state', last_settled_at=self.now + 3, current_leaf_message_uuid=LEAF_B)
        self.scan(3)
        self.assertEqual(self.item()['status'], 'cooking')
        self.set_row(last_settled_at=self.now + 3, current_leaf_message_uuid=LEAF_B)
        self.scan(5)
        self.assertEqual(self.item()['status'], 'ready')

    def test_partial_marker_update_does_not_consume_later_confirmed_pair(self):
        self.set_row()
        self.scan()
        self.set_row(current_leaf_message_uuid=LEAF_B)
        self.scan(1)
        self.assertIsNone(self.item())
        self.set_row(current_leaf_message_uuid=LEAF_B, last_settled_at=self.now + 2)
        self.scan(3)
        self.assertEqual(self.item()['status'], 'ready')

    def test_restart_catches_reply_while_closed_without_replaying_review(self):
        self.set_row()
        self.scan()
        self.restart()
        self.set_row(last_settled_at=self.now + 2, current_leaf_message_uuid=LEAF_B)
        self.scan(3)
        ready = self.item()
        self.assertEqual(ready['status'], 'ready')
        self.store.review(ready['key'], ready['event_id'])
        self.restart()
        self.scan(4)
        self.assertEqual(self.item()['reviewed_event'], ready['event_id'])

    def test_account_switch_resets_baseline_and_cannot_finish_old_pending_turn(self):
        self.set_row(live_status='running', live_status_started_at=self.now)
        self.scan()
        old = self.item()
        self.reader.snapshot['account_scope'] = SCOPE_B
        self.set_row(last_settled_at=self.now + 2, current_leaf_message_uuid=LEAF_B)
        self.scan(3)
        self.assertEqual(self.item()['event_id'], old['event_id'])
        self.assertFalse(self.source.cooking)
        self.restart()
        self.scan(4)
        self.assertEqual(self.item()['status'], 'cooking')
        self.set_row(last_settled_at=self.now + 5, current_leaf_message_uuid=LEAF_C)
        self.scan(6)
        self.assertEqual(self.item()['status'], 'ready')
        self.assertNotEqual(self.item()['turn_id'], old['turn_id'])

    def test_positive_needs_input_is_distinct_from_running(self):
        self.set_row(live_status='running', live_status_started_at=self.now,
                     needs_input=True, needs_input_kind='permission')
        self.scan()
        item = self.item()
        self.assertEqual(item['status'], 'needs_you')
        self.assertFalse(self.source.cooking)
        self.store.review(item['key'], item['event_id'])
        self.scan(1)
        self.assertFalse(self.item()['notification_eligible'])

    def test_checkpoint_failure_cannot_lose_completion_or_replay_review(self):
        self.set_row()
        self.scan()
        self.set_row(last_settled_at=self.now + 2, current_leaf_message_uuid=LEAF_B)
        with patch.object(self.source.checkpoint, 'save', side_effect=OSError('fixture')):
            self.assertFalse(self.scan(3)['available'])
        ready = self.item()
        self.assertEqual(ready['status'], 'ready')
        self.store.review(ready['key'], ready['event_id'])
        self.restart()
        self.scan(4)
        self.assertEqual(self.item()['event_id'], ready['event_id'])
        self.assertFalse(self.item()['notification_eligible'])

    def test_transient_reader_and_checkpoint_failures_recover_without_restart(self):
        self.set_row()
        self.scan()
        for offset, problem in [(1, OSError('fixture')), (2, ValueError('fixture')), (3, RuntimeError('fixture'))]:
            with patch.object(self.reader, 'scan', side_effect=problem):
                self.assertFalse(self.scan(offset)['available'])
            self.assertFalse(self.source._checkpoint_error)
        self.set_row(last_settled_at=self.now + 4, current_leaf_message_uuid=LEAF_B)
        with patch.object(self.source.checkpoint, 'save', side_effect=OSError('fixture')):
            self.assertFalse(self.scan(5)['available'])
        ready = self.item()
        self.store.review(ready['key'], ready['event_id'])
        self.assertTrue(self.scan(6)['available'])
        self.assertEqual(self.item()['event_id'], ready['event_id'])
        self.assertFalse(self.item()['notification_eligible'])

    def test_malformed_reader_snapshot_recovers_without_poisoning_checkpoint(self):
        self.set_row()
        self.scan()
        for value in [None, [], {'available': True, 'fresh': True, 'observed_at': 10**400}]:
            with patch.object(self.reader, 'scan', return_value=value):
                self.assertFalse(self.scan(1)['available'])
            self.assertFalse(self.source._checkpoint_error)
        self.assertTrue(self.scan(2)['available'])

    def test_real_async_reader_cookie_refresh_keeps_fast_reply_baseline(self):
        clock = [self.now]
        revision = ['first-cookie']
        rows = {CHAT_A: self.row()}
        reader = ClaudeChatAccountReader(fetcher=lambda: {'account_scope': SCOPE_A, 'conversations': deepcopy(rows)},
                                        revision_reader=lambda: revision[0], clock=lambda: clock[0])
        self.source.close()
        self.source = ClaudeChatConversationSource(reader, self.path)
        def wait_for(condition):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                self.source.scan(self.store, clock[0])
                if condition():
                    return
                time.sleep(.005)
            self.fail('The asynchronous account reader did not reach the expected state')
        wait_for(lambda: self.source.available)
        self.assertIsNone(self.item())
        revision[0] = 'refreshed-cookie'
        clock[0] += 2
        rows[CHAT_A].update(last_settled_at=clock[0], current_leaf_message_uuid=LEAF_B, updated_at=clock[0])
        wait_for(lambda: self.item() is not None)
        self.assertEqual(self.item()['status'], 'ready')
        self.assertEqual(len(self.store.snapshot()['sessions']), 1)

    def test_symlink_malformed_or_public_checkpoint_fails_closed_without_overwrite(self):
        self.source.close()
        outside = self.root / 'owner-file'
        outside.write_text('Owner data')
        self.path.symlink_to(outside)
        self.source = ClaudeChatConversationSource(self.reader, self.path)
        self.assertFalse(self.scan()['available'])
        self.assertEqual(outside.read_text(), 'Owner data')
        self.source.close()
        self.path.unlink()
        self.path.write_text('{"message":"must not be copied"}')
        self.path.chmod(0o600)
        self.source = ClaudeChatConversationSource(self.reader, self.path)
        self.assertFalse(self.scan()['available'])
        self.assertEqual(json.loads(self.path.read_text()), {'message': 'must not be copied'})
        self.source.close()
        self.path.chmod(0o644)
        self.source = ClaudeChatConversationSource(self.reader, self.path)
        self.assertFalse(self.scan()['available'])


if __name__ == '__main__':
    unittest.main()
