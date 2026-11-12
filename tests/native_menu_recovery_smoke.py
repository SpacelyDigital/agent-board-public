"""Compile production native menu/configuration/recovery methods in a hidden harness.

No collector, provider app, permission prompt, real user defaults or external
log viewer is opened. Zoom uses a real hidden nonpersistent WKWebView with
production-extracted methods and fake local preferences. Its owned HTML layout
is not the product board. Actual foreground keyboard routing, product reflow,
VoiceOver, process-restart persistence and the startup alert remain GUI checks.
"""

from pathlib import Path
import re
import subprocess
import uuid


ROOT = Path(__file__).resolve().parents[1]


def method(source, name):
    start = re.search(r"^    (?:@objc )?func " + re.escape(name) + r"\(", source, re.M)
    assert start, name
    end = re.search(r"^    (?:@objc )?func |^}", source[start.end():], re.M)
    assert end, name
    return source[start.start():start.end() + end.start()].rstrip()


def main():
    source = (ROOT / "MacApp.swift").read_text()
    methods = "\n\n".join(method(source, name) for name in (
        "item", "addNotificationItems", "buildMainMenu", "updateMenu",
        "changePageZoom", "validateMenuItem",
        "showCollectorFailurePage", "showSetupPendingPage", "retryMonitoring", "handleStartupErrorChoice"))
    configuration = source[source.index("        let config = WKWebViewConfiguration()"):
                           source.index("        webView = WKWebView(frame:")]
    zoom_preferences = source[source.index("    let pageZoomLevels ="):
                              source.index("    var sampleModeSelected:")]
    zoom_initialization = re.search(
        r"^        webView\.pageZoom = CGFloat\(pageZoomPercent\) / 100$", source, re.M).group(0)
    folder_method = method(source, "grantFolder")
    folder_configuration = folder_method[folder_method.index("        let panel = NSOpenPanel()"):
                                         folder_method.index("        guard panel.runModal()")]
    qa = ROOT / ".local/native-menu-recovery-qa" / uuid.uuid4().hex
    qa.mkdir(parents=True)
    swift = r'''import AppKit
import WebKit
import UserNotifications

func check(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() { print("FAIL: " + message); exit(1) }
}
struct Chat { var eligible = true; var canOpen = false }
final class TestDefaults {
    var setupComplete = true
    var integers: [String: Int] = [:]
    func bool(forKey key: String) -> Bool { key == "setupCompletedV1" && setupComplete }
    func integer(forKey key: String) -> Int { integers[key] ?? 0 }
    func set(_ value: Int, forKey key: String) { integers[key] = value }
}
final class Runtime { var needsLegacyMigration = false; var sampleMode = false; var storeDistribution = true }
final class StatusItem { let button: NSButton? = NSButton(title: "", target: nil, action: nil) }
final class KeyRecorder: NSObject, NSMenuItemValidation {
    var calls: [String] = []
    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool { true }
    @objc func performClose(_ sender: Any?) { calls.append("close") }
    @objc func performMiniaturize(_ sender: Any?) { calls.append("minimize") }
    @objc func performZoom(_ sender: Any?) { calls.append("zoom") }
    @objc func toggleFullScreen(_ sender: Any?) { calls.append("fullscreen") }
}
final class Harness: NSObject, WKScriptMessageHandler, WKNavigationDelegate, NSMenuItemValidation {
    var setupMenuItem: NSMenuItem?
    var sampleBoardItem: NSMenuItem?
    var nextChatItems: [NSMenuItem] = []
    var notificationToggleItems: [NSMenuItem] = []
    var notificationStatusItems: [NSMenuItem] = []
    var testNotificationItems: [NSMenuItem] = []
    var connectionItem: NSMenuItem? = NSMenuItem()
    var startingCollector = false
    var startupModeError: Error?
    var sampleModeSelected: Bool { runtime.sampleMode || startupModeError != nil }
    var chats: [Chat] = []
    var statusItem: StatusItem? = StatusItem()
    var receivedInitialSnapshot = false
    var collectorAvailable = false
    var notificationsEnabled = false
    var permissionAllowsNotifications = false
    var requestingPermission = false
    var lastDeliveryFailed = false
    var permission: UNAuthorizationStatus = .notDetermined
    let defaults: TestDefaults
    init(defaults: TestDefaults = TestDefaults()) {
        self.defaults = defaults
        super.init()
    }
__ZOOM_PREFERENCES__
    var webView: WKWebView!
    var pageLoaded = false
    func makeZoomView() {
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "agentBoard")
        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 540, height: 440), configuration: makeConfiguration())
__ZOOM_INITIALIZATION__
        webView.navigationDelegate = self
        window.contentView = webView
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { pageLoaded = true }
    func loadZoomLayout() {
        pageLoaded = false
        webView.loadHTMLString("<!doctype html><meta http-equiv='Content-Security-Policy' content=\"default-src 'none';style-src 'unsafe-inline'\"><style>*{box-sizing:border-box}body{margin:0;padding:12px;font:16px -apple-system}main{max-width:100%;overflow-wrap:anywhere}button{max-width:100%;font:inherit}</style><main><h1>Local zoom fixture</h1><p>A disposable paragraph wraps in this real WebKit viewport.</p><button id='zoom-target'>Local target</button></main>", baseURL: nil)
        let deadline = Date(timeIntervalSinceNow: 8)
        while !pageLoaded && Date() < deadline { RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.01)) }
        check(pageLoaded, "owned zoom layout finished loading")
    }
    func script(_ body: String) -> Any? {
        var finished = false
        var result: Any?
        var failure: Error?
        webView.evaluateJavaScript(body) { value, error in result = value; failure = error; finished = true }
        let deadline = Date(timeIntervalSinceNow: 8)
        while !finished && Date() < deadline { RunLoop.current.run(until: Date(timeIntervalSinceNow: 0.01)) }
        check(finished && failure == nil, "owned WebKit layout query completed")
        return result
    }
    let runtime = Runtime()
    var window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 540, height: 440),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
    var collectorRetryButton: NSButton?
    var starts = 0
    var setups = 0
    var logOpens = 0
    @objc func showAbout() {}
    @objc func showSetup() { setups += 1 }
    var samples = 0
    @objc func toggleSampleBoard() { samples += 1 }
    @objc func openLocalData() {}
    @objc func showBoard() {}
    @objc func openNextChat() {}
    @objc func toggleNotifications() {}
    @objc func sendTestNotification() {}
    @objc func reload() {}
    @objc func toggleOnTop(_ sender: NSMenuItem) {}
    func startCollector() { starts += 1; startingCollector = true }
    func openCollectorLog() { logOpens += 1 }
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {}
    func makeConfiguration() -> WKWebViewConfiguration {
__CONFIGURATION__
        return config
    }
    func makeFolderPanel(name: String, expected: URL) -> NSOpenPanel {
__FOLDER_CONFIGURATION__
        return panel
    }
__METHODS__
}

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let harness = Harness()
let config = harness.makeConfiguration()
check(config.preferences.tabFocusesLinks, "production WebKit Tab navigation enabled")
check(!config.websiteDataStore.isPersistent, "production WebKit data remains nonpersistent")
for (name, relative) in [("Codex", ".codex"), ("Claude Sessions", ".claude"),
                         ("Claude Code sessions", "Library/Application Support/Claude/claude-code-sessions")] {
    let expected = URL(fileURLWithPath: "/Users/sample-user", isDirectory: true).appendingPathComponent(relative)
    let panel = harness.makeFolderPanel(name: name, expected: expected)
    check(panel.message.contains(expected.path), "source picker preserves the exact expected folder")
    check(panel.message.contains("read-only") && panel.message.contains("session metadata and previews"),
          "source picker retains access and content disclosure")
    let widths = panel.message.split(separator: "\n").map {
        (String($0) as NSString).size(withAttributes: [.font: NSFont.systemFont(ofSize: 13)]).width
    }
    check(widths.max()! < 500, "source picker message fits the disposable guest's display")
    check(panel.prompt == "Allow Folder Access" && panel.canChooseDirectories && !panel.canChooseFiles
          && !panel.allowsMultipleSelection && panel.showsHiddenFiles && panel.directoryURL == expected,
          "source picker configuration preserves single-folder selection")
}
harness.buildMainMenu()
func entry(_ menu: String, _ title: String) -> NSMenuItem {
    let submenu = app.mainMenu!.items.compactMap { $0.submenu }.first { $0.title == menu }!
    return submenu.items.first { $0.title == title }!
}
let close = entry("File", "Close")
let minimize = entry("Window", "Minimize")
let zoom = entry("Window", "Zoom")
let fullscreen = entry("View", "Enter Full Screen")
check(close.target == nil && minimize.target == nil && zoom.target == nil && fullscreen.target == nil,
      "window actions use the responder chain")
check(app.windowsMenu === zoom.menu, "standard Window menu registered")
let recorder = KeyRecorder()
for item in [close, minimize, zoom, fullscreen] { item.target = recorder }
func key(_ value: String, _ flags: NSEvent.ModifierFlags, code: UInt16) -> Bool {
    let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: flags,
                                timestamp: 0, windowNumber: 0, context: nil,
                                characters: value, charactersIgnoringModifiers: value,
                                isARepeat: false, keyCode: code)!
    return app.mainMenu!.performKeyEquivalent(with: event)
}
check(key("w", .command, code: 13) && recorder.calls == ["close"], "Command-W dispatches Close")
check(key("m", .command, code: 46) && recorder.calls.last == "minimize", "Command-M dispatches Minimize")
check(!key("f", .command, code: 3) && recorder.calls.last == "minimize", "Command-F is not stolen by fullscreen")
check(key("f", [.control, .command], code: 3) && recorder.calls.last == "fullscreen", "Control-Command-F dispatches fullscreen")
check(app.sendAction(zoom.action!, to: recorder, from: zoom) && recorder.calls.last == "zoom", "Zoom selector dispatches")

// These are real menu validation and WKWebView property/layout checks. The
// owned HTML is deliberately small; it does not establish product200% reflow.
let appMenu = app.mainMenu!.items[0].submenu!
let viewMenu = entry("View", "Reload").menu!
check(!appMenu.autoenablesItems && viewMenu.autoenablesItems,
      "manual app-menu states and automatic View zoom validation remain distinct")
let zoomIn = entry("View", "Zoom In"), zoomOut = entry("View", "Zoom Out"), actualSize = entry("View", "Actual Size")
check(zoomIn.target === harness && zoomOut.target === harness && actualSize.target === harness,
      "zoom commands target production extracted methods")
check(zoomIn.keyEquivalent == "+" && zoomOut.keyEquivalent == "-" && actualSize.keyEquivalent == "0",
      "zoom command key equivalents are registered")
check(harness.pageZoomPercent == 100, "missing preference defaults to100")
for invalid in [-100, 0, 76, 101, 201, 10000] {
    harness.defaults.integers["pageZoomPercentV1"] = invalid
    check(harness.pageZoomPercent == 100, "invalid saved zoom defaults to100")
}
harness.defaults.integers.removeAll()
harness.makeZoomView()
harness.loadZoomLayout()
check(harness.webView.pageZoom == 1, "default applied to real WKWebView")
viewMenu.update()
check(!actualSize.isEnabled && zoomIn.isEnabled && zoomOut.isEnabled,
      "actual menu validation has default bounds and reset state")
func invokeZoom(_ item: NSMenuItem) {
    check(app.sendAction(item.action!, to: harness, from: item), "zoom selector dispatched to production method")
}
func inspectLayout(_ label: String) {
    let value = harness.script("({viewport:innerWidth,client:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth,right:document.getElementById('zoom-target').getBoundingClientRect().right,focus:document.activeElement.id})") as! [String: Any]
    let viewport = (value["viewport"] as! NSNumber).doubleValue
    let client = (value["client"] as! NSNumber).doubleValue
    let scroll = (value["scroll"] as! NSNumber).doubleValue
    let right = (value["right"] as! NSNumber).doubleValue
    check(viewport > 0 && client > 0 && scroll <= client + 1 && right <= viewport + 1,
          "real owned HTML remains reachable at" + label)
    check(value["focus"] as? String == "zoom-target", "zoom retains the real DOM focus target")
    print("ZOOM_LAYOUT " + label + " viewport=\(viewport) client=\(client) scroll=\(scroll) right=\(right)")
}
_ = harness.script("document.getElementById('zoom-target').focus(); true")
inspectLayout("100percent")
for expected in [125, 150, 175, 200] {
    invokeZoom(zoomIn)
    check(harness.pageZoomPercent == expected && harness.webView.pageZoom == CGFloat(expected) / 100,
          "zoom-in advances the real WebView and saved discrete factor")
}
viewMenu.update()
check(!zoomIn.isEnabled && zoomOut.isEnabled && actualSize.isEnabled, "actual menu update disables upper bound")
invokeZoom(zoomIn)
check(harness.webView.pageZoom == 2 && harness.pageZoomPercent == 200, "upper bound remains clamped")
inspectLayout("200percent")
let restoredZoom = Harness(defaults: harness.defaults)
restoredZoom.makeZoomView()
check(restoredZoom.webView.pageZoom == 2, "new real WebView restores shared fake saved preference")
restoredZoom.webView.configuration.userContentController.removeScriptMessageHandler(forName: "agentBoard")
for expected in [175, 150, 125, 100, 75] {
    invokeZoom(zoomOut)
    check(harness.pageZoomPercent == expected && harness.webView.pageZoom == CGFloat(expected) / 100,
          "zoom-out advances the real WebView and saved discrete factor")
}
viewMenu.update()
check(!zoomOut.isEnabled && zoomIn.isEnabled && actualSize.isEnabled, "actual menu update disables lower bound")
invokeZoom(zoomOut)
check(harness.webView.pageZoom == 0.75 && harness.pageZoomPercent == 75, "lower bound remains clamped")
invokeZoom(actualSize)
viewMenu.update()
check(harness.webView.pageZoom == 1 && harness.pageZoomPercent == 100 && !actualSize.isEnabled,
      "Actual Size restores100and disables reset through actual menu update")
harness.runtime.sampleMode = true
harness.updateMenu()
appMenu.update()
check(!harness.setupMenuItem!.isEnabled && !harness.notificationToggleItems[0].isEnabled
      && !harness.testNotificationItems[0].isEnabled,
      "actual app menu update preserves Sample-disabled setup and notifications")
harness.runtime.sampleMode = false
harness.collectorAvailable = false
harness.chats = [Chat(eligible: true, canOpen: true)]
harness.updateMenu()
appMenu.update()
check(!harness.nextChatItems[0].isEnabled, "actual app menu update preserves disconnected Open Next Chat")
harness.startingCollector = true
harness.updateMenu()
appMenu.update()
check(!harness.setupMenuItem!.isEnabled && !harness.sampleBoardItem!.isEnabled,
      "actual app menu update preserves starting-disabled setup and board switch")
harness.startingCollector = false
harness.chats = []
harness.updateMenu()
harness.webView.configuration.userContentController.removeScriptMessageHandler(forName: "agentBoard")

harness.updateMenu()
check(harness.statusItem!.button!.accessibilityLabel()!.contains("connecting"), "initial AX count is not presented as live")
harness.chats = [Chat()]
harness.receivedInitialSnapshot = true
harness.collectorAvailable = true
harness.updateMenu()
check(harness.statusItem!.button!.accessibilityLabel() == "Agent Board: 1 conversation needs review.", "live singular AX count")
harness.collectorAvailable = false
harness.updateMenu()
let offlineLabel = harness.statusItem!.button!.accessibilityLabel()!
check(offlineLabel.contains("unavailable") && offlineLabel.contains("Last known: 1 conversation needs review."), "offline AX count discloses uncertainty")
let error = NSError(domain: "DisposableFixture", code: 1,
                    userInfo: [NSLocalizedDescriptionKey: "The disposable local collector stopped. Its saved board remains available."])
harness.showCollectorFailurePage(error)
let retry = harness.collectorRetryButton!
check(retry.isDescendant(of: harness.window.contentView!), "Retry remains in the native failure page")
check(retry.keyEquivalent == "\r", "Return can activate native Retry")
harness.handleStartupErrorChoice(.alertSecondButtonReturn)
check(harness.logOpens == 1 && harness.starts == 0, "Open Local Log does not silently restart")
check(retry.isDescendant(of: harness.window.contentView!), "Open Local Log leaves Retry available")
retry.performClick(nil)
check(harness.starts == 1, "persistent Retry invokes monitoring recovery")
retry.performClick(nil)
check(harness.starts == 1, "Retry cannot start a second collector while starting")
harness.startingCollector = false
harness.defaults.setupComplete = false
harness.showCollectorFailurePage(error)
harness.collectorRetryButton!.performClick(nil)
check(harness.setups == 1 && harness.starts == 1, "Retry respects incomplete setup")
harness.defaults.setupComplete = true
harness.runtime.needsLegacyMigration = true
harness.collectorRetryButton!.performClick(nil)
check(harness.setups == 2 && harness.starts == 1, "Retry respects required legacy setup")
harness.defaults.setupComplete = false
harness.runtime.needsLegacyMigration = false
harness.runtime.storeDistribution = true
harness.showSetupPendingPage()
let pendingStack = harness.window.contentView!.subviews.compactMap { $0 as? NSStackView }.first!
let pendingButtons = pendingStack.arrangedSubviews.compactMap { $0 as? NSButton }
check(pendingButtons.map { $0.title } == ["Set Up Monitoring…", "Try Sample Board"], "cancel leaves setup and Store sample actions available")
check(!harness.defaults.setupComplete && harness.starts == 1, "cancel page does not accept setup or start a collector")
pendingButtons[0].performClick(nil)
check(harness.setups == 3, "cancel page can reopen Setup")
pendingButtons[1].performClick(nil)
check(harness.samples == 1, "cancel page can enter Sample Board")
check(app.mainMenu!.items.compactMap { $0.submenu }.flatMap { $0.items }.contains {
    $0.title == "Quit Agent Board" && $0.action == #selector(NSApplication.terminate(_:))
}, "Quit remains reachable after setup cancellation")
harness.runtime.storeDistribution = false
harness.showSetupPendingPage()
let directStack = harness.window.contentView!.subviews.compactMap { $0 as? NSStackView }.first!
check(directStack.arrangedSubviews.compactMap { $0 as? NSButton }.map { $0.title } == ["Set Up Monitoring…"], "direct cancel page does not advertise Store sample mode")
config.userContentController.removeScriptMessageHandler(forName: "agentBoard")
print("PASS: compiled source-picker/menu recovery, real WebKit zoom bounds/layout/fake-preference restore, preserved manual menu eligibility and native configuration")
'''.replace("__CONFIGURATION__", configuration.rstrip()).replace("__FOLDER_CONFIGURATION__", folder_configuration.rstrip()).replace("__METHODS__", methods)
    swift = swift.replace("__ZOOM_PREFERENCES__", zoom_preferences.rstrip()).replace(
        "__ZOOM_INITIALIZATION__", zoom_initialization)
    runner = qa / "main.swift"
    runner.write_text(swift)
    binary = qa / "native-menu-recovery-smoke"
    subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(qa / "swift-cache"),
                    "-framework", "AppKit", "-framework", "WebKit", "-framework", "UserNotifications",
                    str(runner), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=60)


if __name__ == "__main__":
    main()
