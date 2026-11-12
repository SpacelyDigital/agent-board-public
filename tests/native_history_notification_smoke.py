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
    methods = source[source.index("    func applySnapshot("):source.index("    func performAction(")]
    methods += source[source.index("    @objc func openNextChat("):source.index("    func openExistingChat(")]
    program = r'''import Foundation
import UserNotifications
import CryptoKit
__CHAT__
final class Defaults { func set(_ value: Any, forKey key: String) {} }
final class Runtime { var sampleMode = false }
final class Center {
    var requests: [UNNotificationRequest] = []
    var completions: [(Error?) -> Void] = []
    var automatic = true
    func add(_ request: UNNotificationRequest, withCompletionHandler completion: @escaping (Error?) -> Void) {
        requests.append(request)
        if automatic { completion(nil) } else { completions.append(completion) }
    }
    func answer(failed: Bool) {
        completions.removeFirst()(failed ? NSError(domain: "Fixture", code: 1) : nil)
    }
}
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
    var stateGeneration = UUID()
    var notificationIntent = UUID()
    var pendingNotifications: [String: AttentionDelivery] = [:]
    var notificationClock: () -> TimeInterval = { 0 }
    var lastDeliveryFailed = false
    let notificationPrefix = "disposable-fixture"
    let center = Center()
    var alerts: [String] { center.requests.map { $0.content.userInfo["event_id"] as! String } }
    var opened: [String] = []
    func updateMenu() {}
    func attentionNotificationSound() -> UNNotificationSound? { nil }
    func performAction(_ action: String, key: String, eventID: String) { opened.append(eventID) }
__METHODS__
}
func check(_ value: @autoclosure () -> Bool, _ name: String) {
    if !value() { print("FAIL: " + name); exit(1) }
}
func drain() { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
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

// Fault injection uses the production notify method and asynchronous OS callback
// path. The center records requests but never sends anything to macOS.
let retry = Model()
retry.center.automatic = false
var clock = 0.0
retry.notificationClock = { clock }
retry.applySnapshot(snapshot([]))
retry.applySnapshot(snapshot([row("retry")]))
retry.applySnapshot(snapshot([row("retry")]))
check(retry.alerts == ["retry"], "an in-flight request cannot be submitted twice")
retry.center.answer(failed:true); drain()
check(retry.lastDeliveryFailed && retry.pendingNotifications.count == 1, "failed add retains retry without forgetting seen event")
clock = 4
retry.applySnapshot(snapshot([row("retry")]))
check(retry.alerts.count == 1, "first retry respects five-second delay")
clock = 5
retry.applySnapshot(snapshot([row("retry")]))
check(retry.alerts.count == 2 && retry.center.requests[0].identifier == retry.center.requests[1].identifier,
      "retry submits the identical request identifier")
retry.center.answer(failed:false); drain()
clock = 100
retry.applySnapshot(snapshot([row("retry")]))
check(retry.alerts.count == 2 && retry.pendingNotifications.isEmpty && !retry.lastDeliveryFailed,
      "an accepted retry is never submitted again")
let relaunched = Model()
relaunched.seen = retry.seen; relaunched.seenOrder = retry.seenOrder; relaunched.handledAt = retry.handledAt
relaunched.applySnapshot(snapshot([row("retry")]))
relaunched.applySnapshot(snapshot([row("retry")]))
check(relaunched.alerts.isEmpty, "saved ledger prevents accepted event replay after relaunch")

let bounded = Model()
bounded.center.automatic = false
clock = 0; bounded.notificationClock = { clock }
bounded.applySnapshot(snapshot([])); bounded.applySnapshot(snapshot([row("bounded")]))
bounded.center.answer(failed:true); drain()
clock = 5; bounded.applySnapshot(snapshot([row("bounded")]))
bounded.center.answer(failed:true); drain()
clock = 19; bounded.applySnapshot(snapshot([row("bounded")]))
check(bounded.alerts.count == 2, "second retry respects fifteen-second delay")
clock = 20; bounded.applySnapshot(snapshot([row("bounded")]))
bounded.center.answer(failed:true); drain()
clock = 100; bounded.applySnapshot(snapshot([row("bounded")]))
check(bounded.alerts.count == 3 && bounded.pendingNotifications.isEmpty && bounded.lastDeliveryFailed,
      "persistent add failure stops after three attempts")

for change in ["reviewed", "replacement", "account", "unavailable", "cooking", "snoozed", "visible", "disabled", "permission", "intent", "generation", "expiry"] {
    let cancelled = Model()
    cancelled.center.automatic = false
    clock = 0; cancelled.notificationClock = { clock }
    cancelled.applySnapshot(snapshot([])); cancelled.applySnapshot(snapshot([row("old")]))
    cancelled.center.answer(failed:true); drain()
    var current = row("old")
    var rows = [current]
    switch change {
    case "reviewed": rows = []
    case "replacement", "account": rows = [row("new-" + change)]
    case "unavailable": current["source_unavailable"] = true; rows = [current]
    case "cooking": current["status"] = "cooking"; rows = [current]
    case "snoozed": current["snoozed_until"] = 100; rows = [current]
    case "visible": cancelled.boardIsVisibleAndActive = true
    case "disabled": cancelled.notificationsEnabled = false
    case "permission": cancelled.permissionAllowsNotifications = false
    case "intent": cancelled.notificationIntent = UUID()
    case "generation": cancelled.stateGeneration = UUID()
    default: break
    }
    clock = change == "expiry" ? 120 : 5
    cancelled.applySnapshot(snapshot(rows))
    check(cancelled.alerts.filter { $0 == "old" }.count == 1, "no retry after " + change)
    check(!cancelled.pendingNotifications.keys.contains { $0.hasSuffix("|old") }, "cancelled receipt released after " + change)
}

// A late failed callback cannot revive a reviewed event or replace the new
// reminder's in-flight receipt for the same reply.
let late = Model()
late.center.automatic = false
late.applySnapshot(snapshot([])); late.applySnapshot(snapshot([row("late")]))
late.applySnapshot(snapshot([]))
late.center.answer(failed:true); drain()
late.applySnapshot(snapshot([row("late")]))
check(late.alerts.count == 1 && late.pendingNotifications.isEmpty, "late error after review cannot revive a retry")
let reminder = Model()
reminder.center.automatic = false
clock = 0; reminder.notificationClock = { clock }
reminder.applySnapshot(snapshot([row("reminder", silent:true, snooze:10)]))
reminder.applySnapshot(snapshot([row("reminder", silent:true, snooze:10)], now:11))
reminder.center.answer(failed:true); drain()
clock = 5; reminder.applySnapshot(snapshot([row("reminder", silent:true, snooze:10)], now:12))
check(reminder.alerts == ["reminder", "reminder"], "failed snooze reminder retries despite quiet historical reconfirmation")
reminder.center.answer(failed:false); drain()
clock = 30; reminder.applySnapshot(snapshot([row("reminder", silent:true, snooze:10)], now:13))
check(reminder.alerts.count == 2, "accepted snooze reminder does not replay")

let stale = Model()
stale.center.automatic = false
clock = 0; stale.notificationClock = { clock }
stale.applySnapshot(snapshot([])); stale.applySnapshot(snapshot([row("stale")]))
stale.center.answer(failed:true); drain()
clock = 5
var staleState = snapshot([row("stale")], now:50); staleState["last_scan"] = 1
stale.applySnapshot(staleState)
check(stale.alerts.count == 1, "stale collector cannot submit a retry")
stale.applySnapshot(snapshot([row("stale")], now:51))
check(stale.alerts.count == 2, "fresh eligible snapshot can resume bounded retry")
let slept = Model()
slept.center.automatic = false
clock = 0; slept.notificationClock = { clock }
slept.applySnapshot(snapshot([])); slept.applySnapshot(snapshot([row("sleep")]))
slept.center.answer(failed:true); drain()
clock = 5; slept.applySnapshot(snapshot([row("sleep")], now:1000))
check(slept.alerts.count == 1 && slept.pendingNotifications.isEmpty,
      "long suspension expires retries even when the monotonic clock barely advances")
print("PASS: production native queue and OS-add fault recovery; bounded retry, accepted delivery dedup, review/account/revision cancellation, relaunch suppression and snooze; no OS deliveries")
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
