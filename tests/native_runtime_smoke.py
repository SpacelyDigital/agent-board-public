"""Exercise the native runtime manager against the real frozen collector.

Run manually after build_app(); does not install the app or show OS dialogs.
"""
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import plistlib
import shlex
import sys

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / ".local/native-distribution-qa"


def main():
    QA.mkdir(parents=True, exist_ok=True)
    sources = QA / "sources"
    sources.mkdir(exist_ok=True)
    codex = sources / "codex"
    claude = sources / "claude"
    codex.mkdir(exist_ok=True)
    claude.mkdir(exist_ok=True)
    data = QA / "support"
    if data.exists():
        shutil.rmtree(data)
    database = codex / "state_5.sqlite"
    database.unlink(missing_ok=True)
    now = time.time()
    rollout = codex / "sessions" / time.strftime("%Y/%m/%d", time.gmtime(now)) / "rollout-native-fixture.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text("\n".join(json.dumps(record) for record in [
        {"type": "session_meta", "payload": {"id": "native-fixture", "cwd": "/example", "thread_source": "user", "source": "vscode"}},
        {"timestamp": now, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "test-turn", "started_at": now}},
    ]) + "\n")
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER, title TEXT, name TEXT, cwd TEXT, archived INTEGER, thread_source TEXT, source TEXT, agent_path TEXT)")
        db.execute("CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT)")
        db.execute("INSERT INTO threads VALUES (?, ?, ?, ?, NULL, ?, 0, 'user', 'vscode', NULL)",
                   ("native-fixture", str(rollout), int(now), "Native fixture", "/example"))
    values = [str(ROOT / "build/Agent Board.app"), str(data), str(codex), str(claude), str(QA / "no-legacy.plist")]
    encoded = ", ".join(json.dumps(value) for value in values)
    swift = '''import Foundation
import Darwin
let paths: [String] = [__PATHS__]
func check(_ value: @autoclosure () -> Bool, _ message: String) {
    if !value() { print("FAIL: " + message); exit(1) }
}
let manager = CollectorRuntime(bundle: Bundle(path: paths[0])!, support: URL(fileURLWithPath: paths[1]),
    sourceRoots: (URL(fileURLWithPath: paths[2]), URL(fileURLWithPath: paths[3])),
    legacyPlist: URL(fileURLWithPath: paths[4]), preferredPort: 18967)
func request(_ url: URL, token: String? = nil, body: [String: Any]? = nil, csrf: String? = nil) -> (Int, [String: Any]) {
    var value = URLRequest(url: url)
    value.timeoutInterval = 5
    if let token { value.setValue(token, forHTTPHeaderField: "X-Agent-Board-Access") }
    if let csrf { value.setValue(csrf, forHTTPHeaderField: "X-Agent-Board-Token") }
    if let body { value.httpMethod = "POST"; value.httpBody = try! JSONSerialization.data(withJSONObject: body); value.setValue("application/json", forHTTPHeaderField: "Content-Type") }
    let semaphore = DispatchSemaphore(value: 0)
    var result: (Int, [String: Any]) = (0, [:])
    URLSession.shared.dataTask(with: value) { data, response, _ in
        result = ((response as? HTTPURLResponse)?.statusCode ?? 0, data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:])
        semaphore.signal()
    }.resume()
    check(semaphore.wait(timeout: .now() + 7) == .success, "HTTP callback")
    return result
}
manager.start { result in
    guard case .success(let url) = result else { print(result); exit(1) }
    let firstProcess = manager.process!
    let firstToken = manager.accessToken
    check(firstToken.count == 64, "native token length")
    check(getpgid(firstProcess.processIdentifier) == firstProcess.processIdentifier, "owned collector process group")
    check(request(url.appendingPathComponent("api/state")).0 == 403, "state denied without native token")
    let snapshot = request(url.appendingPathComponent("api/state"), token: firstToken)
    check(snapshot.0 == 200, "authenticated state")
    let rows = snapshot.1["sessions"] as? [[String: Any]] ?? []
    check(rows.contains { $0["id"] as? String == "native-fixture" }, "fixture session read by packaged runtime")
    let rename = request(url.appendingPathComponent("api/sessions/codex:native-fixture/rename"), token: firstToken, body: ["title": "Saved native fixture"], csrf: snapshot.1["csrf_token"] as? String)
    check(rename.0 == 200, "authenticated rename")
    let attributes = try! FileManager.default.attributesOfItem(atPath: paths[1] + "/data")
    check((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o700, "private data directory")
    let tokens = try! FileManager.default.contentsOfDirectory(atPath: paths[1] + "/data").filter { $0.hasPrefix(".collector-access-") }
    check(tokens.count == 1, "one access file")
    let tokenAttributes = try! FileManager.default.attributesOfItem(atPath: paths[1] + "/data/" + tokens[0])
    check((tokenAttributes[.posixPermissions] as? NSNumber)?.intValue == 0o600, "private access file")
    manager.start { second in
        guard case .success(let nextURL) = second else { print(second); exit(1) }
        check(url == nextURL, "stable local origin after restart")
        check(!firstProcess.isRunning, "old collector stopped before restart")
        check(firstToken != manager.accessToken, "new access token after restart")
        check(request(nextURL.appendingPathComponent("api/state"), token: firstToken).0 == 403, "old access rejected")
        let after = request(nextURL.appendingPathComponent("api/state"), token: manager.accessToken)
        let sessions = after.1["sessions"] as? [[String: Any]] ?? []
        check(sessions.contains { $0["title"] as? String == "Saved native fixture" }, "saved board retained across native restart")
        manager.unexpectedExit = {
            check(manager.process == nil, "unexpected-exit cleanup")
            check((try! FileManager.default.contentsOfDirectory(atPath: paths[1] + "/data")).filter { $0.hasPrefix(".collector-access-") }.isEmpty, "access file removed on exit")
            print("PASS: packaged native launch, private auth, actual fixture read/rename, persistence, stable restart, owned group and unexpected-exit cleanup")
            exit(0)
        }
        manager.process!.terminate()
    }
}
DispatchQueue.main.asyncAfter(deadline: .now() + 40) { manager.stop(); print("FAIL: native runtime timeout"); exit(1) }
RunLoop.main.run()
'''.replace("__PATHS__", encoded)
    runner = QA / "main.swift"
    runner.write_text(swift)
    binary = QA / "native-runtime-smoke"
    subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(ROOT / "build/swift-cache"), str(runner), str(ROOT / "CollectorRuntime.swift"), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=50)
    group_cleanup_smoke()


