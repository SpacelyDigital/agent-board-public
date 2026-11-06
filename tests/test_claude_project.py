import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from board_store import BoardStore
from claude_project_source import (ClaudeProjectSource, ProjectUnavailable, _Client,
                                   _collect, _session_token, _thread_preview)


CHANNEL = "chan_12345678"
SESSION = "cse_12345678"
THREAD = "cmsg_12345678"
IDENT = "project:" + CHANNEL
TASK = IDENT + ":thread:" + THREAD
NOW = 1793307000.0


class FakeClient:
    def __init__(self, status="blocked", unread=True, thread=None, messages=None):
        self.channel = {"id": CHANNEL, "name": "Test project", "channel_session_id": SESSION,
                        "has_unread": unread, "created_at": NOW - 100000}
        self.session = {"id": SESSION, "status_bucket": status, "unread": unread,
                        "last_event_at": NOW - 30, "updated_at": NOW}
        self.thread = thread
        self.messages = messages or []
        self.calls = []
        self.fail = set()

    def get(self, path):
        self.calls.append(path)
        if path in self.fail:
            raise ProjectUnavailable("Disposable read failure.")
        if path == "/v1/code/channels?scope=all":
            return {"data": [self.channel]}
        if path == "/v1/code/sessions/" + SESSION:
            return {"response_shape": self.session}
        if "/timeline?" in path:
            return {"data": ([self.thread] if self.thread else []) + self.messages}
        if "/threads/" in path:
            return {"data": ([self.thread] if self.thread else []) + self.messages, "next_cursor": ""}
        raise AssertionError(path)


def root(**fields):
    return {"id": THREAD, "has_thread": True, "thread_status": "needs_input",
            "thread_any_session_working": False, "thread_waits_on_live_prompt": False,
            "has_unread": True, "created_at": NOW - 10000, "last_reply_at": NOW - 10,
            "reply_count": "5", "thread_preview": "Choose the final layout", **fields}


def reply(**fields):
    return {"id": "cmsg_reply123456", "author_agent_id": "cagt_12345678",
            "author_session_id": SESSION, "created_at": NOW - 10,
            "body": "Which layout should I use?", **fields}


