"""Exercise the production preference and toolbar bridges in native WebKit.

Only a throwaway localhost page, fake in-memory defaults and nonpersistent
WebKit are used. No owner app, source grant, provider or account is accessed.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import subprocess
import threading
import uuid


ROOT = Path(__file__).resolve().parents[1]


def method(source, name):
    start = re.search(r"^    (?:@objc )?func " + re.escape(name) + r"\(", source, re.M)
    assert start, name
    end = re.search(r"^    (?:@objc )?func |^}", source[start.end():], re.M)
    assert end, name
    return source[start.start():start.end() + end.start()].rstrip()


class Page(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"<!doctype html><html><body>Disposable local preference fixture.</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    source = (ROOT / "MacApp.swift").read_text()
    methods = "\n\n".join(method(source, name) for name in ("installAccessScript", "userContentController", "publishFullscreenState"))
    key = re.search(r"^    var displayPreferencesKey:.*$", source, re.M).group(0)
    sounds = re.search(r"^enum AgentBoardAlertSound \{.*?^}", source, re.M | re.S).group(0)
    qa = ROOT / ".local/native-sample-preferences-qa" / uuid.uuid4().hex
    qa.mkdir(parents=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        program = r'''import AppKit
import WebKit

func check(_ value: @autoclosure () -> Bool, _ message: String) {
    if !value() { print("FAIL: " + message); exit(1) }
}
final class Defaults {
    var values: [String: [String: String]] = [:]
    func dictionary(forKey key: String) -> [String: Any]? { values[key] }
    func set(_ value: [String: String], forKey key: String) { values[key] = value }
}
final class Runtime { var storeDistribution = true; var sandboxed = true; var sampleMode = false; let accessToken = "disposable-token" }
final class Window {
    var appearance: NSAppearance?
    var styleMask: NSWindow.StyleMask = []
}
__SOUNDS__
final class Model: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
    let defaults = Defaults()
    let runtime = Runtime()
    let fixtureMode = false
    var startingCollector = false
    var sampleModeSelected: Bool { runtime.sampleMode }
    var stateGeneration = UUID()
    let boardURL = URL(string: CommandLine.arguments[1])!
    let window = Window()
    var webView: WKWebView!
    var stage = 0
    var oldGeneration = ""
    var opens = 0
    var setups = 0
    var fullscreenRequests = 0
    var sampleRequests = 0
    func setBoardMode(sample: Bool) { if sample { sampleRequests += 1 } }
    func openExistingChat(_ value: String, provider: String) { opens += 1 }
    func showSetup() { setups += 1 }
    func toggleFullscreen() { fullscreenRequests += 1 }
__KEY__
__METHODS__
    func load(sample: Bool) {
        runtime.sampleMode = sample
        oldGeneration = stateGeneration.uuidString
        stateGeneration = UUID()
        installAccessScript()
        webView.load(URLRequest(url: boardURL))
    }
    func value(_ body: String, _ done: @escaping (Any?) -> Void) {
        webView.evaluateJavaScript(body) { result, error in
            if let error { print("FAIL: fixture JavaScript stage \(self.stage): \(error)"); exit(1) }
            done(result)
        }
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) { self.next() }
    }
    func next() {
        switch stage {
        case 0:
            value("localStorage.getItem('agent-board:theme-id')") { result in
                check(result is NSNull, "initial live board has no inherited preference")
                self.value("localStorage.setItem('agent-board:theme-id','matrix')") { _ in
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
                        check(self.defaults.values["displayPreferences"]?["agent-board:theme-id"] == "matrix", "live preference persisted")
                        self.stage = 1; self.load(sample: true)
                    }
                }
            }
        case 1:
            value("localStorage.getItem('agent-board:theme-id')") { result in
                check(result is NSNull, "first sample board clears same-origin live preference")
                let stale = "window.webkit.messageHandlers.agentBoard.postMessage({kind:'displayPreferences',generation:'" + self.oldGeneration + "',values:{'agent-board:theme-id':'lava'}})"
                self.value(stale + "; true") { _ in
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
                        check(self.defaults.values["sampleDisplayPreferences"]?["agent-board:theme-id"] == nil, "late old-generation preference message rejected")
                        self.value("localStorage.setItem('agent-board:theme-id','hearth')") { _ in
                            DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
                                check(self.defaults.values["sampleDisplayPreferences"]?["agent-board:theme-id"] == "hearth", "sample preference persisted separately")
                                self.stage = 2; self.load(sample: false)
                            }
                        }
                    }
                }
            }
        case 2:
            value("localStorage.getItem('agent-board:theme-id')") { result in
                check(result as? String == "matrix", "return restores exact saved live preference")
                check(self.defaults.values["sampleDisplayPreferences"]?["agent-board:theme-id"] == "hearth", "live return preserves sample preference")
                self.stage = 3; self.load(sample: true)
            }
        case 3:
            value("localStorage.getItem('agent-board:theme-id')") { result in
                check(result as? String == "hearth", "sample preference survives return")
                self.defaults.values.removeValue(forKey: "displayPreferences")
                self.stage = 4; self.load(sample: false)
            }
        case 4:
            value("localStorage.getItem('agent-board:theme-id')") { result in
                check(result is NSNull, "first live return with no saved map clears sample preference")
                check(self.opens == 0, "preference workflow requested no provider open")
                self.value("""
                    window.fullscreenTransitions = [];
                    window.addEventListener('agentboard:fullscreenchange', event => window.fullscreenTransitions.push(event.detail));
                    window.webkit.messageHandlers.agentBoard.postMessage('settings');
                    window.webkit.messageHandlers.agentBoard.postMessage('fullscreen');
                    window.webkit.messageHandlers.agentBoard.postMessage('sampleBoard');
                    true;
                    """) { _ in
                    self.publishFullscreenState()
                    self.window.styleMask = [.fullScreen]
                    self.publishFullscreenState()
                    self.window.styleMask = []
                    self.publishFullscreenState()
                    self.value("JSON.stringify(window.fullscreenTransitions)") { result in
                        check(result as? String == "[false,true,false]", "native window state reaches WebKit as booleans")
                        check(self.fullscreenRequests == 1, "board fullscreen request reaches native window")
                        check(self.setups == 1, "board settings request reaches native setup")
                        check(self.sampleRequests == 1, "empty-board sample request reaches Store mode action")
                        self.stage = 5
                        self.webView.load(URLRequest(url: self.boardURL.appendingPathComponent("untrusted")))
                    }
                }
            }
        default:
            value("window.webkit.messageHandlers.agentBoard.postMessage('settings'); window.webkit.messageHandlers.agentBoard.postMessage('sampleBoard'); true") { _ in
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
                    check(self.setups == 1, "settings is rejected outside the board path on the same origin")
                    check(self.sampleRequests == 1, "sample request is rejected outside the board root")
                    print("PASS: native WebKit preference isolation, saved-mode restore, stale-generation rejection, and toolbar bridge")
                    self.webView.configuration.userContentController.removeScriptMessageHandler(forName: "agentBoard")
                    exit(0)
                }
            }
        }
    }
}
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let model = Model()
let configuration = WKWebViewConfiguration()
configuration.websiteDataStore = .nonPersistent()
configuration.userContentController.add(model, name: "agentBoard")
model.webView = WKWebView(frame: .zero, configuration: configuration)
model.webView.navigationDelegate = model
model.load(sample: false)
DispatchQueue.main.asyncAfter(deadline: .now() + 12) { print("FAIL: native preference workflow timed out"); exit(1) }
RunLoop.main.run()
'''.replace("__KEY__", key).replace("__SOUNDS__", sounds).replace("__METHODS__", methods)
        path = qa / "main.swift"
        path.write_text(program)
        binary = qa / "native-sample-preferences-smoke"
        subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(qa / "swift-cache"), str(path), "-o", str(binary)], check=True)
        subprocess.run([str(binary), f"http://127.0.0.1:{server.server_port}/"], check=True, timeout=20)
        print("Evidence:", qa)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


if __name__ == "__main__":
    main()
