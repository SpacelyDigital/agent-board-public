"""Verify native-owner pipe lifetime with real isolated source/frozen collectors.

Optional first argument selects the built frozen collector. Every collector has
empty disposable provider roots and account reads disabled. No owner app runs.
"""

import json
import hashlib
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.025)
    return predicate()


def running(pid):
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=2)
    value = result.stdout.strip()
    return bool(value and not value.startswith("Z"))


def kill_fixture(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def configure(folder, frozen=None, lifetime=True):
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    for name in ("codex", "claude"):
        (folder / name).mkdir()
    token_file = folder / "access-token"
    token_file.write_text("a" * 64)
    token_file.chmod(0o600)
    args = ["--data-dir", str(folder / "data"), "--port", str(port), "--instance-id", "lifetime-fixture",
            "--access-token-file", str(token_file), "--codex-root", str(folder / "codex"),
            "--claude-root", str(folder / "claude"), "--no-usage", "--no-projects"]
    if lifetime:
        args.append("--parent-lifetime-stdin")
    command = [str(frozen)] if frozen else [sys.executable, str(ROOT / "server.py")]
    return command + args, port, args


def healthy(port):
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/health",
                                     headers={"X-Agent-Board-Access": "a" * 64})
    try:
        with urllib.request.urlopen(request, timeout=0.2) as response:
            return response.status == 200 and json.load(response)["instance_id"] == "lifetime-fixture"
    except (OSError, urllib.error.URLError, ValueError):
        return False


def check(mode, frozen=None):
    with tempfile.TemporaryDirectory(prefix="agent-board-lifetime-") as folder:
        folder = Path(folder)
        command, port, _ = configure(folder, frozen, lifetime=mode != "source-cli-opt-out")
        if mode == "already-closed":
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, start_new_session=True)
        else:
            child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, start_new_session=True)
        try:
            if mode == "already-closed":
                child.wait(timeout=5)
                assert child.returncode == 0, child.stderr.read().decode(errors="replace")
                assert not (folder / "data").exists(), "An already-dead owner initialized source data"
            else:
                assert wait_for(lambda: healthy(port)), "Disposable collector did not become ready"
                if mode == "unexpected-byte":
                    child.stdin.write(b"X")
                    child.stdin.flush()
                else:
                    child.stdin.close()
                if mode == "source-cli-opt-out":
                    time.sleep(0.2)
                    assert child.poll() is None and healthy(port), "Source CLI unexpectedly depends on native lifetime"
                else:
                    child.wait(timeout=5)
                    assert child.returncode == 0, child.stderr.read().decode(errors="replace")
                    assert not healthy(port), "Collector still served after owner pipe closed"
            return {"mode": mode, "passed": True, "frozen": bool(frozen)}
        finally:
            kill_fixture(child.pid)
            child.wait(timeout=5)


