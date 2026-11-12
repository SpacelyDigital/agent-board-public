"""Compile native sample/live transitions and login handling without OS grants.

The AppDelegate harness uses fake local services and never presents its alert.
A separate production-runtime probe stops/reaps only its own /bin/sleep child.
No installed app, provider, notification, login registration or account is used.
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


def compile_and_run(directory, program, *, runtime=False, compiler_flags=(), arguments=()):
    directory.mkdir()
    source = directory / "main.swift"
    source.write_text(program)
    binary = directory / "native-smoke"
    command = ["/usr/bin/swiftc", *compiler_flags, "-module-cache-path", str(directory / "swift-cache"),
               str(source)]
    if runtime:
        command.append(str(ROOT / "CollectorRuntime.swift"))
    command += ["-o", str(binary)]
    subprocess.run(command, check=True)
    subprocess.run([str(binary), *arguments], check=True, timeout=20)


def main():
    source = (ROOT / "MacApp.swift").read_text()
    login = source[source.index("protocol AgentBoardLoginService"):source.index("struct AttentionChat")]
    ledgers = source[source.index("    var sampleModeSelected:"):source.index("    var notificationsEnabled:")]
    methods = "\n\n".join(method(source, name) for name in ("invalidateStateRequests", "setBoardMode", "startCollector", "poll"))
    usage = method(source, "connectClaudeUsage").split("\n    #endif", 1)[0].replace("NSAlert()", "UsageAlert()")
    usage = usage.replace("NSPasteboard.general", "UsagePasteboard.general").replace("NSWorkspace.shared", "UsageWorkspace.shared")
    setup = method(source, "showSetup")
    buttons = setup[setup.index("        let alert = NSAlert()"):setup.index("        let content = NSStackView()")]
    qa = ROOT / ".local/native-sample-login-qa" / uuid.uuid4().hex
    qa.mkdir(parents=True)
    program = r'''import AppKit
import ServiceManagement

func check(_ value: @autoclosure () -> Bool, _ message: String) {
    if !value() { print("FAIL: " + message); exit(1) }
}
__LOGIN__
final class LoginService: AgentBoardLoginService {
    var value: SMAppService.Status
    var afterRegister: SMAppService.Status = .enabled
    var afterUnregister: SMAppService.Status = .notRegistered
    var registers = 0
    var unregisters = 0
    var reads = 0
    var failure: NSError?
    init(_ status: SMAppService.Status) { value = status }
    var status: SMAppService.Status { reads += 1; return value }
    func register() throws {
        registers += 1
        if let failure { throw failure }
        value = afterRegister
    }
    func unregister() throws {
        unregisters += 1
        if let failure { throw failure }
        value = afterUnregister
    }
}
let pending = LoginService(.notRegistered)
pending.afterRegister = .requiresApproval
check(try! updateLoginPreference(enabled: true, service: pending) == .requiresApproval,
      "new registration returns pending approval")
check(pending.registers == 1 && pending.reads == 2, "registration rereads actual service status")
let existing = LoginService(.enabled)
check(try! updateLoginPreference(enabled: true, service: existing) == .enabled && existing.registers == 0,
      "already enabled service is not registered again")
for before in [SMAppService.Status.notFound, .notRegistered] {
    for after in [SMAppService.Status.enabled, .requiresApproval] {
        let service = LoginService(before)
        service.afterRegister = after
        var diagnostics: [String] = []
        check(try! updateLoginPreference(enabled: true, service: service,
              onFailure: { diagnostics.append($0) }) == after, "explicit enable accepts only confirmed/pending result")
        check(service.registers == 1 && service.unregisters == 0 && service.reads == 2,
              "initial notFound/notRegistered makes exactly one register attempt and one poststatus read")
        check(diagnostics.isEmpty, "successful registration writes no failure diagnostic")
    }
    for after in [SMAppService.Status.notFound, .notRegistered] {
        let service = LoginService(before)
        service.afterRegister = after
        var diagnostics: [String] = []
        do { _ = try updateLoginPreference(enabled: true, service: service,
                    onFailure: { diagnostics.append($0) }); fatalError("unconfirmed enable accepted") }
        catch LoginPreferenceError.notFound { check(after == .notFound, "not-found poststatus remains an error") }
        catch LoginPreferenceError.notConfirmed { check(after == .notRegistered, "unregistered poststatus remains an error") }
        catch { fatalError("wrong poststatus error") }
        check(service.registers == 1 && service.unregisters == 0 && service.reads == 2,
              "unconfirmed enable never retries or resets registration")
        check(diagnostics.count == 1 && diagnostics[0].contains("phase=verify")
              && diagnostics[0].contains("before=" + loginStatusName(before))
              && diagnostics[0].contains("after=" + loginStatusName(after))
              && diagnostics[0].contains("register_attempted=true"), "failure diagnostic preserves exact status phase")
    }
}
let alreadyPending = LoginService(.requiresApproval)
check(try! updateLoginPreference(enabled: true, service: alreadyPending) == .requiresApproval
      && alreadyPending.registers == 0 && alreadyPending.unregisters == 0,
      "pending approval never triggers a second registration")
let unconfirmed = LoginService(.notRegistered)
unconfirmed.afterRegister = .notRegistered
do { _ = try updateLoginPreference(enabled: true, service: unconfirmed); fatalError("unconfirmed registration accepted") }
catch LoginPreferenceError.notConfirmed {} catch { fatalError("wrong unconfirmed-service error") }
for domain in ["SMAppServiceErrorDomain", "fixture-account@example.invalid"] {
    let service = LoginService(.notFound)
    let original = NSError(domain: domain, code: -18,
        userInfo: [NSLocalizedDescriptionKey: "fixture-private-description",
                   "path": "/synthetic-account/private", "account": "fixture-private-account"])
    service.failure = original
    var diagnostics: [String] = []
    do { _ = try updateLoginPreference(enabled: true, service: service,
                onFailure: { diagnostics.append($0) }); fatalError("registration failure swallowed") }
    catch { check((error as NSError) === original, "registration preserves the original service error") }
    check(service.registers == 1 && service.unregisters == 0 && service.reads == 2,
          "failed register makes one attempt, rereads status and never unregisters")
    let expectedDomain = domain == "SMAppServiceErrorDomain" ? domain : "other"
    check(diagnostics.count == 1 && diagnostics[0].contains("phase=register")
          && diagnostics[0].contains("error_domain=" + expectedDomain)
          && diagnostics[0].contains("error_code=-18") && diagnostics[0].count < 512,
          "diagnostic contains bounded phase and allowlisted domain/numeric code")
    check(!diagnostics[0].contains("fixture-") && !diagnostics[0].contains("/synthetic-account"),
          "diagnostic omits description, userInfo, account and unapproved error domain")
}
check(try! updateLoginPreference(enabled: false, service: existing) == .notRegistered
      && existing.unregisters == 1, "disable unregisters and rereads")
let missing = LoginService(.notFound)
check(try! updateLoginPreference(enabled: false, service: missing) == .notFound
      && missing.registers == 0 && missing.unregisters == 0,
      "already absent disabled service needs no registration")
let cannotDisable = LoginService(.enabled)
cannotDisable.afterUnregister = .enabled
do { _ = try updateLoginPreference(enabled: false, service: cannotDisable); fatalError("unconfirmed disable accepted") }
catch LoginPreferenceError.notConfirmed {} catch { fatalError("wrong disable poststatus error") }
check(cannotDisable.unregisters == 1 && cannotDisable.registers == 0, "disable never resets or registers")
let failedDisable = LoginService(.enabled)
let disableError = NSError(domain: NSPOSIXErrorDomain, code: 1, userInfo: ["account": "fixture-private-account"])
failedDisable.failure = disableError
var disableDiagnostics: [String] = []
do { _ = try updateLoginPreference(enabled: false, service: failedDisable,
            onFailure: { disableDiagnostics.append($0) }); fatalError("unregister error swallowed") }
catch { check((error as NSError) === disableError, "disable propagates original service failure") }
check(failedDisable.unregisters == 1 && failedDisable.registers == 0 && failedDisable.reads == 2
      && disableDiagnostics.count == 1 && disableDiagnostics[0].contains("phase=unregister"),
      "disable failure records the real phase without a reset fallback")

final class Defaults {
    var values: [String: Any] = ["setupCompletedV1": true,
        "seenAttentionEvents": ["live-event"], "handledAttentionEvents": ["live-event": NSNumber(value: 1)],
        "sampleSeenAttentionEvents": ["sample-event"], "sampleHandledAttentionEvents": ["sample-event": NSNumber(value: 2)],
        "displayPreferences": ["agent-board:theme-id": "matrix"],
        "codexSourceBookmark": Data([1,2,3]), "claudeSessionsBookmark": Data([4,5,6]),
        "sampleLastCollectorPort": 19001, "lastCollectorPort": 19002]
    func bool(forKey key: String) -> Bool { values[key] as? Bool ?? false }
    func integer(forKey key: String) -> Int { values[key] as? Int ?? 0 }
    func stringArray(forKey key: String) -> [String]? { values[key] as? [String] }
    func dictionary(forKey key: String) -> [String: Any]? { values[key] as? [String: Any] }
    func set(_ value: Any?, forKey key: String) { values[key] = value }
}
final class Scope { var releases = 0; func stopAccessingSecurityScopedResource() { releases += 1 } }
final class UsageAlert {
    static var response = NSApplication.ModalResponse.alertFirstButtonReturn
    static var latest: UsageAlert?
    var messageText = ""
    var informativeText = ""
    var buttons: [String] = []
    init() { Self.latest = self }
    func addButton(withTitle title: String) { buttons.append(title) }
    func runModal() -> NSApplication.ModalResponse { Self.response }
}
final class UsagePasteboard {
    static let general = UsagePasteboard()
    var copied: String?
    func clearContents() { copied = nil }
    func setString(_ value: String, forType type: NSPasteboard.PasteboardType) { copied = value }
}
final class UsageWorkspace {
    static let shared = UsageWorkspace()
    var selected: [URL] = []
    func activateFileViewerSelecting(_ urls: [URL]) { selected = urls }
}
final class Runtime {
    enum StartupPhase { case restoringSources, sourcesRestored }
    var startupWaiting: (() -> Void)?
    func recordStartupPhase(_ phase: StartupPhase) {}
    var storeDistribution = true
    var sampleMode = false
    var sandboxed = true
    var usageEnabled = false
    var projectsEnabled = false
    var claudeChatsEnabled = false
    var chatGPTChatsEnabled = false
    var grantedSourceURLs: [String: Scope] = [:]
    var preferredPort = 0
    var rejectSample = false
    var stops = 0
    var starts = 0
    var finishStop: (() -> Void)?
    let executable = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
        .appendingPathComponent("usage-fixture/AgentBoardCollector.app/Contents/MacOS/agent-board-collector")
    func stopAndWait(completion: @escaping () -> Void) { stops += 1; finishStop = completion }
    func setSampleMode(_ value: Bool) throws {
        if value && rejectSample { throw LoginPreferenceError.notConfirmed }
        sampleMode = value; grantedSourceURLs.removeAll()
    }
    func start(completion: @escaping (Result<URL, Error>) -> Void) {
        starts += 1
        completion(.success(URL(string: "http://127.0.0.1:19003/")!))
    }
}
final class View {
    var stopped = 0
    var documents: [String] = []
    func stopLoading() { stopped += 1 }
    func loadHTMLString(_ value: String, baseURL: URL?) { documents.append(value) }
}
final class Window {
    var title = "Agent Board"
    var contentView: View?
    func makeFirstResponder(_ view: View) {}
}
final class Button { var title = "" }
final class Status { var button: Button? = Button() }
final class Model: NSObject {
    let defaults = Defaults()
    let runtime = Runtime()
    var fixtureMode = false
    var startingCollector = false
    var startupModeError: Error?
    var stateGeneration = UUID()
    var polling = false
    var csrfToken: String? = "old-token"
    var notificationIntent = UUID()
    var requestingPermission = false
    var receivedInitialSnapshot = true
    var collectorAvailable = true
    var ownedCollectorRunning = true
    var chats = ["old-live-chat"]
    var statusItem: Status? = Status()
    let webView = View()
    let window = Window()
    var accessURLs: [Scope] = []
    var sourceURLs: [String: Scope] = [:]
    var sourceAccessChanged = false
    var seen: Set<String> = []
    var seenOrder: [String] = []
    var handledAt: [String: Double] = [:]
    var boardURL = URL(string: "http://127.0.0.1:19000/")!
    var collectorRetryButton: Button?
    var restored = 0
    var setups = 0
    var errors = 0
    var fetches = 0
    var nativeSnapshots = 0
    var actionErrors: [String] = []
    func updateMenu() {}
    func restoreSourceAccess() { restored += 1 }
    func installAccessScript() {}
    func reload() {}
    func fetchState(_ completion: @escaping (Result<[String: Any], Error>) -> Void) { fetches += 1; completion(.success([:])) }
    func applySnapshot(_ value: [String: Any]) { nativeSnapshots += 1; receivedInitialSnapshot = true; collectorAvailable = true }
    func showSetup() { setups += 1 }
    func presentStartupError(_ error: Error) { errors += 1 }
    func showActionError(_ value: String) { actionErrors.append(value) }
    func makeSetupAlert() -> NSAlert {
__BUTTONS__
        return alert
    }
__LEDGERS__
__METHODS__
__USAGE__
}
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let model = Model()
let plugin = model.runtime.executable.deletingLastPathComponent().deletingLastPathComponent()
    .appendingPathComponent("Resources/integrations/claude-local-usage", isDirectory: true)
let manifest = plugin.appendingPathComponent(".claude-plugin/plugin.json")
let hook = plugin.appendingPathComponent("hooks/register.js")
try FileManager.default.createDirectory(at: manifest.deletingLastPathComponent(), withIntermediateDirectories: true)
try FileManager.default.createDirectory(at: hook.deletingLastPathComponent(), withIntermediateDirectories: true)
try Data("{}".utf8).write(to: manifest)
try Data("A fixture only; never executed.".utf8).write(to: hook)
model.connectClaudeUsage()
check(UsagePasteboard.general.copied == plugin.path, "Copy Plugin Path targets actual plugin folder, not marketplace parent")
check(UsageAlert.latest!.buttons == ["Copy Plugin Path", "Show Plugin Files", "Cancel"]
      && UsageAlert.latest!.informativeText.contains("~/.claude/skills/")
      && UsageAlert.latest!.informativeText.contains("do not replace or merge")
      && UsageAlert.latest!.informativeText.contains("new local Code session"), "local-only installation instructions and unchanged action names")
UsageAlert.response = .alertSecondButtonReturn
model.connectClaudeUsage()
check(UsageWorkspace.shared.selected == [plugin], "Show Plugin Files selects actual plugin folder")
try FileManager.default.removeItem(at: hook)
model.connectClaudeUsage()
check(model.actionErrors.count == 1, "missing plugin code fails without folder or clipboard action")
model.runtime.sampleMode = true
model.connectClaudeUsage()
check(model.actionErrors.count == 1, "sample mode cannot offer installation")
model.runtime.sampleMode = false
model.defaults.values["setupCompletedV1"] = false
check(model.makeSetupAlert().buttons.map { $0.title } == ["Start Monitoring", "Cancel", "Try Sample Board"],
      "first-run Store setup offers explicit sample board")
model.runtime.storeDistribution = false
check(model.makeSetupAlert().buttons.count == 2, "direct setup offers no sample mode")
model.runtime.storeDistribution = true
model.defaults.values["setupCompletedV1"] = true
check(model.makeSetupAlert().buttons.map { $0.title } == ["Save", "Cancel"], "completed setup preserves Save and Cancel")
let liveScope = Scope()
model.accessURLs = [liveScope]
model.sourceURLs = ["codex": liveScope]
model.runtime.grantedSourceURLs = model.sourceURLs
model.loadAttentionLedger()
check(model.seen == ["live-event"], "live attention ledger loaded")
let previousGeneration = model.stateGeneration
model.setBoardMode(sample: true)
check(model.startingCollector && model.runtime.stops == 1 && liveScope.releases == 0,
      "mode switch waits for owned child before releasing grants")
check(model.stateGeneration != previousGeneration && model.csrfToken == nil && !model.polling
      && model.chats.isEmpty && !model.collectorAvailable, "mode switch invalidates stale state and clears live cards")
check(model.webView.documents.last!.contains("changing boards"), "old live document is replaced during transition")
model.runtime.finishStop!()
check(liveScope.releases == 1 && model.sourceURLs.isEmpty && model.accessURLs.isEmpty,
      "live scopes released only after stop completion")
check(model.runtime.sampleMode && model.runtime.starts == 1 && model.restored == 0
      && model.runtime.grantedSourceURLs.isEmpty, "sample start never restores or passes live grants")
check(model.seen == ["sample-event"] && model.seenLedgerKey == "sampleSeenAttentionEvents"
      && model.displayPreferencesKey == "sampleDisplayPreferences", "sample ledgers and display keys are distinct")
check(model.defaults.values["codexSourceBookmark"] as? Data == Data([1,2,3])
      && model.defaults.bool(forKey: "setupCompletedV1"), "live bookmarks and consent survive sample entry")
check(model.defaults.dictionary(forKey: "displayPreferences")?["agent-board:theme-id"] as? String == "matrix",
      "live appearance is not overwritten")
model.setBoardMode(sample: false)
model.runtime.finishStop!()
check(!model.runtime.sampleMode && model.runtime.starts == 2 && model.restored == 1
      && model.seen == ["live-event"], "return to consented live board restores its own attention ledger")
model.defaults.values["setupCompletedV1"] = false
model.setBoardMode(sample: true)
model.runtime.finishStop!()
model.setBoardMode(sample: false)
model.runtime.finishStop!()
check(model.setups == 1 && !model.defaults.bool(forKey: "setupCompletedV1") && model.runtime.starts == 3,
      "return without live consent opens Setup without starting live monitoring")
model.runtime.storeDistribution = false
let previousStops = model.runtime.stops
model.setBoardMode(sample: true)
check(model.runtime.stops == previousStops, "direct distribution cannot select sample mode")
let firstRun = Model()
firstRun.defaults.values["setupCompletedV1"] = false
firstRun.setBoardMode(sample: true)
firstRun.runtime.finishStop!()
check(firstRun.fetches == 1 && firstRun.nativeSnapshots == 1 && !firstRun.defaults.bool(forKey: "setupCompletedV1"),
      "first-run sample receives native snapshots without live consent")
let blocked = Model()
blocked.defaults.values["sampleBoardSelected"] = true
blocked.runtime.rejectSample = true
blocked.restoreSelectedSampleMode()
check(blocked.sampleModeSelected && blocked.startupModeError != nil, "invalid saved sample storage retains sample intent")
blocked.startCollector()
check(blocked.runtime.starts == 0 && blocked.restored == 0 && blocked.errors == 1,
      "invalid saved sample storage cannot fall back to consented live sources")
blocked.setBoardMode(sample: false)
blocked.runtime.finishStop!()
check(!blocked.sampleModeSelected && blocked.startupModeError == nil && blocked.runtime.starts == 1 && blocked.restored == 1,
      "explicit Return to Live Board recovers from invalid saved sample storage")
print("PASS: native login status rereads/errors, first-run sample choice and isolated sample/live transitions")
'''.replace("__LOGIN__", login).replace("__BUTTONS__", buttons.rstrip()).replace("__LEDGERS__", ledgers).replace("__METHODS__", methods).replace("__USAGE__", usage)
    compile_and_run(qa / "appdelegate", program)
    runtime = r'''import Foundation

func check(_ value: @autoclosure () -> Bool, _ message: String) {
    if !value() { print("FAIL: " + message); exit(1) }
}
let root = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
func bundle(_ name: String, store: Bool) throws -> Bundle {
    let url = root.appendingPathComponent(name + ".app")
    try FileManager.default.createDirectory(at: url.appendingPathComponent("Contents"), withIntermediateDirectories: true)
    let info: [String: Any] = ["CFBundleIdentifier": "local.agentboard.runtime-sample-" + name,
        "CFBundleName": name, "CFBundlePackageType": "APPL", "AgentBoardStoreDistribution": store]
    try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
        .write(to: url.appendingPathComponent("Contents/Info.plist"))
    return Bundle(url: url)!
}
let support = root.appendingPathComponent("disposable-support")
let runtime = CollectorRuntime(bundle: try bundle("store", store: true), support: support, sandboxed: true)
let direct = CollectorRuntime(bundle: try bundle("direct", store: false), support: support, sandboxed: false)
do { try direct.setSampleMode(true); fatalError("direct runtime accepted sample mode") } catch {}
try FileManager.default.createDirectory(at: support, withIntermediateDirectories: true)
let sampleDirectory = support.appendingPathComponent("Samples")
try FileManager.default.createSymbolicLink(at: sampleDirectory, withDestinationURL: support)
do { try runtime.setSampleMode(true); fatalError("sample storage followed a link into live support") } catch {}
check(!runtime.sampleMode, "rejected sample path preserves live mode")
try FileManager.default.removeItem(at: sampleDirectory)
try FileManager.default.createDirectory(at: sampleDirectory, withIntermediateDirectories: true)
let redirectedData = sampleDirectory.appendingPathComponent("data")
try FileManager.default.createSymbolicLink(at: redirectedData, withDestinationURL: support)
do { try runtime.setSampleMode(true); fatalError("sample data followed a link into live support") } catch {}
try FileManager.default.removeItem(at: redirectedData)
try FileManager.default.createDirectory(at: redirectedData, withIntermediateDirectories: true)
let liveLog = support.appendingPathComponent("live-log-sentinel")
let sampleLog = redirectedData.appendingPathComponent("collector.log")
let originalLog = Data("Preserve the disposable live log.\n".utf8)
try originalLog.write(to: liveLog)
try FileManager.default.createSymbolicLink(at: sampleLog, withDestinationURL: liveLog)
do { try runtime.setSampleMode(true); fatalError("sample log accepted a symbolic link") } catch {}
try FileManager.default.removeItem(at: sampleLog)
try FileManager.default.linkItem(at: liveLog, to: sampleLog)
do { try runtime.setSampleMode(true); fatalError("sample log accepted a hard link") } catch {}
try FileManager.default.removeItem(at: sampleLog)
check(try! Data(contentsOf: liveLog) == originalLog, "sample path rejection preserves the live log")
try Data("An ordinary sample log.\n".utf8).write(to: sampleLog)
runtime.grantedSourceURLs["codex"] = root.appendingPathComponent("not-a-granted-source")
try runtime.setSampleMode(true)
check(runtime.support == support.appendingPathComponent("Samples", isDirectory: true)
      && runtime.grantedSourceURLs.isEmpty, "production runtime isolates sample support and removes grants")
try runtime.setSampleMode(false)
check(runtime.support == support, "production runtime restores exact live support path")
let child = Process()
child.executableURL = URL(fileURLWithPath: "/bin/sleep")
child.arguments = ["20"]
try child.run()
runtime.process = child
do { try runtime.setSampleMode(true); fatalError("mode changed before child stopped") } catch {}
var finished = false
runtime.stopAndWait {
    check(!child.isRunning && runtime.process == nil, "stop completion follows owned child exit/reaping")
    finished = true
}
let deadline = Date().addingTimeInterval(5)
while !finished && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
check(finished, "owned child stop completion arrived")
print("PASS: production runtime mode capability/storage guards and exact owned-child stop/reap")
'''
    grant_properties = source[source.index("    private var sourceBookmarkKeys:"):
                              source.index("    private var sourceHome:")]
    grants = r'''import Foundation
func check(_ value: @autoclosure () -> Bool, _ label: String) {
    if !value() { print("FAIL: " + label); exit(1) }
}
final class Preferences {
    var values: [String: Data] = [:]
    func data(forKey key: String) -> Data? { values[key] }
    func set(_ value: Data, forKey key: String) { values[key] = value }
}
final class Grants {
    var sampleModeSelected = false
    let defaults = Preferences()
    let sourceHome = URL(fileURLWithPath: "__HOME__", isDirectory: true)
    var sourceURLs: [String: URL] = [:]
    var accessURLs: [URL] = []
__PROPERTIES__
__RESTORE__
    var keys: [String] { sourceBookmarkKeys.map { $0.key } }
    var needsReselection: Bool { claudeDesktopNeedsReselection }
    func accepts(_ value: URL) -> Bool { validSourceGrant(value, provider: "claude_desktop") }
}
let model = Grants()
let legacy = Data("a legacy broad grant must never be resolved".utf8)
model.defaults.values["claudeSourceBookmark"] = legacy
model.restoreSourceAccess()
check(model.sourceURLs.isEmpty && model.accessURLs.isEmpty, "legacy parent bookmark cannot restore any source grant")
check(model.needsReselection && !model.keys.contains("claudeSourceBookmark")
      && model.keys.contains("claudeDesktopSessionsBookmarkV2"), "legacy grant requests explicit narrow reselection")
check(model.defaults.data(forKey: "claudeSourceBookmark") == legacy, "migration does not silently rewrite the old authorization")
let parent = model.sourceHome.appendingPathComponent("Library/Application Support/Claude")
let narrow = parent.appendingPathComponent("claude-code-sessions")
check(model.accepts(narrow) && !model.accepts(parent), "only the exact sessions folder satisfies the native grant contract")
try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
try FileManager.default.createSymbolicLink(at: narrow, withDestinationURL: parent)
check(!model.accepts(narrow), "a sessions-name symlink cannot widen the grant to Claude's app data")
print("PASS: native legacy bookmark exclusion, reselection state and narrow-folder validation without grants")
'''.replace("__HOME__", str(qa / "grant-fixture-home")).replace("__PROPERTIES__", grant_properties).replace("__RESTORE__", method(source, "restoreSourceAccess"))
    compile_and_run(qa / "grants", grants)
    initializer = source[source.index('        var selected = URL(string: "http://127.0.0.1:8769/")!'):
                         source.index("        boardURL = selected")]
    profile = '''import Foundation
__INITIALIZER__
#if AGENT_BOARD_DIRECT
assert(fixture && selected.absoluteString == "http://127.0.0.1:58419/")
print("PASS: direct profile retains explicitly requested local fixture mode")
#else
assert(!fixture && selected.absoluteString == "http://127.0.0.1:8769/")
print("PASS: Store profile ignores fixture URL arguments and keeps authenticated monitoring")
#endif
'''.replace("__INITIALIZER__", initializer)
    for name, flags in (("direct", ("-D", "AGENT_BOARD_DIRECT")), ("store", ())):
        compile_and_run(qa / ("profile-" + name), profile, compiler_flags=flags,
                        arguments=("--board-url", "http://127.0.0.1:58419/"))
    compile_and_run(qa / "runtime", runtime, runtime=True)
    print("Evidence:", qa)


if __name__ == "__main__":
    main()
