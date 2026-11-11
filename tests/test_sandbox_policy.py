"""Exercise sandbox read scope and native handoff with disposable local data."""

import hashlib
import http.client
import json
import os
import plistlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import builtins
import unittest
from unittest.mock import patch

from codex_source import CodexSource
from server import BoardHTTPServer, Handler, Monitor
from source_access import require_source_path
from sandbox_sources import activate_source_bookmarks
import collector_entry


class SourceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "selected"
        self.source.mkdir()
        self.file = self.source / "record.jsonl"
        self.file.write_text("{}\n")
        self.outside = self.root / "private-unselected-canary"
        self.outside.write_text("Never read this from source metadata.")

    def test_selected_regular_file_is_readable_without_mutation(self):
        before = hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.assertEqual(require_source_path(self.file, self.source).read_text(), "{}\n")
        self.assertEqual(before, hashlib.sha256(self.file.read_bytes()).hexdigest())

    def test_escape_symlink_directory_and_fifo_are_rejected(self):
        link = self.source / "substituted.jsonl"
        link.symlink_to(self.outside)
        directory = self.source / "substituted-folder"
        directory.symlink_to(self.root, target_is_directory=True)
        fifo = self.source / "never-block.jsonl"
        os.mkfifo(fifo)
        for path in (self.outside, self.source / ".." / self.outside.name,
                     link, directory / self.outside.name, fifo, self.source):
            with self.subTest(path=path.name), self.assertRaises(ValueError) as failure:
                require_source_path(path, self.source)
            self.assertNotIn(str(self.outside), str(failure.exception))

    def test_database_path_cannot_redirect_collector_to_unselected_file(self):
        now = time.time()
        self.outside.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "scope-fixture", "thread_source": "user", "source": "vscode"}}) + "\n" +
            json.dumps({"type": "event_msg", "timestamp": now, "payload": {
                "type": "task_complete", "turn_id": "never-trust", "completed_at": now}}) + "\n")
        with sqlite3.connect(self.source / "state_5.sqlite") as database:
            database.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, updated_at INTEGER, title TEXT)")
            database.execute("INSERT INTO threads VALUES (?,?,?,?)",
                             ("scope-fixture", str(self.outside), int(now), "Disposable selected index"))
        before = self.outside.read_bytes()
        rows, _ = CodexSource(self.source, confine_paths=True).scan(now, now - 60)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "disconnected")
        self.assertNotIn("never-trust", json.dumps(rows))
        self.assertEqual(before, self.outside.read_bytes())

    def test_symlinked_database_is_not_opened(self):
        (self.source / "state_5.sqlite").symlink_to(self.outside)
        rows, error = CodexSource(self.source, confine_paths=True)._thread_rows(0)
        self.assertEqual(rows, [])
        self.assertEqual(error, "Codex thread database could not be read.")

    def test_selected_index_cannot_read_auth_config_or_unrelated_jsonl(self):
        source = CodexSource(self.source, confine_paths=True)
        for relative in ("auth.json", "config.toml", "private.jsonl", "logs/private.jsonl",
                         "sessions/auth.json", "archived_sessions/config.toml"):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"fixture_private_credential":"never-open-this-file"}')
            with self.subTest(path=relative), patch.object(Path, "open", side_effect=AssertionError("Unauthorized read")):
                state, _, error = source._read(path)
            self.assertIsNone(state)
            self.assertEqual(error, "Codex rollout could not be read.")

    def test_supported_session_paths_remain_readable_and_unchanged(self):
        now = time.time()
        source = CodexSource(self.source, confine_paths=True)
        for folder in ("sessions/fixture", "archived_sessions"):
            path = self.source / folder / "rollout-fixture.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"type": "session_meta", "payload": {
                "id": "scope-fixture", "thread_source": "user"}}) + "\n" +
                json.dumps({"type": "event_msg", "timestamp": now, "payload": {
                    "type": "task_complete", "turn_id": "fixture-ready", "completed_at": now}}) + "\n")
            before = path.read_bytes()
            state, _, error = source._read(path)
            self.assertIsNone(error)
            self.assertEqual(state.status, "ready")
            self.assertEqual(before, path.read_bytes())


