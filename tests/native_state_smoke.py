"""Test production native callbacks against delayed loopback fixture responses.

This extracts the actual request/poll/action methods from MacApp.swift and uses
Foundation URLSession with UI side effects replaced by counters. It does not
launch the installed app, show permissions or access any provider account.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.get_count += 1
        time.sleep(self.server.delay)
        self.send_response(self.server.fixture_status)
        self.end_headers()
        self.wfile.write(json.dumps({"sessions": [], "csrf_token": "fixture", "version": self.server.version}).encode())

    def do_POST(self):
        self.server.post_count += 1
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        time.sleep(self.server.delay)
        opening = self.path.endswith("/open")
        self.send_response(200 if opening else 503)
        self.end_headers()
        body = ({"ok": True} if self.path.endswith("/missing-link/open") else
                {"native_open_url": "codex://threads/11111111-1111-4111-8111-111111111111", "provider": "codex"}) if opening else {
                    "error": "stale fixture error"}
        self.wfile.write(json.dumps(body).encode())


def main():
    source = (ROOT / "MacApp.swift").read_text()
    methods = source[source.index("    func fetchState("):source.index("    func applySnapshot(")]
    methods += source[source.index("    func performAction("):source.index("    @objc func openNextChat(")]
    servers = []
    try:
        for version, delay in (("old", .65), ("new", .01)):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
            server.version, server.delay, server.fixture_status = version, delay, 200
            server.get_count = server.post_count = 0
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
        program = '''import Foundation
final class Runtime { var accessToken = "fixture"; var sandboxed = true; var sampleMode = false }
final class Defaults { func bool(forKey key: String) -> Bool { true } }
final class NativeFixture {
    var boardURL: URL
    let fixtureMode = true
    let runtime = Runtime()
    let defaults = Defaults()
    let session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.connectionProxyDictionary = [:]
        return URLSession(configuration: configuration)
    }()
    var stateGeneration = UUID()
    var polling = false
    var startingCollector = false
    var ownedCollectorRunning = true
    var collectorAvailable = true
    var csrfToken: String? = "fixture"
    var applied: [String] = []
    var errors: [String] = []
    var opened: [String] = []
    init(_ url: URL) { boardURL = url }
    func applySnapshot(_ value: [String: Any]) { applied.append(value["version"] as! String); collectorAvailable = true }
    func updateMenu() {}
    func showActionError(_ value: String) { errors.append(value) }
    func openExistingChat(_ value: String, provider: String) { opened.append(provider + "|" + value) }
__METHODS__
}
let urls = CommandLine.arguments.dropFirst().map { URL(string: $0)! }
let model = NativeFixture(urls[0])
model.poll()
model.performAction("snooze", key: "fixture", eventID: "event-1")
DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
    model.stateGeneration = UUID()
    model.boardURL = urls[1]
    model.polling = false
    model.poll()
}
DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
    if model.applied != ["new"] || !model.collectorAvailable || !model.errors.isEmpty {
        print("FAIL: stale native response crossed restart; applied=\\(model.applied), errors=\\(model.errors)")
        exit(1)
    }
    model.ownedCollectorRunning = false
    model.poll()
    model.performAction("snooze", key: "private-key", eventID: "private-event")
}
DispatchQueue.main.asyncAfter(deadline: .now() + 1.3) {
    if model.applied != ["new"] || model.errors != ["The local collector is unavailable. Retry to resume monitoring."] {
        print("FAIL: native callback or dead-collector guard")
        exit(1)
    }
    model.ownedCollectorRunning = true
    model.errors.removeAll()
    model.performAction("open", key: "valid-chat", eventID: "open-event")
}
DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) {
    if model.opened != ["codex|codex://threads/11111111-1111-4111-8111-111111111111"] || !model.errors.isEmpty {
        print("FAIL: sandbox conversation handoff was not recorded")
        exit(1)
    }
    model.performAction("open", key: "missing-link", eventID: "missing-event")
}
DispatchQueue.main.asyncAfter(deadline: .now() + 1.9) {
    if model.opened.count != 1 || model.errors != ["The conversation link is unavailable. Open the provider app directly."] {
        print("FAIL: missing sandbox conversation link was not handled")
        exit(1)
    }
    print("PASS: stale responses discarded; dead-collector guard and sandbox link handoff/error handled without opening apps")
    exit(0)
}
RunLoop.main.run()
'''.replace("__METHODS__", methods)
        qa = ROOT / ".local/native-state-qa" / uuid.uuid4().hex
        qa.mkdir(parents=True, exist_ok=False)
        path = qa / "main.swift"
        path.write_text(program)
        binary = qa / "native-state-smoke"
        subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(qa / "swift-cache"), str(path), "-o", str(binary)], check=True)
        urls = ["http://127.0.0.1:" + str(server.server_port) + "/" for server in servers]
        for old_status in (200, 503):
            for server in servers:
                server.get_count = server.post_count = 0
            servers[0].fixture_status = old_status
            subprocess.run([str(binary), *urls], check=True, timeout=10)
            assert servers[1].get_count == 3 and servers[1].post_count == 2
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
