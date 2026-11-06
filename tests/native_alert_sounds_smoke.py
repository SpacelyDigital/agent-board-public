"""Compile real native sound selection and preference filtering, never deliver alerts."""

from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / "MacApp.swift").read_text()
    sound = source[source.index("enum AgentBoardAlertSound {"):source.index("struct AttentionChat {")]
    begin = source.index('            let allowed: [String: Set<String>] = [', source.index('body["kind"] as? String == "displayPreferences"'))
    end = source.index('            installAccessScript()', begin)
    filter_code = source[begin:end]
    program = '''import Foundation
__SOUND__
final class Defaults {
    var saved: [String: String] = [:]
    func set(_ value: [String: String], forKey key: String) { saved = value }
}
final class Bridge {
    let defaults = Defaults()
    let displayPreferencesKey = "fixture"
    func receive(_ values: [String: String]) {
        guard values.count <= 9 else { return }
__FILTER__
    }
}
func check(_ value: Bool, _ name: String) { if !value { print("FAIL " + name); exit(1) } }
let bridge = Bridge()
check(AgentBoardAlertSound.filename(preferences: [:]) == nil, "fresh Sound off is silent")
check(AgentBoardAlertSound.filename(preferences: ["agent-board:sound":"on"]) == "agent-board-soft-ding.wav", "legacy default uses original ding")
for id in AgentBoardAlertSound.ids {
    bridge.receive(["agent-board:sound":"on", "agent-board:sound-id":id])
    check(AgentBoardAlertSound.filename(preferences: bridge.defaults.saved) == "agent-board-" + id + ".wav", "selected native filename")
    bridge.receive(["agent-board:sound":"off", "agent-board:sound-id":id])
    check(AgentBoardAlertSound.filename(preferences: bridge.defaults.saved) == nil, "mute retains choice without notification audio")
}
for invalid in ["../private.wav", "https://example.invalid/sound.wav", "FART", "unreviewed"] {
    bridge.receive(["agent-board:sound":"on", "agent-board:sound-id":invalid])
    check(bridge.defaults.saved["agent-board:sound-id"] == nil, "bridge drops unreviewed name")
    check(AgentBoardAlertSound.filename(preferences: bridge.defaults.saved) == "agent-board-soft-ding.wav", "invalid name falls back locally")
}
bridge.receive(["agent-board:sound":"on", "agent-board:sound-id":"fart", "agent-board:layout":"ledger", "agent-board:theme-id":"terminal", "agent-board:mode":"dark", "agent-board:motion":"false", "agent-board:selected":"fixture", "agent-board:palette":"terminal", "agent-board:theme":"dark"])
check(bridge.defaults.saved.count == 9, "all existing preferences plus sound choice survive")
print("PASS: real native sound choice and nine-key preference filtering; no notification delivery")
'''.replace("__SOUND__", sound).replace("__FILTER__", filter_code)
    with tempfile.TemporaryDirectory(prefix="agent-board-audio-", dir=ROOT / ".local") as directory:
        qa = Path(directory); swift = qa / "main.swift"; binary = qa / "native-audio"
        swift.write_text(program)
        subprocess.run(["/usr/bin/swiftc", "-target", "arm64-apple-macos13.0", "-module-cache-path", str(qa / "cache"), str(swift), "-o", str(binary)], check=True, timeout=60)
        subprocess.run([str(binary)], check=True, timeout=10)


if __name__ == "__main__":
    main()