class ClaudeProjectTests(unittest.TestCase):
    def collect(self, client, tracked=(), previous=None):
        rows, health = _collect(NOW, NOW - 3600, set(tracked), previous or {}, client)
        return {row["id"]: row for row in rows}, health

    def test_coordinator_uses_own_status_while_child_is_done(self):
        client = FakeClient(thread=root(bound_sessions=[{"id": "child", "status_bucket": "completed"}]))
        rows, health = self.collect(client)
        self.assertTrue(health["available"])
        self.assertEqual(rows[IDENT]["status"], "needs_you")
        self.assertEqual(rows[TASK]["status"], "needs_you")

    def test_current_task_replaces_overview_in_main_queue_without_changing_status(self):
        client = FakeClient(status="working", thread=root(thread_any_session_working=True))
        rows, _ = self.collect(client)
        self.assertEqual(rows[IDENT]["status"], "cooking")
        self.assertEqual(rows[IDENT]["background_reason"], "project_overview")
        self.assertEqual(rows[IDENT]["project_channel_id"], CHANNEL)
        self.assertEqual(rows[IDENT]["project_role"], "overview")
        self.assertEqual(rows[TASK]["project_role"], "thread")
        self.assertEqual(rows[TASK]["project_thread_id"], THREAD)
        self.assertNotIn("background_reason", rows[TASK])

    def test_different_overview_status_does_not_duplicate_current_task(self):
        client = FakeClient(status="blocked", thread=root(thread_any_session_working=True))
        rows, _ = self.collect(client)
        self.assertEqual(rows[IDENT]["status"], "needs_you")
        self.assertEqual(rows[IDENT]["background_reason"], "project_overview")
        self.assertEqual(rows[TASK]["status"], "cooking")

    def test_tracked_ready_task_with_no_native_unread_still_takes_precedence(self):
        client = FakeClient(thread=root(thread_status="resolved", has_unread=False))
        rows, _ = self.collect(client, (TASK,))
        self.assertFalse(rows[TASK]["discovery_eligible"])
        self.assertEqual(rows[IDENT]["background_reason"], "project_overview")

    def test_only_old_background_or_idle_tasks_leave_coordinator_on_board(self):
        for task in (None, root(last_reply_at=NOW - 100000),
                     root(thread_status="idle", has_unread=False)):
            with self.subTest(task=task):
                rows, _ = self.collect(FakeClient(thread=task), (TASK,))
                self.assertNotIn("background_reason", rows[IDENT])

    def test_coordinator_live_permission_is_independent_of_active_task(self):
        client = FakeClient(thread=root(thread_any_session_working=True))
        client.session["requires_action_details_list"] = [{"type": "tool_permission", "session_id": SESSION}]
        rows, _ = self.collect(client)
        self.assertNotIn("background_reason", rows[IDENT])
        self.assertEqual(rows[IDENT]["status"], "needs_you")
        self.assertEqual(rows[TASK]["status"], "cooking")

    def test_child_or_unknown_permission_scope_does_not_duplicate_thread_attention(self):
        for detail, bound in (({"request_id": "request-1"}, []),
                              ({"session_id": "cse_child123456"}, [{"id": "cse_child123456"}]),
                              ({"session_id": SESSION, "thread_id": THREAD}, []),
                              ({"session_id": SESSION}, [{"id": SESSION}])):
            with self.subTest(detail=detail, bound=bound):
                client = FakeClient(thread=root(bound_sessions=bound))
                client.session["requires_action_details_list"] = [detail]
                rows, _ = self.collect(client)
                self.assertEqual(rows[IDENT]["background_reason"], "project_overview")
                self.assertEqual(rows[TASK]["status"], "needs_you")

    def test_live_permission_identity_changes_revision_without_resetting_normal_events(self):
        client = FakeClient(thread=root(thread_any_session_working=True))
        initial, _ = self.collect(client)
        client.session["requires_action_details_list"] = []
        empty, _ = self.collect(client)
        self.assertEqual(initial[IDENT]["event_id"], empty[IDENT]["event_id"])
        with tempfile.TemporaryDirectory() as folder:
            store = BoardStore(Path(folder) / "board.sqlite3")
            store.apply(initial.values(), NOW)
            overview = store.get("claude:" + IDENT)
            store.review(overview["key"], overview["event_id"])
            # The native status and last_event_at need not change when an
            # actual new permission request replaces an already reviewed wait.
            client.session["requires_action_details_list"] = [{"request_id": "permission-1", "session_id": SESSION,
                                                               "label": "Allow check"}]
            prompt, _ = self.collect(client)
            store.apply(prompt.values(), NOW + 1)
            fresh = store.get(overview["key"])
            self.assertNotEqual(initial[IDENT]["event_id"], prompt[IDENT]["event_id"])
            self.assertTrue(fresh["notification_eligible"])
            self.assertFalse(fresh.get("background_reason"))
            client.session["requires_action_details_list"][0]["label"] = "Updated wording"
            unchanged, _ = self.collect(client)
            self.assertEqual(prompt[IDENT]["event_id"], unchanged[IDENT]["event_id"])
            client.session["requires_action_details_list"][0]["request_id"] = "permission-2"
            newer, _ = self.collect(client)
            self.assertNotEqual(prompt[IDENT]["event_id"], newer[IDENT]["event_id"])

    def test_partial_reads_preserve_known_background_route_after_restart_until_healthy_recovery(self):
        client = FakeClient(thread=root(thread_any_session_working=True))
        rows, _ = self.collect(client)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "board.sqlite3"
            store = BoardStore(path)
            store.apply(rows.values(), NOW)
            client.fail.update({"/v1/code/channels/" + CHANNEL + "/timeline?limit=100",
                                "/v1/code/channels/" + CHANNEL + "/threads/" + THREAD + "?limit=1"})
            # A source cache may be empty after a service restart. The explicit
            # uncertainty signal preserves the board's stored routing anyway.
            partial, health = self.collect(client, (IDENT, TASK))
            self.assertFalse(health["available"])
            self.assertTrue(partial[IDENT]["background_routing_uncertain"])
            restarted = BoardStore(path)
            restarted.apply(partial.values(), NOW + 20)
            overview = restarted.get("claude:" + IDENT)
            self.assertEqual(overview["background_reason"], "project_overview")
            self.assertFalse(overview["notification_eligible"])
            self.assertEqual(restarted.snapshot()["counts"]["needs_you"], 0)
            client.fail.clear()
            client.thread = root(thread_status="idle", has_unread=False)
            recovered, health = self.collect(client, (IDENT, TASK))
            self.assertTrue(health["available"])
            self.assertNotIn("background_routing_uncertain", recovered[IDENT])
            restarted.apply(recovered.values(), NOW + 40)
            self.assertFalse(restarted.get("claude:" + IDENT).get("background_reason"))
            self.assertEqual(restarted.snapshot()["counts"]["needs_you"], 1)

    def test_proven_own_permission_or_newer_known_reply_remains_visible_during_partial_reads(self):
        client = FakeClient(thread=root(thread_any_session_working=True))
        rows, _ = self.collect(client)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "board.sqlite3"
            store = BoardStore(path)
            store.apply(rows.values(), NOW)
            client.fail.update({"/v1/code/channels/" + CHANNEL + "/timeline?limit=100",
                                "/v1/code/channels/" + CHANNEL + "/threads/" + THREAD + "?limit=1"})
            client.session["requires_action_details_list"] = [{"request_id": "own-permission", "session_id": SESSION}]
            partial, health = self.collect(client, (IDENT, TASK), rows)
            self.assertFalse(health["available"])
            self.assertNotIn("background_routing_uncertain", partial[IDENT])
            store.apply(partial.values(), NOW + 20)
            self.assertFalse(store.get("claude:" + IDENT).get("background_reason"))
            self.assertEqual(store.snapshot()["counts"]["needs_you"], 1)
            # A healthy timeline can also establish an actual independent reply
            # relative to the last known task activity while its root read fails.
            client.session["requires_action_details_list"] = []
            client.fail.remove("/v1/code/channels/" + CHANNEL + "/timeline?limit=100")
            client.thread = None
            client.messages = [reply(created_at=NOW, thread_root_id="cmsg_reply123456")]
            partial, health = self.collect(client, (IDENT, TASK), rows)
            self.assertFalse(health["available"])
            self.assertNotIn("background_routing_uncertain", partial[IDENT])

    def test_newer_top_level_coordinator_reply_is_independent(self):
        client = FakeClient(thread=root(), messages=[reply(created_at=NOW, thread_root_id="cmsg_reply123456")])
        rows, _ = self.collect(client)
        self.assertNotIn("background_reason", rows[IDENT])
        # A heartbeat after a stale reply is not a separate request for input.
        client.messages[0]["created_at"] = NOW - 20
        client.session["last_event_at"] = NOW + 10
        rows, _ = self.collect(client)
        self.assertEqual(rows[IDENT]["background_reason"], "project_overview")
        # A reply attached to this task is not an independent top-level reply.
        client.messages[0].update(created_at=NOW, thread_root_id=THREAD)
        rows, _ = self.collect(client)
        self.assertEqual(rows[IDENT]["background_reason"], "project_overview")

    def test_newer_top_level_user_input_can_start_independent_coordinator_work(self):
        message = {"id": "cmsg_input123456", "author_account_id": "user_12345678",
                   "created_at": NOW, "body": "Check the project settings", "has_thread": False}
        client = FakeClient(status="working", thread=root(thread_any_session_working=True), messages=[message])
        rows, _ = self.collect(client)
        self.assertNotIn("background_reason", rows[IDENT])
        self.assertEqual(rows[IDENT]["status"], "cooking")
        message["thread_root_id"] = THREAD
        rows, _ = self.collect(client)
        self.assertEqual(rows[IDENT]["background_reason"], "project_overview")

    def test_multiple_task_roots_are_kept_individually_with_one_background_overview(self):
        client = FakeClient(thread=root(thread_any_session_working=True))
        second = root(id="cmsg_second123456", thread_any_session_working=True)
        client.messages = [second]
        rows, _ = self.collect(client)
        self.assertEqual(rows[IDENT]["background_reason"], "project_overview")
        self.assertEqual({r["project_thread_id"] for r in rows.values() if r.get("project_role") == "thread"},
                         {THREAD, second["id"]})
        self.assertTrue(all("background_reason" not in r for r in rows.values() if r.get("project_role") == "thread"))

    def test_overlap_is_scoped_to_native_channel_id_not_title(self):
        client = FakeClient(thread=root(thread_any_session_working=True))
        other_channel = dict(client.channel, id="chan_other123456", channel_session_id="cse_other123456")
        original_get = client.get
        def get(path):
            if path == "/v1/code/channels?scope=all":
                return {"data": [client.channel, other_channel]}
            if path == "/v1/code/sessions/cse_other123456":
                return {"response_shape": dict(client.session, id="cse_other123456")}
            if path.startswith("/v1/code/channels/chan_other123456/"):
                return {"data": []}
            return original_get(path)
        client.get = get
        rows, _ = self.collect(client)
        self.assertEqual(rows[IDENT]["background_reason"], "project_overview")
        self.assertNotIn("background_reason", rows["project:chan_other123456"])

    def test_replying_to_overview_replaces_stale_attention_with_current_task(self):
        client = FakeClient(status="blocked", messages=[reply(created_at=NOW - 100)])
        rows, _ = self.collect(client)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "board.sqlite3"
            store = BoardStore(path)
            store.apply(rows.values(), NOW)
            before = store.snapshot()
            self.assertEqual(before["counts"]["needs_you"], 1)
            self.assertTrue(before["sessions"][0]["notification_eligible"])
            # Reading/opening a row alone never acknowledges or changes it.
            store.get("claude:" + IDENT)
            self.assertEqual(store.snapshot()["counts"]["needs_you"], 1)
            # A native response starts the real task. The overview may retain
            # blocked and receive a newer aggregate timestamp during that turn.
            client.thread = root(thread_any_session_working=True, thread_status="working")
            client.session["last_event_at"] = NOW + 10
            rows, _ = self.collect(client, (IDENT,))
            store.apply(rows.values(), NOW + 10)
            after = BoardStore(path).snapshot()
            self.assertEqual([r["id"] for r in after["sessions"]], [TASK])
            self.assertEqual(after["counts"]["cooking"], 1)
            self.assertEqual(after["counts"]["needs_you"], 0)
            self.assertFalse(any(r["notification_eligible"] for r in after["sessions"]))
            self.assertEqual(after["background"][0]["id"], IDENT)
            self.assertEqual(after["background"][0]["status"], "needs_you")
            self.assertFalse(after["background"][0]["notification_eligible"])

    def test_overlap_counts_alerts_reviews_snooze_and_manual_placement_survive_restart(self):
        client = FakeClient(thread=root(thread_status="resolved"))
        rows, _ = self.collect(client)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "board.sqlite3"
            store = BoardStore(path)
            store.apply(rows.values(), NOW)
            snapshot = store.snapshot()
            self.assertEqual([r["id"] for r in snapshot["sessions"]], [TASK])
            self.assertEqual(snapshot["counts"]["ready"], 1)
            self.assertEqual(sum(snapshot["counts"].values()), 1)
            self.assertEqual([r["id"] for r in snapshot["background"]], [IDENT])
            self.assertFalse(store.get("claude:" + IDENT)["notification_eligible"])
            task = store.get("claude:" + TASK)
            store.snooze(task["key"], task["event_id"])
            store.review(task["key"], task["event_id"])
            client.thread["has_unread"] = False
            rows, _ = self.collect(client, (TASK,))
            store.apply(rows.values(), NOW + 1)
            restarted = BoardStore(path)
            snapshot = restarted.snapshot()
            self.assertFalse(snapshot["sessions"], "Reviewing a task must not reveal its stale project overview")
            self.assertEqual([r["id"] for r in snapshot["history"]], [TASK])
            self.assertEqual(sum(snapshot["counts"].values()), 0)
            self.assertGreater(restarted.get(task["key"])["snoozed_until"], 0)
            restarted.move("claude:" + IDENT, "board")
            manual = restarted.snapshot()["sessions"][0]
            self.assertEqual(manual["id"], IDENT)
            self.assertEqual(manual["source_background_reason"], "project_overview")
            self.assertTrue(manual["notification_eligible"])
            restarted.move("claude:" + IDENT, "auto")
            self.assertFalse(restarted.snapshot()["sessions"])
            restarted.restore(task["key"])
            self.assertEqual([r["id"] for r in restarted.snapshot()["sessions"]], [TASK])
            self.assertFalse(restarted.get(task["key"])["notification_eligible"], "Snooze remains attached to the task revision")

    def test_exact_task_caption_link_and_identity(self):
        rows, _ = self.collect(FakeClient(thread=root()))
        task = rows[TASK]
        self.assertEqual(task["title"], "Test project · Choose the final layout")
        self.assertEqual(task["open_url"], "claude://code/project/" + CHANNEL + "?thread=" + THREAD)
        self.assertEqual(task["key"], "claude:" + TASK)
        self.assertEqual(task["source_kind"], "project")

    def test_native_task_title_matches_desktop_without_using_its_status(self):
        rows, _ = self.collect(FakeClient(thread=root(thread_status="idle", bound_sessions=[{
            "title": "Check keyboard navigation", "status_bucket": "completed"}])), [TASK])
        self.assertEqual(rows[TASK]["title"], "Test project · Check keyboard navigation")
        self.assertEqual(rows[TASK]["status"], "disconnected")

    def test_idle_unread_thread_does_not_imply_completion(self):
        client = FakeClient(thread=root(thread_status="idle", has_unread=True))
        rows, _ = self.collect(client)
        self.assertNotIn(TASK, rows)
        rows, _ = self.collect(client, [TASK])
        self.assertEqual(rows[TASK]["status"], "disconnected")
        self.assertFalse(rows[TASK]["discovery_eligible"])

    def test_thread_working_is_explicit_even_if_coordinator_completed(self):
        rows, _ = self.collect(FakeClient(status="completed", thread=root(thread_status="idle", thread_any_session_working=True)))
        self.assertEqual(rows[IDENT]["status"], "ready")
        self.assertEqual(rows[TASK]["status"], "cooking")

    def test_old_resolved_unread_thread_is_pending_review(self):
        rows, _ = self.collect(FakeClient(thread=root(thread_status="resolved", last_reply_at=NOW - 100000)))
        self.assertEqual(rows[TASK]["status"], "ready")
        self.assertTrue(rows[TASK]["pending_review"])
        self.assertTrue(rows[TASK]["discovery_eligible"])
        self.assertEqual(rows[TASK]["background_reason"], "older_project_thread")
        self.assertEqual(rows[TASK]["updated_at"], NOW - 100000)

    def test_old_needs_input_thread_is_background_without_live_prompt(self):
        rows, _ = self.collect(FakeClient(thread=root(last_reply_at=NOW - 100000)))
        self.assertEqual(rows[TASK]["status"], "needs_you")
        self.assertEqual(rows[TASK]["background_reason"], "older_project_thread")
        self.assertTrue(rows[TASK]["discovery_eligible"])

    def test_old_read_idle_thread_remains_accessible_in_background(self):
        rows, _ = self.collect(FakeClient(thread=root(thread_status="idle", has_unread=False,
                                                       last_reply_at=NOW - 100000)))
        self.assertEqual(rows[TASK]["status"], "disconnected")
        self.assertEqual(rows[TASK]["background_reason"], "older_project_thread")
        self.assertTrue(rows[TASK]["discovery_eligible"])
        self.assertFalse(rows[TASK]["pending_review"])

    def test_old_working_or_live_prompt_threads_remain_on_board(self):
        for fields in ({"thread_any_session_working": True}, {"thread_waits_on_live_prompt": True}):
            with self.subTest(fields=fields):
                rows, _ = self.collect(FakeClient(thread=root(last_reply_at=NOW - 100000, **fields)))
                self.assertNotIn("background_reason", rows[TASK])
                self.assertTrue(rows[TASK]["discovery_eligible"])

    def test_older_thread_classification_survives_tracking_and_new_source(self):
        client = FakeClient(thread=root(last_reply_at=NOW - 100000))
        for tracked in ((), (TASK,)):
            rows, _ = self.collect(client, tracked)
            self.assertEqual(rows[TASK]["background_reason"], "older_project_thread")

    def test_real_activity_after_activation_clears_background(self):
        client = FakeClient(thread=root(last_reply_at=NOW - 100000))
        older, _ = self.collect(client)
        client.thread["last_reply_at"] = NOW - 5
        newer, _ = self.collect(client, (TASK,), older)
        self.assertNotIn("background_reason", newer[TASK])
        self.assertTrue(newer[TASK]["discovery_eligible"])

    def test_cutoff_is_board_activation_not_fixed_recent_age(self):
        client = FakeClient(thread=root(last_reply_at=NOW - 10000))
        rows, _ = _collect(NOW, NOW - 20000, set(), {}, client)
        task = next(row for row in rows if row["id"] == TASK)
        self.assertNotIn("background_reason", task)
        rows, _ = _collect(NOW, NOW - 5000, set(), {}, client)
        task = next(row for row in rows if row["id"] == TASK)
        self.assertEqual(task["background_reason"], "older_project_thread")

    def test_old_read_ready_coordinator_is_not_new_discovery(self):
        rows, _ = self.collect(FakeClient(status="review_ready", unread=False))
        self.assertEqual(rows[IDENT]["status"], "ready")
        self.assertFalse(rows[IDENT]["pending_review"])
        self.assertFalse(rows[IDENT]["discovery_eligible"])

    def test_heartbeat_and_poll_time_do_not_change_lifecycle(self):
        client = FakeClient(thread=root())
        first, _ = self.collect(client)
        client.session["updated_at"] = NOW + 500
        second, _ = _collect(NOW + 500, NOW, set(), {}, client)
        second = {row["id"]: row for row in second}
        for ident in (IDENT, TASK):
            self.assertEqual(first[ident]["updated_at"], second[ident]["updated_at"])
            self.assertEqual(first[ident]["event_id"], second[ident]["event_id"])

    def test_original_reply_preview_and_question_are_literal(self):
        message = reply(body="Please choose A or B.\nBoth versions are ready.")
        rows, _ = self.collect(FakeClient(thread=root(), messages=[message]))
        self.assertEqual(rows[TASK]["preview"], message["body"])
        self.assertEqual(rows[TASK]["preview_kind"], "reply")
        self.assertEqual(rows[TASK]["preview_at"], NOW - 10)

    def test_generated_summary_and_tool_or_notice_are_not_previews(self):
        client = FakeClient(thread=root(thread_preview="Native task caption"), messages=[reply(server_notice=True)])
        client.session["post_turn_summary"] = {"description": "A generated summary"}
        rows, _ = self.collect(client)
        self.assertNotIn("preview", rows[TASK])
        self.assertNotIn("preview", rows[IDENT])

    def test_user_after_assistant_prevents_old_reply_preview(self):
        messages = [reply(), {"id": "cmsg_user123456", "author_account_id": "account", "created_at": NOW, "body": "One more change"}]
        rows, _ = self.collect(FakeClient(thread=root(), messages=messages))
        self.assertNotIn("preview", rows[TASK])

    def test_server_notice_after_final_reply_does_not_replace_handoff(self):
        messages = [reply(), reply(id="cmsg_notice123456", created_at=NOW, server_notice=True, body="System notice")]
        rows, _ = self.collect(FakeClient(thread=root(), messages=messages))
        self.assertEqual(rows[TASK]["preview"], "Which layout should I use?")

    def test_unchanged_event_reuses_bounded_preview_without_thread_read(self):
        client = FakeClient(thread=root(), messages=[reply()])
        first, _ = self.collect(client)
        client.calls.clear()
        second, _ = self.collect(client, previous=first)
        self.assertEqual(second[TASK]["preview"], first[TASK]["preview"])
        self.assertFalse(any("/threads/" in path for path in client.calls))

    def test_missing_coordinator_is_partial_health_not_child_ready(self):
        client = FakeClient(thread=root())
        client.fail.add("/v1/code/sessions/" + SESSION)
        rows, health = self.collect(client)
        self.assertNotIn(IDENT, rows)
        self.assertEqual(rows[TASK]["status"], "needs_you")
        self.assertFalse(health["available"])

    def test_tracked_old_thread_is_fetched_beyond_timeline_page(self):
        client = FakeClient(thread=root(thread_status="idle", has_unread=False))
        original_get = client.get
        def get(path):
            if "/timeline?" in path:
                return {"data": []}
            return original_get(path)
        client.get = get
        rows, _ = self.collect(client, [TASK])
        self.assertIn(TASK, rows)
        self.assertFalse(rows[TASK]["discovery_eligible"])

    def test_preview_page_without_end_never_exports_old_reply(self):
        class ManyPages:
            def get(self, path):
                return {"data": [reply()], "next_cursor": "same-cursor"}
        self.assertEqual(_thread_preview(ManyPages(), CHANNEL, root(), "needs_you"), {})

    def test_response_redirect_and_arbitrary_origin_are_rejected(self):
        from privacy_network import ProviderRequestError
        with patch("claude_project_source.read_provider_json", side_effect=ProviderRequestError("redirect")):
            with self.assertRaisesRegex(ProjectUnavailable, "redirected"):
                _Client("test-token").get("/v1/code/channels?scope=all")
        for path in ("https://other.invalid/", "//other.invalid/", "/v1/messages", "/v1/code/sessions/nope"):
            with self.assertRaises(ProjectUnavailable):
                _Client("test-token").get(path)

    def test_source_caches_and_read_failure_marks_health_unavailable(self):
        source = ClaudeProjectSource()
        with patch("claude_project_source._fetch", return_value=([{ "id": IDENT}], {"available": True})) as fetch:
            first = source.scan(NOW, NOW - 3600)
            first[0].clear()
            self.assertEqual(len(source.scan(NOW + 1, 0)[0]), 1)
            self.assertEqual(fetch.call_count, 1)
        with patch("claude_project_source._fetch", side_effect=ProjectUnavailable("Unavailable")):
            rows, health = source.scan(NOW + 20, NOW)
            self.assertEqual(rows, [])
            self.assertFalse(health["available"])

    def test_auth_requires_exact_active_account_org_scope_and_expiry(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "config.json").write_text(json.dumps({"lastKnownAccountUuid": "account-a", "oauth:tokenCacheV2": base64.b64encode(b"v10dummy").decode()}))
            def entry(token, expires=NOW * 1000 + 5000):
                return {"token": token, "expiresAt": expires}
            key = "acct:account-a|region:org-a:https://api.anthropic.com:"
            cache = {key + "user:profile": entry("profile-only"),
                     key + "user:sessions:claude_code": entry("correct"),
                     "acct:account-b|region:org-a:https://api.anthropic.com:user:sessions:claude_code": entry("wrong-account"),
                     "acct:account-a|region:org-b:https://api.anthropic.com:user:sessions:claude_code": entry("wrong-org"),
                     key + "user:sessions:claude_code user:inference": entry("expired", NOW * 1000 - 1)}
            with patch("claude_project_source._keychain_password", return_value=b"test"), patch("claude_project_source._active_claude_org", return_value="org-a"), patch("claude_project_source._decrypt_chromium", return_value=json.dumps(cache).encode()):
                self.assertEqual(_session_token(path, NOW), "correct")
                del cache[key + "user:sessions:claude_code"]
            with patch("claude_project_source._keychain_password", return_value=b"test"), patch("claude_project_source._active_claude_org", return_value="org-a"), patch("claude_project_source._decrypt_chromium", return_value=json.dumps(cache).encode()):
                with self.assertRaises(ProjectUnavailable):
                    _session_token(path, NOW)


if __name__ == "__main__":
    unittest.main()