class FrozenStoreEntryTests(unittest.TestCase):
    """Exercise the real frozen entry dispatch with isolated signed-metadata fixtures."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.contents = self.root / "Fixture.app/Contents"
        (self.contents / "MacOS").mkdir(parents=True)
        (self.contents / "Resources").mkdir()
        self.executable = self.contents / "MacOS/fixture-collector"
        self.calls = []

    def invoke(self, strategy="hidden", *, sandbox=True, arguments=(), policy=True, policy_version=1, policy_extra=False):
        info = {"AgentBoardStoreDistribution": True}
        if strategy != "MISSING":
            info["AgentBoardStoreSupportStrategy"] = strategy
        if sandbox != "MISSING":
            info["AgentBoardSandboxed"] = sandbox
        (self.contents / "Info.plist").write_bytes(plistlib.dumps(info))
        policy_path = self.contents / "Resources/sandbox-collector-policy.json"
        if policy is not None:
            policy_values = {"version": policy_version, "sandbox_required": policy}
            if policy_extra:
                policy_values["unexpected"] = True
            policy_path.write_text(json.dumps(policy_values))
        server = types.ModuleType("server")
        server.main = lambda: self.calls.append({
            "store": os.environ.get("AGENT_BOARD_STORE_PROFILE"),
            "sandbox": os.environ.get("AGENT_BOARD_SANDBOX"),
            "ssl": os.environ.get("SSL_CERT_FILE"),
        })
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name in {"certifi", "usage_source", "claude_project_source", "claude_chat_source",
                        "claude_chat_monitor", "chatgpt_reply_source", "privacy_network", "process_reader"}:
                self.fail("A Store entry attempted a credential, network or direct helper import: " + name)
            return original_import(name, *args, **kwargs)

        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(self.executable)), \
                patch.object(sys, "_MEIPASS", str(self.contents / "Frameworks"), create=True), \
                patch.object(sys, "argv", [str(self.executable), *arguments]), \
                patch.dict(sys.modules, {"server": server}), \
                patch.dict(os.environ, {"AGENT_BOARD_STORE_PROFILE": "untrusted-shell-flag"}, clear=True), \
                patch("builtins.__import__", side_effect=guarded_import), \
                patch("collector_entry.os.getpgid", return_value=os.getpid()), \
                patch("collector_entry.runpy.run_module", side_effect=AssertionError("Account helper dispatched")):
            collector_entry.main()

    def test_both_reviewed_store_strategies_reach_server_with_sandbox_and_without_direct_imports(self):
        for strategy in ("hidden", "us-external"):
            with self.subTest(strategy=strategy):
                self.calls.clear()
                self.invoke(strategy)
                self.assertEqual(self.calls, [{"store": "1", "sandbox": "1", "ssl": None}])

    def test_unknown_missing_and_wrong_type_support_strategies_fail_before_server(self):
        for strategy in ("MISSING", "", "worldwide", "US-external", "hidden ", True, False, 1, 0,
                         ["us-external"], {"strategy": "us-external"}):
            with self.subTest(strategy=strategy), self.assertRaisesRegex(SystemExit, "distribution profile is invalid"):
                self.invoke(strategy)
            self.assertEqual(self.calls, [])

    def test_store_metadata_requires_the_strict_boolean_sandbox_flag(self):
        for sandbox in ("MISSING", False, 1, 0, "true", []):
            with self.subTest(sandbox=sandbox), self.assertRaisesRegex(SystemExit, "distribution profile is invalid"):
                self.invoke("us-external", sandbox=sandbox)
            self.assertEqual(self.calls, [])

    def test_both_strategies_keep_all_direct_account_and_reader_helpers_blocked(self):
        for strategy in ("hidden", "us-external"):
            for argument in ("--authorize-claude", "--read-projects", "--read-claude-usage", "--read-claude-chats",
                             "--reader-watchdog", "--reader-launch"):
                with self.subTest(strategy=strategy, argument=argument), \
                        self.assertRaisesRegex(SystemExit, "account helper is unavailable"):
                    self.invoke(strategy, arguments=(argument,))
                self.assertEqual(self.calls, [])

    def test_both_strategies_cannot_replace_the_required_immutable_sandbox_policy(self):
        for strategy in ("hidden", "us-external"):
            for policy in (None, False, 1, "true", []):
                path = self.contents / "Resources/sandbox-collector-policy.json"
                path.unlink(missing_ok=True)
                with self.subTest(strategy=strategy, policy=policy), self.assertRaisesRegex(SystemExit, "collector policy"):
                    self.invoke(strategy, policy=policy)
                self.assertEqual(self.calls, [])

    def test_policy_version_type_and_exact_key_set_remain_fail_closed(self):
        for version in (True, False, 1.0, "1", 0, 2):
            with self.subTest(version=version), self.assertRaisesRegex(SystemExit, "collector policy"):
                self.invoke("us-external", policy_version=version)
            self.assertEqual(self.calls, [])
        with self.assertRaisesRegex(SystemExit, "collector policy"):
            self.invoke("us-external", policy_extra=True)
        self.assertEqual(self.calls, [])


class SandboxPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        for name in ("codex", "claude", "desktop"):
            (self.root / name).mkdir()

    def monitor(self):
        return Monitor(self.root / "data", self.root / "codex", self.root / "claude",
                       usage_enabled=True, projects_enabled=True,
                       claude_desktop_root=self.root / "desktop", sandbox=True)

    def test_unsupported_credential_and_external_command_paths_cannot_start(self):
        with patch("claude_source._find_claude", side_effect=AssertionError("External discovery")), \
                patch("server.UsageSource", side_effect=AssertionError("Credential read")), \
                patch("server.ClaudeProjectSource", side_effect=AssertionError("Account read")):
            monitor = self.monitor()
            monitor.scan()
            self.assertIsNone(monitor.sources[1].command)
            self.assertIsNone(monitor.usage_source)
            self.assertIsNone(monitor.project_source)
            self.assertTrue(all(not item["available"] for item in monitor.snapshot()["usage"]))

    def test_no_default_home_discovery_when_grants_are_missing(self):
        with self.assertRaises(ValueError):
            Monitor(self.root / "missing", sandbox=True)

    def test_source_errors_never_log_transcript_or_credential_contents(self):
        monitor = self.monitor()
        marker = "fixture-private-path-and-token-canary"
        with patch.object(monitor.sources[0], "scan", side_effect=RuntimeError(marker)):
            with self.assertLogs("agent-board", level="WARNING") as captured:
                monitor.scan()
        self.assertNotIn(marker, " ".join(captured.output))
        self.assertNotIn(marker, json.dumps(monitor.snapshot()))

    def test_direct_helper_modes_fail_closed_before_credentials_or_network(self):
        entry = Path(__file__).resolve().parents[1] / "collector_entry.py"
        environment = dict(os.environ, AGENT_BOARD_SANDBOX="1")
        for argument in ("--authorize-claude", "--read-projects", "--read-claude-usage", "--read-claude-chats"):
            result = subprocess.run([sys.executable, str(entry), argument], env=environment,
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unavailable in the sandbox preview", result.stderr)
            self.assertEqual(result.stdout, "")

    def test_sandbox_flag_without_owner_grants_does_not_start(self):
        entry = Path(__file__).resolve().parents[1] / "collector_entry.py"
        result = subprocess.run([sys.executable, str(entry), "--sandbox", "--data-dir", str(self.root / "run")],
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sandbox source grants could not be verified", result.stderr)
        self.assertFalse((self.root / "run/board.sqlite3").exists())

    def test_frozen_build_policy_requires_strict_helper_modes(self):
        policy = self.root / "sandbox-collector-policy.json"
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", str(self.root), create=True):
            self.assertFalse(collector_entry.sandbox_build_required())
            policy.write_text(json.dumps({"version": 1, "sandbox_required": True}))
            self.assertTrue(collector_entry.sandbox_build_required())
            for content in ('{}', '{"version":1,"sandbox_required":false}', '[1]', 'malformed', 'x' * 1025):
                policy.write_text(content)
                with self.assertRaises(SystemExit):
                    collector_entry.sandbox_build_required()
            policy.unlink()
            policy.symlink_to(self.root / "missing-policy")
            with self.assertRaises(SystemExit):
                collector_entry.sandbox_build_required()

    def test_unselected_source_manifest_cannot_be_replaced_or_expand_access(self):
        support = self.root / "manifest-support"
        (support / "data").mkdir(parents=True, mode=0o700)
        roots = {provider: support / "sources-unavailable" / provider
                 for provider in ("codex", "claude", "claude_desktop")}
        for root in roots.values():
            root.mkdir(parents=True)
        manifest = support / "data/manifest.json"
        contents = json.dumps({"version": 1, "sources": {
            provider: {"path": str(root)} for provider, root in roots.items()}})
        manifest.write_text(contents)
        manifest.chmod(0o600)
        self.assertEqual(activate_source_bookmarks(manifest, roots, support), dict.fromkeys(roots, False))
        manifest.chmod(0o644)
        with self.assertRaises(ValueError):
            activate_source_bookmarks(manifest, roots, support)
        manifest.chmod(0o600)
        substitute = manifest.with_name("substitute.json")
        substitute.symlink_to(manifest)
        with self.assertRaises((OSError, ValueError)):
            activate_source_bookmarks(substitute, roots, support)
        changed = dict(roots, codex=self.root / "codex")
        with self.assertRaises(ValueError):
            activate_source_bookmarks(manifest, changed, support)
        (roots["claude"] / "unexpected-data").write_text("A source cannot become selected implicitly.")
        with self.assertRaises(ValueError):
            activate_source_bookmarks(manifest, roots, support)

    def test_native_open_handoff_validates_saved_chat_without_running_open(self):
        monitor = self.monitor()
        now = time.time() + 1
        monitor.store.apply([{"provider": "codex", "id": "sandbox-fixture", "title": "Selected chat",
                              "status": "ready", "updated_at": now, "event_id": "fixture-ready", "can_open": True}])
        server = BoardHTTPServer(("127.0.0.1", 0), Handler)
        server.monitor, server.access_token, server.instance_id = monitor, "a" * 64, "sandbox-fixture"
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        try:
            with patch("server.subprocess.run", side_effect=AssertionError("Forbidden external command")):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request("POST", "/api/sessions/codex:sandbox-fixture/open", "{}", {
                    "X-Agent-Board-Access": server.access_token, "X-Agent-Board-Token": monitor.token,
                    "Content-Type": "application/json"})
                response = connection.getresponse()
                value = json.loads(response.read())
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(value["native_open_url"], "codex://threads/sandbox-fixture")
                self.assertEqual(value["provider"], "codex")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == "__main__":
    unittest.main()
