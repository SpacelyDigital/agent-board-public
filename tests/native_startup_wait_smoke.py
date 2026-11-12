"""Run owned startup controls with the real Swift runtime and no provider reads.

The injected URLProtocol handles every health request in memory. A disposable
shell child only waits for its stdin lifetime to close. This does not establish
real TCC, login-item or packaged-app behavior.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import plistlib
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]

SWIFT = r'''import Foundation
import Darwin
let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
var count = 0
func check(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { fputs("FAIL: \(message)\n", stderr); exit(1) }
    count += 1
}
func pump(_ seconds: Double) {
    let end = ProcessInfo.processInfo.systemUptime + seconds
    while ProcessInfo.processInfo.systemUptime < end {
        _ = RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.005))
    }
}
func until(_ seconds: Double, _ condition: () -> Bool) {
    let end = ProcessInfo.processInfo.systemUptime + seconds
    while !condition() && ProcessInfo.processInfo.systemUptime < end { pump(0.005) }
    check(condition(), "bounded condition completed")
}
struct Reply {
    var status = 200
    var value: [String: Any] = [:]
    var delay: Double = 0
    var stall = false
    var failure = false
    var malformed = false
}
final class FakeHealth: URLProtocol {
    static var handler: ((URLRequest) -> Reply)!
    static var active = 0
    static var peak = 0
    static var requests = 0
    private var ended = false
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        DispatchQueue.main.async { [self] in
            if ended { return }
            Self.active += 1
            Self.peak = max(Self.peak, Self.active)
            Self.requests += 1
            check(request.url?.host == "127.0.0.1" && request.url?.path == "/api/health", "only local health URL")
            let reply = Self.handler(request)
            if reply.stall { return }
            DispatchQueue.main.asyncAfter(deadline: .now() + reply.delay) { [self] in
                if ended { return }
                ended = true
                Self.active -= 1
                if reply.failure {
                    client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
                    return
                }
                let response = HTTPURLResponse(url: request.url!, statusCode: reply.status,
                                               httpVersion: "HTTP/1.1", headerFields: [:])!
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: reply.malformed ? Data("not-json".utf8) : try! JSONSerialization.data(withJSONObject: reply.value))
                client?.urlProtocolDidFinishLoading(self)
            }
        }
    }
    override func stopLoading() {
        DispatchQueue.main.async { [self] in
            if !ended { ended = true; Self.active -= 1 }
        }
    }
    static func reset() {
        check(active == 0, "no prior health task retained")
        active = 0; peak = 0; requests = 0
    }
}
let config = URLSessionConfiguration.ephemeral
config.protocolClasses = [FakeHealth.self]
config.connectionProxyDictionary = [:]
let session = URLSession(configuration: config, delegate: LocalSessionDelegate(), delegateQueue: nil)
let production = CollectorRuntime.StartupTiming.production
check(production.softAttempts == 100 && production.hardSeconds == 300 && production.retrySeconds == 0.25 && production.requestSeconds == 1, "fixed production budgets")
func manager(_ name: String, hard: Double = 1.5, helper: String = "hold") -> CollectorRuntime {
    CollectorRuntime(bundle: Bundle(url: root.appendingPathComponent(helper + ".app"))!,
        support: root.appendingPathComponent(name),
        sourceRoots: (root.appendingPathComponent("codex"), root.appendingPathComponent("claude")),
        legacyPlist: root.appendingPathComponent("no-legacy.plist"), sandboxed: false,
        startupTiming: .init(softAttempts: 2, hardSeconds: hard, retrySeconds: 0.02, requestSeconds: 0.05),
        healthSession: session, portProvider: { 19999 })
}
func valid(_ runtime: CollectorRuntime) -> [String: Any] {
    let args = runtime.process!.arguments!
    let index = args.firstIndex(of: "--instance-id")!
    return ["app": "agent-board", "instance_id": args[index + 1]]
}
func verifyRequest(_ request: URLRequest, _ runtime: CollectorRuntime) {
    check(request.value(forHTTPHeaderField: "X-Agent-Board-Access") == runtime.accessToken && runtime.accessToken.count == 64,
          "exact per-launch native access header")
    check(request.timeoutInterval > 0 && request.timeoutInterval <= 0.05, "request bounded by configured budget")
}
func cleaned(_ runtime: CollectorRuntime, _ name: String, _ child: Process?) {
    until(1.5) { child?.isRunning != true && FakeHealth.active == 0 }
    check(runtime.process == nil && runtime.startupTask == nil, "owned process and health task cleared")
    let files = (try? FileManager.default.contentsOfDirectory(atPath: root.appendingPathComponent(name + "/data").path)) ?? []
    check(!files.contains { $0.hasPrefix(".collector-access-") || $0.hasPrefix(".collector-sources-") }, "per-launch secrets removed")
}

// Invalid status, app, nonce and transport/JSON responses cannot bypass readiness.
do {
    FakeHealth.reset()
    let runtime = manager("authentication")
    var completed = 0
    var passed = false
    var waiting = 0
    runtime.startupWaiting = { waiting += 1 }
    FakeHealth.handler = { request in
        verifyRequest(request, runtime)
        check(completed == 0, "no premature readiness")
        switch FakeHealth.requests {
        case 1: return Reply(status: 403, value: valid(runtime))
        case 2: return Reply(status: 302, value: valid(runtime))
        case 3: return Reply(value: ["app": "other", "instance_id": valid(runtime)["instance_id"]!])
        case 4: return Reply(value: ["app": "agent-board", "instance_id": "wrong"])
        case 5: return Reply(malformed: true)
        case 6: return Reply(failure: true)
        default: return Reply(value: valid(runtime))
        }
    }
    runtime.start { result in completed += 1; if case .success = result { passed = true } }
    let child = runtime.process
    until(2) { completed > 0 }
    check(passed && completed == 1 && waiting == 1 && FakeHealth.requests == 7, "authenticated recovery after one soft notification")
    check(FakeHealth.peak == 1 && runtime.process === child, "serialized requests and same child")
    pump(0.04)
    check(completed == 1, "completion delivered once")
    runtime.stop(); cleaned(runtime, "authentication", child)
}

// The soft signal retains the same process, token and launch files.
do {
    FakeHealth.reset()
    let runtime = manager("soft-recovery")
    var result: Result<URL, Error>?
    var waiting = 0
    var originalToken = ""
    var child: Process?
    var waitingPID: Int32?
    runtime.startupWaiting = {
        waiting += 1
        check(runtime.accessToken == originalToken && runtime.process === child && child?.isRunning == true, "soft waiting preserves child and token")
        waitingPID = runtime.process?.processIdentifier
    }
    FakeHealth.handler = { request in
        verifyRequest(request, runtime)
        return FakeHealth.requests <= 4 ? Reply(failure: true) : Reply(value: valid(runtime), delay: 0.02)
    }
    runtime.start { result = $0 }
    child = runtime.process; originalToken = runtime.accessToken
    until(2) { result != nil }
    if case .success? = result {} else { check(false, "soft recovery succeeded") }
    check(waiting == 1 && waitingPID == child?.processIdentifier && FakeHealth.peak == 1, "soft signal once without relaunch")
    let log = try! String(contentsOf: runtime.logURL, encoding: .utf8)
    check(log.contains("phase=still_starting") && log.contains("phase=ready"), "waiting and ready diagnostics recorded")
    check(!log.contains(originalToken) && !log.contains(root.path), "diagnostics exclude token and paths")
    runtime.stop(); cleaned(runtime, "soft-recovery", child)
}

// A request which never returns is cancelled by the monotonic outer deadline.
do {
    FakeHealth.reset()
    let runtime = manager("hard-expiry", hard: 0.25)
    var calls = 0
    var timedOut = false
    FakeHealth.handler = { request in verifyRequest(request, runtime); return Reply(stall: true) }
    let began = ProcessInfo.processInfo.systemUptime
    runtime.start { result in
        calls += 1
        if case .failure(let error) = result, case CollectorRuntime.RuntimeError.startupTimedOut = error { timedOut = true }
    }
    let child = runtime.process
    until(1) { calls > 0 }
    let elapsed = ProcessInfo.processInfo.systemUptime - began
    check(timedOut && elapsed >= 0.24 && elapsed < 0.9 && calls == 1, "hard deadline independent of attempts and pending request")
    let requestsAtFailure = FakeHealth.requests
    cleaned(runtime, "hard-expiry", child)
    pump(0.08); check(calls == 1 && FakeHealth.requests == requestsAtFailure && FakeHealth.peak == 1, "no retries after hard expiry")
}

// A response arriving after the hard deadline cannot report readiness.
do {
    FakeHealth.reset()
    let runtime = manager("late-ready", hard: 0.12)
    var calls = 0
    var success = false
    FakeHealth.handler = { _ in Reply(value: valid(runtime), delay: 0.3) }
    runtime.start { result in calls += 1; if case .success = result { success = true } }
    let child = runtime.process
    until(1) { calls > 0 }
    pump(0.35)
    check(calls == 1 && !success, "late readiness ignored")
    cleaned(runtime, "late-ready", child)
}

// Child exit is terminal immediately, even with a pending health request.
do {
    FakeHealth.reset()
    let runtime = manager("child-exit", hard: 2, helper: "exit")
    var calls = 0
    var exited = false
    FakeHealth.handler = { _ in Reply(stall: true) }
    let began = ProcessInfo.processInfo.systemUptime
    runtime.start { result in
        calls += 1
        if case .failure(let error) = result, case CollectorRuntime.RuntimeError.exitedDuringStartup = error { exited = true }
    }
    let child = runtime.process
    until(1) { calls > 0 }
    check(exited && calls == 1 && ProcessInfo.processInfo.systemUptime - began < 1, "early child exit visible without waiting for hard deadline")
    cleaned(runtime, "child-exit", child)
}

// Explicit stop cancels pending work without a late completion or waiting page.
do {
    FakeHealth.reset()
    let runtime = manager("cancel", hard: 0.25)
    var calls = 0
    var waiting = 0
    runtime.startupWaiting = { waiting += 1 }
    FakeHealth.handler = { _ in Reply(value: valid(runtime), delay: 0.2) }
    runtime.start { _ in calls += 1 }
    let child = runtime.process
    until(0.5) { FakeHealth.requests == 1 }
    runtime.stop()
    cleaned(runtime, "cancel", child)
    pump(0.35)
    check(calls == 0 && waiting == 0 && FakeHealth.requests == 1, "cancel silences late callbacks, timer and retries")
}

// Once ready, an owned child exit follows the existing unexpected-exit path.
do {
    FakeHealth.reset()
    let runtime = manager("ready-exit")
    var calls = 0
    var exited = 0
    runtime.unexpectedExit = { exited += 1 }
    FakeHealth.handler = { _ in Reply(value: valid(runtime)) }
    runtime.start { result in
        if case .success = result { calls += 1 }
    }
    until(1) { calls == 1 }
    let child = runtime.process
    child!.terminate()
    until(1) { exited == 1 }
    cleaned(runtime, "ready-exit", child)
    pump(0.05)
    check(calls == 1 && exited == 1, "post-ready exit does not complete startup twice")
}

// Cancelling from the waiting callback also prevents another health request.
do {
    FakeHealth.reset()
    let runtime = manager("cancel-on-wait", hard: 0.25)
    var calls = 0
    var waiting = 0
    runtime.startupWaiting = { waiting += 1; runtime.stop() }
    FakeHealth.handler = { _ in Reply(failure: true) }
    runtime.start { _ in calls += 1 }
    let child = runtime.process
    until(0.5) { waiting == 1 }
    cleaned(runtime, "cancel-on-wait", child)
    pump(0.3)
    check(calls == 0 && FakeHealth.requests == 2, "waiting callback cancellation does not schedule another request")
}

// Replacing a pending generation cannot complete the retired launch.
do {
    FakeHealth.reset()
    let runtime = manager("generation")
    var oldCalls = 0
    var newCalls = 0
    var success = false
    FakeHealth.handler = { _ in Reply(value: valid(runtime), delay: FakeHealth.requests == 1 ? 0.3 : 0) }
    runtime.start { _ in oldCalls += 1 }
    let old = runtime.process
    let oldToken = runtime.accessToken
    until(0.5) { FakeHealth.requests == 1 }
    runtime.start { result in newCalls += 1; if case .success = result { success = true } }
    until(2) { newCalls > 0 }
    let child = runtime.process
    pump(0.35)
    check(oldCalls == 0 && newCalls == 1 && success, "only current generation completes")
    check(old?.isRunning == false && child !== old && runtime.accessToken != oldToken, "replacement gets new child and credentials")
    check(FakeHealth.peak == 1, "generation replacement does not overlap health requests")
    runtime.stop(); cleaned(runtime, "generation", child)
}

// An executable with an invalid interpreter reports a sanitized launch failure.
do {
    FakeHealth.reset()
    let runtime = manager("invalid-launch", helper: "invalid")
    var calls = 0
    var failed = false
    FakeHealth.handler = { _ in check(false, "invalid child must not receive health request"); return Reply() }
    runtime.start { result in
        calls += 1
        if case .failure(let error) = result, case CollectorRuntime.RuntimeError.launchFailed = error {
            failed = !error.localizedDescription.contains(root.path)
        }
    }
    check(failed && calls == 1 && FakeHealth.requests == 0, "invalid launch fails without a path in UI error")
    cleaned(runtime, "invalid-launch", runtime.process)
    let log = try! String(contentsOf: runtime.logURL, encoding: .utf8)
    check(log.contains("phase=failed") && !log.contains(root.path), "launch diagnostic has no exception path")
}

// Native pre-launch diagnostics also redact arbitrary error text and domains.
do {
    let runtime = manager("diagnostic")
    runtime.recordStartupPhase(.restoringSources, error: NSError(domain: root.path + "-secret", code: 42,
        userInfo: [NSLocalizedDescriptionKey: "synthetic-token-and-title"]))
    let log = try! String(contentsOf: runtime.logURL, encoding: .utf8)
    check(log.contains("phase=restore_sources error_domain=other error_code=42"), "bounded diagnostic domain and code")
    check(!log.contains(root.path) && !log.contains("synthetic-token-and-title"), "pre-launch diagnostic excludes error text")
}

// A startup diagnostic must not bypass Sample storage's path validation.
do {
    let live = root.appendingPathComponent("sample-diagnostic")
    let outside = root.appendingPathComponent("sample-outside")
    let runtime = CollectorRuntime(bundle: Bundle(url: root.appendingPathComponent("sample.app"))!, support: live,
        legacyPlist: root.appendingPathComponent("no-legacy.plist"), sandboxed: true,
        startupTiming: .init(softAttempts: 2, hardSeconds: 0.25, retrySeconds: 0.02, requestSeconds: 0.05),
        healthSession: session, portProvider: { 19999 })
    try! runtime.setSampleMode(true)
    try! FileManager.default.createDirectory(at: live, withIntermediateDirectories: true)
    try! FileManager.default.createDirectory(at: outside, withIntermediateDirectories: true)
    try! FileManager.default.createSymbolicLink(at: live.appendingPathComponent("Samples"), withDestinationURL: outside)
    runtime.recordStartupPhase(.preparing)
    check(try! FileManager.default.contentsOfDirectory(atPath: outside.path).isEmpty, "Sample diagnostic cannot follow substituted intermediate path")
    var failed = false
    runtime.start { if case .failure = $0 { failed = true } }
    check(failed && runtime.process == nil, "invalid Sample path still fails before launching")
    check(try! FileManager.default.contentsOfDirectory(atPath: outside.path).isEmpty, "failed Sample startup cannot write diagnostic outside its data root")
}
session.invalidateAndCancel()
print("STARTUP_WAIT_CHECKS=\(count)")
'''


def helper_phase_checks(qa):
    """Execute server.main with owned fakes, checking ordering without I/O."""
    import importlib.util
    import io
    import logging
    import sys
    from unittest.mock import patch
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("startup_wait_server", ROOT / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    events = []
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    server.LOG.addHandler(handler)
    previous = server.LOG.level
    server.LOG.setLevel(logging.INFO)

    class Monitor:
        def __init__(self, *args, **kwargs):
            assert not args[4] and not args[5], "account reads disabled"
            assert "startup phase=construct_readers" in output.getvalue()
            self.stop = server.threading.Event()
            events.append("monitor")
        def scan(self):
            assert "startup phase=initial_scan" in output.getvalue()
            assert "server" not in events, "listener cannot precede first scan"
            events.append("scan")
        def run(self): pass
        def run_usage(self): pass
        def run_projects(self): pass

    class HTTP:
        server_port = 19999
        def __init__(self, *args):
            assert events == ["monitor", "scan"]
            assert "startup phase=initial_scan_complete" in output.getvalue()
            assert "startup phase=bind_listener" in output.getvalue()
            events.append("server")
        def serve_forever(self, **kwargs):
            assert "startup phase=ready" in output.getvalue()
            events.append("serve")
        def server_close(self): events.append("close")

    try:
        with patch.object(sys, "argv", ["server", "--data-dir", str(qa / "fake-data"), "--no-usage", "--no-projects"]), \
             patch.object(server, "Monitor", Monitor), patch.object(server, "BoardHTTPServer", HTTP), \
             patch.object(server.signal, "signal"), patch.object(server, "stop_all_processes"), \
             patch.object(server, "NativeLaunchFiles") as files, patch.object(server.threading, "Thread"):
            server.main()
            files.return_value.cleanup.assert_called_once()
        assert events == ["monitor", "scan", "server", "serve", "close"]
        expected = ["prepare", "construct_readers", "initial_scan", "initial_scan_complete", "bind_listener", "ready"]
        actual = [line.split("phase=", 1)[1] for line in output.getvalue().splitlines() if line.startswith("startup phase=")]
        assert actual == expected, actual
        assert str(qa) not in output.getvalue()
        # Grant validation must log before it can block, and refuse readiness
        # on a failed grant without logging the exception's private text.
        import sandbox_sources
        output.seek(0)
        output.truncate()
        def reject_grants(*args, **kwargs):
            assert "startup phase=verify_source_grants" in output.getvalue()
            raise ValueError(str(qa) + "/synthetic-private-grant")
        sandbox_argv = ["server", "--data-dir", str(qa / "fake-data"), "--sandbox", "--no-usage", "--no-projects",
                        "--access-token-file", str(qa / "fake-token"), "--parent-lifetime-stdin",
                        "--codex-root", str(qa / "codex"), "--claude-root", str(qa / "claude"),
                        "--claude-desktop-root", str(qa / "desktop/claude-code-sessions")]
        with patch.object(sys, "argv", sandbox_argv), patch.object(server, "read_access_token", return_value="synthetic"), \
             patch.object(sandbox_sources, "activate_source_bookmarks", side_effect=reject_grants), \
             patch.object(server, "Monitor") as forbidden, patch.object(server, "BoardHTTPServer") as listener:
            try:
                server.main()
            except SystemExit as error:
                assert error.code == 1
            else:
                raise AssertionError("Unverified grants reached readiness")
            forbidden.assert_not_called()
            listener.assert_not_called()
        assert "startup phase=source_grants_failed" in output.getvalue()
        assert "phase=ready" not in output.getvalue() and str(qa) not in output.getvalue()
        return 16
    finally:
        server.LOG.removeHandler(handler)
        server.LOG.setLevel(previous)
        sys.path.remove(str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-root", type=Path, default=ROOT / ".local")
    args = parser.parse_args()
    args.qa_root.mkdir(parents=True, exist_ok=True)
    qa = Path(tempfile.mkdtemp(prefix="startup-wait-", dir=args.qa_root))
    os.chmod(qa, 0o700)
    pins = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ("CollectorRuntime.swift", "MacApp.swift", "server.py")}
    for name, body in {"hold": "#!/bin/sh\nwhile IFS= read -r line; do :; done\n", "sample": "#!/bin/sh\nwhile IFS= read -r line; do :; done\n", "exit": "#!/bin/sh\nexit 7\n",
                       "invalid": "#!/missing-owned-fixture-interpreter\n"}.items():
        app = qa / (name + ".app")
        contents = app / "Contents"
        helper = contents / "Helpers/AgentBoardCollector.app/Contents/MacOS/agent-board-collector"
        helper.parent.mkdir(parents=True)
        helper.write_text(body)
        helper.chmod(0o700)
        with (contents / "Info.plist").open("wb") as file:
            plistlib.dump({"CFBundleIdentifier": "local.agentboard.startupfixture." + name,
                          "CFBundlePackageType": "APPL", "CFBundleVersion": "1",
                          "AgentBoardStoreDistribution": name == "sample"}, file)
    (qa / "codex").mkdir()
    (qa / "claude").mkdir()
    swift = qa / "main.swift"
    swift.write_text(SWIFT)
    binary = qa / "startup-wait-checks"
    compile_command = ["xcrun", "swiftc", "-module-cache-path", str(qa / "module-cache"),
                       str(ROOT / "CollectorRuntime.swift"), str(swift), "-o", str(binary)]
    compiled = subprocess.run(compile_command, capture_output=True, text=True, timeout=90)
    (qa / "compile.stdout").write_text(compiled.stdout)
    (qa / "compile.stderr").write_text(compiled.stderr)
    if compiled.returncode:
        raise RuntimeError("Swift compilation failed; see " + str(qa))
    began = time.monotonic()
    native = subprocess.run([str(binary), str(qa)], capture_output=True, text=True, timeout=30)
    (qa / "native.stdout").write_text(native.stdout)
    (qa / "native.stderr").write_text(native.stderr)
    if native.returncode:
        raise RuntimeError("Native startup controls failed; see " + str(qa))
    checks = int(native.stdout.strip().split("STARTUP_WAIT_CHECKS=")[-1])
    helper_checks = helper_phase_checks(qa)
    assert all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value for name, value in pins.items()), "source changed during proof"
    receipt = {"native_checks": checks, "helper_phase_checks": helper_checks,
               "seconds": round(time.monotonic() - began, 3), "source_sha256": pins,
               "provider_reads": False, "real_network_requests": False,
               "scope": "real Swift runtime with in-memory health responses and owned lifetime children; helper ordering with owned fakes; not packaged, login or TCC proof"}
    (qa / "RESULT.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"qa": str(qa), **receipt}, indent=2))


if __name__ == "__main__":
    main()