def group_cleanup_smoke():
    """An owned helper that ignores SIGTERM must not survive the native stop."""
    fixture = QA / "helper-fixture.app"
    executable = fixture / "Contents/Helpers/AgentBoardCollector.app/Contents/MacOS/agent-board-collector"
    executable.parent.mkdir(parents=True, exist_ok=True)
    (fixture / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "local.agentboard.fixture"}))
    helper = QA / "group-helper.py"
    helper.write_text('''import argparse,json,os,signal,subprocess,sys,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer
p=argparse.ArgumentParser();p.add_argument('--data-dir');p.add_argument('--port',type=int);p.add_argument('--instance-id');p.add_argument('--access-token-file');a,_=p.parse_known_args()
if os.getpgid(0)!=os.getpid(): os.setsid()
child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
Path(a.data_dir,'fixture-child.pid').write_text(str(child.pid))
token=Path(a.access_token_file).read_text()
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  body=json.dumps({'app':'agent-board','instance_id':a.instance_id}).encode();self.send_response(200 if self.headers.get('X-Agent-Board-Access')==token else 403);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*args): pass
HTTPServer(('127.0.0.1',a.port),Handler).serve_forever()
''')
    executable.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(helper)) + ' "$@"\n')
    executable.chmod(0o755)
    support = QA / "group-support"
    if support.exists():
        shutil.rmtree(support)
    encoded = ", ".join(json.dumps(str(path)) for path in (fixture, support, QA / "no-legacy.plist"))
    swift = '''import Foundation
import Darwin
let paths: [String] = [__PATHS__]
let manager = CollectorRuntime(bundle: Bundle(path: paths[0])!, support: URL(fileURLWithPath: paths[1]), legacyPlist: URL(fileURLWithPath: paths[2]), preferredPort: 18968)
manager.start { result in
    guard case .success = result else { print(result); exit(1) }
    let parent = manager.process!.processIdentifier
    let child = Int32(try! String(contentsOfFile: paths[1] + "/data/fixture-child.pid", encoding: .utf8))!
    guard getpgid(parent) == parent, getpgid(child) == parent, parent != getpgrp() else { manager.stop(); print("FAIL: fixture group isolation"); exit(1) }
    manager.stop()
    DispatchQueue.main.asyncAfter(deadline: .now() + 9) {
        if kill(child, 0) == 0 { kill(child, SIGKILL); print("FAIL: owned helper survived native stop"); exit(1) }
        print("PASS: native stop terminates collector group and a helper that ignores SIGTERM")
        exit(0)
    }
}
DispatchQueue.main.asyncAfter(deadline: .now() + 30) { manager.stop(); print("FAIL: group cleanup timeout"); exit(1) }
RunLoop.main.run()
'''.replace("__PATHS__", encoded)
    runner = QA / "main.swift"
    runner.write_text(swift)
    binary = QA / "native-group-smoke"
    subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(ROOT / "build/swift-cache"), str(runner), str(ROOT / "CollectorRuntime.swift"), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=35)


if __name__ == "__main__":
    main()
