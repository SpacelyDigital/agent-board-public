#!/usr/bin/env python3
"""Run reproducible, disposable browser journeys without touching provider accounts.

Usage: python3 tests/stress_browser.py --profiles 300 --concurrency 2
Requires Playwright WebKit; set PLAYWRIGHT_MODULE to its installed module path.
Every profile receives an independent HOME, provider fixtures, collector, and DB.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


class Fixture:
    def __init__(self, directory, runtime_root=ROOT):
        self.runtime_root = runtime_root
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home = self.directory / "home"
        self.home.mkdir(mode=0o700)
        self.codex = self.directory / "codex"
        (self.codex / "sessions").mkdir(parents=True)
        self.claude = self.directory / "claude"
        (self.claude / "projects" / "disposable").mkdir(parents=True)
        self.data = self.directory / "data"
        self.data.mkdir(mode=0o700)
        self.token = secrets.token_hex(32)
        self.tokenfile = self.directory / "access"
        self.tokenfile.write_text(self.token)
        self.tokenfile.chmod(0o600)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.process = None
        self.log = (self.directory / "collector.log").open("wb")
        self.serial = 0
        self.turns = {ident: "turn-1" for ident in ("ready", "waiting", "working")}
        now = time.time()
        with sqlite3.connect(self.codex / "state_5.sqlite") as db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY,rollout_path TEXT,updated_at INTEGER,title TEXT,cwd TEXT,archived INTEGER,thread_source TEXT,agent_path TEXT)")
            for ident in ("ready", "waiting", "working"):
                path = self.codex_path(ident)
                self.append(path, {"type": "session_meta", "payload": {"id": ident, "cwd": "/disposable/stress", "thread_source": "user"}})
                self.codex_event(ident, "task_started", turn="turn-1")
                db.execute("INSERT INTO threads VALUES (?,?,?,?,?,0,'user',NULL)", (ident, str(path), int(now), f"Disposable {ident}", "/disposable/stress"))
        for ident in ("claude-ready", "claude-working"):
            self.claude_event(ident, {"type": "user", "uuid": "prompt-1", "promptId": "prompt-1", "message": {"role": "user", "content": "Disposable stress request"}})
        self.start()
        self.wait(lambda state: len(state["sessions"]) == 3)
        # Claude discovery intentionally ignores transcript files older than
        # activation, so simulate those prompts after the collector starts.
        for ident in ("claude-ready", "claude-working"):
            self.claude_event(ident, {"type": "user", "uuid": "prompt-2", "promptId": "prompt-2", "message": {"role": "user", "content": "Disposable fresh request"}})
        self.wait(lambda state: len(state["sessions"]) == 5)
        self.codex_event("ready", "task_complete", turn="turn-1")
        self.append(self.codex_path("ready"), {"timestamp": time.time(), "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": 'Literal <img src=x onerror="window.owned=true"> & disposable reply'}]}})
        self.codex_event("waiting", "request_user_input", turn="turn-1", call_id="ask-1")
        self.claude_event("claude-ready", {"type": "assistant", "uuid": "reply-1", "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "Disposable Claude reply"}]}})
        self.wait(lambda state: {row["status"] for row in state["sessions"]} >= {"ready", "needs_you", "cooking"})

    @staticmethod
    def append(path, record):
        with path.open("a") as stream:
            stream.write(json.dumps(record) + "\n")

    def codex_path(self, ident):
        return self.codex / "sessions" / (ident + ".jsonl")

    def codex_event(self, ident, kind, turn=None, **fields):
        self.append(self.codex_path(ident), {"timestamp": time.time(), "type": "event_msg", "payload": {"type": kind, "turn_id": turn or f"turn-{self.serial}", **fields}})

    def claude_event(self, ident, record):
        self.append(self.claude / "projects" / "disposable" / (ident + ".jsonl"), {"sessionId": ident, "cwd": "/disposable/stress", "timestamp": time.time(), **record})

    def start(self):
        # Do not forward API keys, account configuration, or owner HOME to the
        # collector, even though explicit fixture roots disable those adapters.
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONNOUSERSITE": "1", "LANG": "en_US.UTF-8"}
        self.process = subprocess.Popen([sys.executable, str(self.runtime_root / "server.py"), "--port", str(self.port), "--data-dir", str(self.data), "--codex-root", str(self.codex), "--claude-root", str(self.claude), "--no-usage", "--no-projects", "--interval", "0.5", "--instance-id", "disposable-stress", "--access-token-file", str(self.tokenfile)], cwd=self.home, env=env, stdout=self.log, stderr=subprocess.STDOUT)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(2)
        self.process = None

    def state(self):
        req = urllib.request.Request(self.base + "/api/state", headers={"X-Agent-Board-Access": self.token})
        with urllib.request.urlopen(req, timeout=2) as response:
            state = json.load(response)
        assert all(not item["available"] and not item["windows"] for item in state["usage"])
        assert not any(item["id"] == "claude_projects" for item in state["sources"])
        return state

    def wait(self, predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                state = self.state()
                if predicate(state):
                    return state
            except OSError:
                pass
            time.sleep(0.04)
        raise AssertionError("Disposable collector did not reach expected fixture state")

    def advance(self, ident="ready", status="cooking"):
        self.serial += 1
        if status == "cooking":
            self.turns[ident] = f"turn-{self.serial + 1}"
            self.codex_event(ident, "task_started", turn=self.turns[ident])
        elif status == "ready":
            self.codex_event(ident, "task_complete", turn=self.turns[ident])
        elif status == "needs_you":
            self.codex_event(ident, "request_user_input", turn=self.turns[ident], call_id=f"ask-{self.serial}")
        else:
            raise ValueError("Unsupported disposable state")
        return self.wait(lambda state: any(row["key"] == "codex:" + ident and row["status"] == status for section in ("sessions", "history", "background") for row in state[section]))

    def close(self):
        self.stop()
        self.log.close()


def fixture_main(directory, runtime_root):
    fixture = Fixture.__new__(Fixture)
    try:
        fixture.__init__(directory, runtime_root)
        print(json.dumps({"base": fixture.base, "token": fixture.token}), flush=True)
        for line in sys.stdin:
            command = json.loads(line)
            try:
                operation = command["operation"]
                if operation == "state":
                    result = fixture.state()
                elif operation == "advance":
                    result = fixture.advance(command.get("ident", "ready"), command.get("status", "cooking"))
                elif operation == "stop":
                    fixture.stop()
                    result = {"ok": True}
                elif operation == "restart":
                    fixture.stop()
                    fixture.start()
                    result = fixture.wait(lambda state: True)
                elif operation == "corrupt":
                    fixture.stop()
                    (fixture.data / "board.sqlite3").write_bytes(b"Disposable malformed SQLite fixture for recovery testing\n")
                    fixture.start()
                    result = fixture.wait(lambda state: bool(state.get("recovery_notice")))
                elif operation == "quit":
                    break
                else:
                    raise ValueError("Unknown fixture operation")
                print(json.dumps({"result": result}), flush=True)
            except Exception as error:
                print(json.dumps({"error": str(error)}), flush=True)
    finally:
        if hasattr(fixture, "log"):
            fixture.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20261030)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--fixture", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--runtime-root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.fixture:
        fixture_main(args.fixture, args.runtime_root)
        return
    if not 1 <= args.profiles <= 1000 or not 1 <= args.concurrency <= 3:
        parser.error("Use 1 to 1000 profiles and 1 to 3 concurrent browser journeys")
    parent = ROOT / ".local" / "stress-browser"
    parent.mkdir(parents=True, exist_ok=True)
    out = args.out or Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    out.mkdir(parents=True, exist_ok=True)
    runtime = out / "runtime-snapshot"
    runtime.mkdir()
    for name in ("server.py", "board_store.py", "codex_source.py", "claude_source.py", "claude_project_source.py", "chat_links.py", "usage_source.py", "process_reader.py", "privacy_network.py", "sandbox_sources.py", "source_access.py", "local_quota.py"):
        shutil.copyfile(ROOT / name, runtime / name)
    shutil.copytree(ROOT / "static", runtime / "static")
    hashes = {str(file.relative_to(runtime)): hashlib.sha256(file.read_bytes()).hexdigest() for file in sorted(runtime.rglob("*")) if file.is_file()}
    harness_hashes = {name: hashlib.sha256((ROOT / "tests" / name).read_bytes()).hexdigest() for name in ("stress_browser.py", "stress_browser.cjs")}
    (out / "source-manifest.json").write_text(json.dumps({"runtime": hashes, "harness": harness_hashes}, indent=2))
    config = {"out": str(out.resolve()), "profiles": args.profiles, "concurrency": args.concurrency, "seed": args.seed, "python": sys.executable, "fixtureScript": str(Path(__file__).resolve()), "runtimeRoot": str(runtime.resolve())}
    file = out / "config.json"
    file.write_text(json.dumps(config, indent=2))
    subprocess.run(["node", str(ROOT / "tests" / "stress_browser.cjs"), str(file)], check=True)
    print(out / "results.json")


if __name__ == "__main__":
    main()
