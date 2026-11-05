"""Run actual native notification-intent methods with a fake permission center.

This compiles a private Swift fixture. It never launches the app, displays an OS
permission dialog, delivers a notification or contacts a provider account.
"""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / "MacApp.swift").read_text()
    methods = source[source.index("    @objc func toggleNotifications()"):source.index("    @objc func sendTestNotification()")]
    program = '''import AppKit
import UserNotifications
final class Defaults {
    var enabled = false
    func bool(forKey key: String) -> Bool { enabled }
    func set(_ value: Bool, forKey key: String) { enabled = value }
}
struct Settings { let authorizationStatus: UNAuthorizationStatus = .authorized }
final class Center {
    var settings: [(Settings) -> Void] = []
    var grants: [(Bool, Error?) -> Void] = []
    var requests = 0
    func getNotificationSettings(_ completion: @escaping (Settings) -> Void) { settings.append(completion) }
    func requestAuthorization(options: UNAuthorizationOptions, completionHandler: @escaping (Bool, Error?) -> Void) {
        requests += 1
        grants.append(completionHandler)
    }
    func answerSettings() { settings.removeFirst()(Settings()) }
    func answerGrant(_ value: Bool) { grants.removeFirst()(value, nil) }
}
final class Model: NSObject {
    let defaults = Defaults()
    let center = Center()
    var permission: UNAuthorizationStatus = .authorized
    var requestingPermission = false
    var lastDeliveryFailed = false
    var notificationIntent = UUID()
    var notificationsEnabled: Bool { defaults.bool(forKey: "notificationsEnabled") }
    var permissionAllowsNotifications: Bool { permission == .authorized || permission == .provisional }
    func updateMenu() {}
    func refreshPermission() {}
__METHODS__
}
func drain() { RunLoop.main.run(until: Date().addingTimeInterval(0.03)) }
func check(_ value: @autoclosure () -> Bool, _ name: String) {
    if !value() { print("FAIL: " + name); exit(1) }
}
let settingsRace = Model()
settingsRace.toggleNotifications()
settingsRace.disableNotifications()
settingsRace.center.answerSettings()
drain()
check(settingsRace.center.requests == 0 && !settingsRace.notificationsEnabled, "late settings response respected explicit off")

let grantRace = Model()
grantRace.toggleNotifications()
grantRace.center.answerSettings()
drain()
check(grantRace.center.grants.count == 1, "fake authorization requested")
grantRace.disableNotifications()
grantRace.center.answerGrant(true)
drain()
check(!grantRace.notificationsEnabled && !grantRace.requestingPermission, "late grant respected explicit off")

let latest = Model()
latest.toggleNotifications()
latest.center.answerSettings()
drain()
let olderGrant = latest.center.grants.removeFirst()
latest.toggleNotifications()
latest.center.answerSettings()
drain()
latest.center.answerGrant(false)
drain()
olderGrant(true, nil)
drain()
check(!latest.notificationsEnabled, "newer deny survived stale grant")

let successful = Model()
successful.toggleNotifications()
successful.center.answerSettings()
drain()
successful.center.answerGrant(true)
drain()
check(successful.notificationsEnabled && !successful.requestingPermission, "current grant still enables notifications")
print("PASS: four native notification-intent workflows; fake permission center; no OS prompts or deliveries")
'''.replace("__METHODS__", methods)
    qa = ROOT / ".local/native-notification-qa"
    qa.mkdir(parents=True, exist_ok=True)
    path, binary = qa / "main.swift", qa / "native-notification-smoke"
    path.write_text(program)
    subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(ROOT / "build/swift-cache"), str(path), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=10)


if __name__ == "__main__":
    main()