def check_forced_owner_death(frozen=None):
    with tempfile.TemporaryDirectory(prefix="agent-board-owner-death-") as folder:
        folder = Path(folder)
        command, port, _ = configure(folder, frozen)
        marker = folder / "collector-pid"
        bootstrap = """import pathlib,subprocess,sys,time,json
child=subprocess.Popen(json.loads(sys.argv[1]),stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
pathlib.Path(sys.argv[2]).write_text(str(child.pid))
time.sleep(60)
"""
        owner = subprocess.Popen([sys.executable, "-c", bootstrap, json.dumps(command), str(marker)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        collector_pid = None
        try:
            assert wait_for(marker.exists), "Fixture owner did not launch collector"
            collector_pid = int(marker.read_text())
            assert wait_for(lambda: healthy(port)), "Disposable collector did not become ready"
            owner.kill()
            owner.wait(timeout=5)
            assert wait_for(lambda: not running(collector_pid)), "Collector survived forced owner death"
            assert not healthy(port), "Collector served after forced owner death"
            return {"mode": "forced-owner-death", "passed": True, "frozen": bool(frozen)}
        finally:
            if collector_pid:
                kill_fixture(collector_pid)
            kill_fixture(owner.pid)
            owner.wait(timeout=5)


def check_managed_reader_cleanup():
    with tempfile.TemporaryDirectory(prefix="agent-board-owned-reader-") as folder:
        folder = Path(folder)
        _, port, args = configure(folder)
        marker = folder / "managed-reader-pid"
        reader = "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
        bootstrap = """import server,sys
from process_reader import read_process
Original=server.Monitor
class FixtureMonitor(Original):
 def run_usage(self):
  try: read_process([sys.executable,'-c',READER,MARKER],timeout=60,max_output=1024)
  except Exception: pass
server.Monitor=FixtureMonitor
server.main()
""".replace("READER", repr(reader)).replace("MARKER", repr(str(marker)))
        child = subprocess.Popen([sys.executable, "-c", bootstrap, *args], cwd=ROOT, stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        reader_pid = None
        try:
            assert wait_for(lambda: healthy(port)) and wait_for(marker.exists), "Fake managed reader did not start"
            reader_pid = int(marker.read_text())
            child.stdin.close()
            child.wait(timeout=5)
            assert child.returncode == 0, child.stderr.read().decode(errors="replace")
            assert wait_for(lambda: not running(reader_pid)), "Managed reader survived native lifetime EOF"
            return {"mode": "managed-reader-cleanup", "passed": True, "frozen": False}
        finally:
            if reader_pid:
                kill_fixture(reader_pid)
            kill_fixture(child.pid)
            child.wait(timeout=5)


def descendants(pid):
    found, pending = set(), [pid]
    while pending:
        parent = pending.pop()
        result = subprocess.run(["/usr/bin/pgrep", "-P", str(parent)], capture_output=True, text=True, timeout=2)
        for value in result.stdout.split():
            if value.isdigit() and int(value) not in found:
                child = int(value)
                found.add(child)
                pending.append(child)
    return found


def check_swift_owner_death(frozen):
    """SIGKILL the actual compiled runtime owner with its actual frozen child."""
    bundle = frozen.parents[5]
    expected = bundle / "Contents/Helpers/AgentBoardCollector.app/Contents/MacOS/agent-board-collector"
    assert expected.resolve() == frozen.resolve(), "Pass the collector inside the built complete Mac app"
    with tempfile.TemporaryDirectory(prefix="agent-board-swift-owner-") as folder:
        folder = Path(folder)
        for name in ("codex", "claude"):
            (folder / name).mkdir()
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        values = [bundle, folder / "support", folder / "codex", folder / "claude",
                  folder / "no-legacy.plist", folder / "ready.json"]
        encoded = ", ".join(json.dumps(str(value)) for value in values)
        program = '''import Foundation
import Darwin
let paths: [String] = [__PATHS__]
let runtime = CollectorRuntime(bundle: Bundle(path: paths[0])!, support: URL(fileURLWithPath: paths[1]),
    sourceRoots: (URL(fileURLWithPath: paths[2]), URL(fileURLWithPath: paths[3])),
    legacyPlist: URL(fileURLWithPath: paths[4]), preferredPort: __PORT__)
runtime.usageEnabled = false
runtime.projectsEnabled = false
runtime.start { result in
    guard case .success(let url) = result, let process = runtime.process else {
        print("FAIL: actual Swift runtime could not launch fixture collector")
        exit(1)
    }
    let args = process.arguments ?? []
    guard ["--no-usage", "--no-projects", "--parent-lifetime-stdin", "--codex-root", "--claude-root"].allSatisfy(args.contains) else {
        runtime.stop()
        print("FAIL: isolated roots/account-off/lifetime flags missing")
        exit(1)
    }
    let value: [String: Any] = ["pid": process.processIdentifier, "url": url.absoluteString,
                              "token": runtime.accessToken, "account_reads_disabled": true]
    try! JSONSerialization.data(withJSONObject: value).write(to: URL(fileURLWithPath: paths[5]))
}
RunLoop.main.run()
'''.replace("__PATHS__", encoded).replace("__PORT__", str(port))
        qa = ROOT / ".local/native-swift-owner-qa"
        qa.mkdir(parents=True, exist_ok=True)
        source, binary = qa / "main.swift", qa / "native-swift-owner"
        source.write_text(program)
        subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(ROOT / "build/swift-cache"),
                        str(source), str(ROOT / "CollectorRuntime.swift"), "-o", str(binary)], check=True)
        owner = subprocess.Popen([str(binary)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True)
        owned, collector_pid = set(), None
        try:
            marker = folder / "ready.json"
            assert wait_for(marker.exists, timeout=10), "Actual Swift owner did not launch isolated frozen collector"
            value = json.loads(marker.read_text())
            collector_pid = int(value["pid"])
            owned = descendants(owner.pid) | {collector_pid}
            assert value["account_reads_disabled"] is True
            request = urllib.request.Request(value["url"] + "api/health",
                                             headers={"X-Agent-Board-Access": value["token"]})
            with urllib.request.urlopen(request, timeout=1) as response:
                assert response.status == 200, "Actual runtime collector did not serve private health"
            owner.kill()
            owner.wait(timeout=5)
            assert wait_for(lambda: not any(running(pid) for pid in owned)), "An actual runtime-owned process survived Swift SIGKILL"
            try:
                urllib.request.urlopen(request, timeout=0.2)
            except (OSError, urllib.error.URLError):
                pass
            else:
                raise AssertionError("Actual frozen collector still served after Swift SIGKILL")
            return {"mode": "compiled-swift-owner-forced-death", "passed": True, "frozen": True,
                    "tracked_owned_processes": len(owned), "remaining_owned_processes": 0,
                    "empty_custom_sources": True, "account_reads_disabled": True}
        finally:
            for pid in owned:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if collector_pid:
                kill_fixture(collector_pid)
            kill_fixture(owner.pid)
            owner.wait(timeout=5)


if __name__ == "__main__":
    frozen = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    checks = [check(mode, frozen) for mode in ("already-closed", "pipe-closed", "unexpected-byte", "source-cli-opt-out")]
    checks += [check_forced_owner_death(frozen), check_managed_reader_cleanup()]
    if frozen:
        checks.append(check_swift_owner_death(frozen))
    report = {"provider_calls": 0, "checks": checks}
    if frozen:
        report["collector_sha256"] = hashlib.sha256(frozen.read_bytes()).hexdigest()
        report["runtime_sha256"] = hashlib.sha256((ROOT / "CollectorRuntime.swift").read_bytes()).hexdigest()
        report["test_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        qa = ROOT / ".local/native-review-qa"
        qa.mkdir(parents=True, exist_ok=True)
        (qa / "parent-lifetime-final.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
