"""Render trusted local status documents in a disposable native WebKit view.

The hidden fixture uses nonpersistent WebKit storage. It opens no owner app,
browser, provider account or external URL and displays no window or OS dialog.
"""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / "MacApp.swift").read_text()
    policy = source[source.index("    func webView(_ webView: WKWebView, decidePolicyFor"):source.index("    func webView(_ webView: WKWebView, createWebViewWith")]
    program = '''import AppKit
import WebKit
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
struct Runtime { var storeDistribution = true }
final class Model: NSObject, WKNavigationDelegate {
    var runtime = Runtime()
    var startingCollector = true
    let boardURL = URL(string: "http://127.0.0.1:18969/")!
    var finished = 0
    func handleKoFiNavigation(_ action: WKNavigationAction) -> Bool {
        if action.request.url?.host == "ko-fi.com" {
            print("FAIL: storefront flow is outside the status-document fixture"); exit(1)
        }
        return false
    }
__POLICY__
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { finished += 1 }
}
let model = Model()
let supportCases: [(String, Bool)] = [
    ("mailto:Spacelydigital@gmail.com", true),
    ("mailto:spacelydigital@gmail.com?body=private", false),
    ("mailto:spacelydigital@gmail.com?attachment=/private/file", false),
    ("mailto:another@example.com", false),
    ("https://ko-fi.com/spacelydigital", false),
    ("https://example.com", false)
]
for (address, expected) in supportCases {
    if model.isSupportURL(URL(string: address)!) != expected {
        print("FAIL: support destination policy"); exit(1)
    }
}
model.runtime.storeDistribution = false
if !model.isSupportURL(URL(string: "https://ko-fi.com/spacelydigital")!) {
    print("FAIL: direct-download support destination"); exit(1)
}
model.runtime.storeDistribution = true
let configuration = WKWebViewConfiguration()
configuration.websiteDataStore = .nonPersistent()
let web = WKWebView(frame: .zero, configuration: configuration)
web.navigationDelegate = model
web.loadHTMLString("<!doctype html><html><body>Starting fixture.</body></html>", baseURL: nil)
DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
    if model.finished != 1 { print("FAIL: starting local status document was blocked"); exit(1) }
    model.startingCollector = false
    web.loadHTMLString("<!doctype html><html><body>Stopped fixture.</body></html>", baseURL: nil)
}
DispatchQueue.main.asyncAfter(deadline: .now() + 4) {
    if model.finished != 2 { print("FAIL: unexpected-exit local status document was blocked"); exit(1) }
    web.evaluateJavaScript("document.body.textContent") { value, error in
        if error != nil || value as? String != "Stopped fixture." { print("FAIL: stale dashboard was not replaced"); exit(1) }
        print("PASS: starting and stopped status documents rendered through actual native navigation policy")
        exit(0)
    }
}
RunLoop.main.run()
'''.replace("__POLICY__", policy)
    qa = ROOT / ".local/native-navigation-qa"
    qa.mkdir(parents=True, exist_ok=True)
    path, binary = qa / "main.swift", qa / "native-navigation-smoke"
    path.write_text(program)
    subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(ROOT / "build/swift-cache"), str(path), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=15)


if __name__ == "__main__":
    main()
