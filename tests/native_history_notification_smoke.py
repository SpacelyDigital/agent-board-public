"""Exercise production native attention parsing, snapshot handling and opening.

No app, account, GUI, permission center or notification service is accessed.
"""

from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / "MacApp.swift").read_text()
    chat = source[source.index("struct AttentionChat {"):source.index("final class AppDelegate:")]
    methods = source[source.index("    func applySnapshot("):source.index("    func notify(")]
    methods += source[source.index("    @objc func openNextChat("):source.index("    func openExistingChat(")]
    program = r'''import Foundation
__CHAT__
final class Defaults { func set(_ value: Any, forKey key: String) {} }
final class Runtime { var sampleMode = false }
final class Model: NSObject {
    var csrfToken: String?
    var collectorAvailable = false
    var chats: [AttentionChat] = []
    var seen: Set<String> = []
    var seenOrder: [String] = []
    var handledAt: [String: Double] = [:]
    var receivedInitialSnapshot = false
    let defaults = Defaults()
    let runtime = Runtime()
    var seenLedgerKey = "seenAttentionEvents"
    var handledLedgerKey = "handledAttentionEvents"
    var notificationsEnabled = true
    var permissionAllowsNotifications = true
    var boardIsVisibleAndActive = false
    var alerts: [String] = []
    var opened: [String] = []
    func updateMenu() {}
    func notify(_ chat: AttentionChat) { alerts.append(chat.eventID) }
    func performAction(_ action: String, key: String, eventID: String) { opened.append(eventID) }
__METHODS__
}
func check(_ value: @autoclosure () -> Bool, _ name: String) {
    if !value() { print("FAIL: " + name); exit(1) }
}
func snapshot(_ rows: [[String: Any]], now: Double = 1) -> [String: Any] {
    ["sessions":rows,"now":now,"last_scan":now,"csrf_token":"fixture"]
}
func row(_ event: String, silent: Bool = false, status: String = "ready", snooze: Double = 0) -> [String: Any] {
    ["key":"claude:fixture","title":"Disposable QA","provider":"claude","status":status,
     "event_id":event,"can_open":true,"notification_eligible":true,
     "reconfirmation_silent":silent,"snoozed_until":snooze]
}
let model = Model()
model.applySnapshot(snapshot([]))
model.applySnapshot(snapshot([row("new-1")]))
check(model.alerts == ["new-1"], "new reply notifies")
model.openNextChat()
check(model.opened == ["new-1"], "new reply remains openable")
model.applySnapshot(snapshot([row("new-1", silent:true, status:"disconnected")]))
check(model.chats.isEmpty, "checking history does not count as confirmed attention")
model.applySnapshot(snapshot([row("new-1", silent:true)]))
check(model.chats.filter { $0.eligible }.count == 1, "reconfirmed reply keeps native queue and badge eligibility")
model.openNextChat()
check(model.opened == ["new-1","new-1"], "reconfirmed reply remains openable")
check(model.alerts == ["new-1"], "reconfirmed reply does not replay notification")
model.applySnapshot(snapshot([row("new-2")]))
check(model.alerts == ["new-1","new-2"], "a different completion still notifies")
model.applySnapshot(snapshot([row("new-2", silent:true, snooze:10)]))
check(!model.chats.contains { $0.eligible }, "future snooze keeps chat out of native queue")
model.applySnapshot(snapshot([row("new-2", silent:true, snooze:10)], now:11))
check(model.alerts == ["new-1","new-2","new-2"], "future snooze reminder still notifies")
let cold = Model()
cold.applySnapshot(snapshot([row("old", silent:true, status:"disconnected")]))
cold.applySnapshot(snapshot([row("old", silent:true)]))
cold.openNextChat()
check(cold.alerts.isEmpty && cold.opened == ["old"], "cold reconfirmation is quiet but openable")
for status in ["ready", "needs_you"] {
    let revoked = Model()
    revoked.applySnapshot(snapshot([]))
    var unavailable = row("revoked-" + status, status: status)
    unavailable["source_unavailable"] = true
    revoked.applySnapshot(snapshot([unavailable]))
    check(revoked.chats.count == 1 && revoked.chats[0].status == status && revoked.chats[0].canOpen,
          "revoked source retains historical state and explicit opening")
    revoked.openNextChat()
    check(revoked.chats.filter { $0.eligible }.isEmpty && revoked.alerts.isEmpty && revoked.opened.isEmpty,
          "revoked source cannot count in native badge, notify or open next")
    unavailable["source_unavailable"] = false
    revoked.applySnapshot(snapshot([unavailable]))
    revoked.openNextChat()
    check(revoked.chats.filter { $0.eligible }.count == 1 && revoked.alerts.isEmpty
          && revoked.opened == ["revoked-" + status], "regrant restores the same quiet event without replay")
    unavailable["event_id"] = "after-regrant-" + status
    revoked.applySnapshot(snapshot([unavailable]))
    check(revoked.alerts == ["after-regrant-" + status], "a genuinely new event after regrant still notifies")
}
let samples = Model()
samples.runtime.sampleMode = true
samples.applySnapshot(snapshot([]))
var sampleReady = row("sample-ready")
sampleReady["key"] = "codex:sample-codex"
sampleReady["provider"] = "codex"
sampleReady["sample"] = true
sampleReady["notification_eligible"] = false
var sampleQuestion = row("sample-question", status: "needs_you")
sampleQuestion["sample"] = true
sampleQuestion["notification_eligible"] = false
samples.applySnapshot(snapshot([sampleReady, sampleQuestion]))
check(samples.chats.filter { $0.eligible }.count == 2, "parsed sample Ready and Needs-you count in native badge")
samples.openNextChat()
check(samples.alerts.isEmpty && samples.opened.isEmpty && samples.chats.allSatisfy { !$0.canOpen },
      "sample badge eligibility never enables alerts or provider opening")
sampleReady["snoozed_until"] = 10
sampleQuestion["is_background"] = true
samples.applySnapshot(snapshot([sampleReady, sampleQuestion]))
check(samples.chats.filter { $0.eligible }.isEmpty, "sample badge still excludes snoozed and background cards")
sampleReady["status"] = "cooking"
sampleQuestion["status"] = "disconnected"
samples.applySnapshot(snapshot([sampleReady, sampleQuestion]))
check(samples.chats.isEmpty, "sample Cooking and Disconnected do not count as attention")
var untrustedSampleFlag = row("not-a-sample")
untrustedSampleFlag["sample"] = true
untrustedSampleFlag["notification_eligible"] = false
model.applySnapshot(snapshot([untrustedSampleFlag]))
check(model.chats.filter { $0.eligible }.isEmpty, "sample marker cannot override live notification eligibility")
print("PASS: production native queue, new completion, warm/cold reconfirmation, snooze and sample badge without alerts/opens")
'''.replace("__CHAT__", chat).replace("__METHODS__", methods)
    qa = ROOT / ".local/native-history-notification-qa" / uuid.uuid4().hex
    qa.mkdir(parents=True, exist_ok=False)
    path, binary = qa / "main.swift", qa / "native-history-notification-smoke"
    path.write_text(program)
    subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(qa / "swift-cache"), str(path), "-o", str(binary)],
                   check=True, timeout=60)
    subprocess.run([str(binary)], check=True, timeout=10)


if __name__ == "__main__":
    main()
