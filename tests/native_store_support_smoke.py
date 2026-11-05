"""Exercise extracted production storefront policy with fake providers only.

No StoreKit import or query, real account, app defaults, browser handoff, visible
window or network request is used. --generate-only prepares the native harness
without compiling or running it. Native WebKit and App Store end-to-end checks
remain separate release checks.
"""

import argparse
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def program():
    source = (ROOT / "MacApp.swift").read_text()
    controller = source[source.index("func fixedKoFiURL("):source.index("func externalSupportSectionScript(")]
    local = source[source.index("    func isLocalAbout("):source.index("    func canPresentExternalSupport(")]
    return r'''import Foundation
__CONTROLLER__
final class LocalOrigin {
    let boardURL = URL(string: "http://127.0.0.1:18969/")!
__LOCAL__
}
@MainActor final class Probe {
    var pending: [Int: CheckedContinuation<String?, Never>] = [:]
    var calls = 0
    var opens = 0
    var rendered: [Bool] = []
    var present = true
    var clock: TimeInterval = 100
    func make(_ strategy: String = "us-external", _ store: Bool = true) -> ExternalSupportController {
        ExternalSupportController(strategy: strategy, storeBuild: store,
            country: { [self] in
                calls += 1
                let index = calls
                return await withCheckedContinuation { pending[index] = $0 }
            }, canPresent: { self.present }, render: { self.rendered.append($0) },
            open: { self.opens += 1 }, now: { self.clock })
    }
    func resolve(_ index: Int, _ code: String?) {
        guard let continuation = pending.removeValue(forKey: index) else { fatalError("Unknown fake lookup") }
        continuation.resume(returning: code)
    }
}
@MainActor var assertions = 0
@MainActor func expect(_ value: @autoclosure () -> Bool, _ label: String) {
    assertions += 1
    if !value() { print("FAIL: " + label); exit(1) }
}
func settle() async { try? await Task.sleep(nanoseconds: 8_000_000) }
@MainActor func run() async {
    for code in [nil, "", "US", "usa", "USA ", "GBR", "CAN"] as [String?] {
        expect(!externalSupportAvailable(strategy: "us-external", storeBuild: true, countryCode: code, connected: true), "unknown/non-US result stays hidden")
    }
    expect(externalSupportAvailable(strategy: "us-external", storeBuild: true, countryCode: "USA", connected: true), "exact USA permits support")
    expect(!externalSupportAvailable(strategy: "hidden", storeBuild: true, countryCode: "USA", connected: true), "hidden strategy remains hidden in USA")
    expect(!externalSupportAvailable(strategy: "us-external", storeBuild: false, countryCode: "USA", connected: true), "Store gate is required")
    expect(!externalSupportAvailable(strategy: "us-external", storeBuild: true, countryCode: "USA", connected: false), "offline gate is required")
    for value in ["http://ko-fi.com/spacelydigital", "https://ko-fi.com/spacelydigital/", "https://ko-fi.com/spacelydigital?chat=fixture", "https://ko-fi.com/spacelydigital#x", "https://user@ko-fi.com/spacelydigital", "https://ko-fi.com:443/spacelydigital", "https://ko-fi.com.evil.invalid/spacelydigital", "https://ko-fi.com/SpacelyDigital", "mailto:Spacelydigital@gmail.com"] {
        expect(fixedKoFiURL(value) == nil, "nonexact target is rejected")
    }
    expect(fixedKoFiURL("https://ko-fi.com/spacelydigital")?.absoluteString == "https://ko-fi.com/spacelydigital", "fixed target remains exact")
    let origin = LocalOrigin()
    for value in ["http://127.0.0.1:18969/about", "http://127.0.0.1:18969/about/", "http://127.0.0.1:18969/about/index.html"] {
        expect(origin.isLocalAbout(URL(string: value)), "exact local About accepted")
    }
    expect(!origin.isLocalAbout(nil), "missing source blocked")
    for value in ["http://127.0.0.1:18969/", "http://127.0.0.1:18969/about/privacy/", "http://127.0.0.1:18969/about/?x=1", "http://user@127.0.0.1:18969/about/", "http://127.0.0.1:18970/about/", "https://127.0.0.1:18969/about/", "https://ko-fi.com/about/"] {
        expect(!origin.isLocalAbout(URL(string: value)), "foreign or wrong local source blocked")
    }
    for (strategy, store, connected, present) in [("hidden", true, true, true), ("us-external", false, true, true), ("us-external", true, false, true), ("us-external", true, true, false)] {
        let p = Probe(), controller = p.make(strategy, store)
        p.present = present; controller.connected = connected
        controller.refresh(); await settle()
        expect(p.calls == 0 && !controller.allowed && p.rendered == [false], "disabled conditions never query the provider")
    }
    do {
        let p = Probe(), controller = p.make(); controller.connected = true
        controller.refresh(); await settle(); p.resolve(1, "USA"); await settle()
        expect(controller.allowed && p.rendered.last == true, "fresh USA displays support")
        controller.refresh(); expect(!controller.allowed && p.rendered.last == false, "refresh hides previous result before await")
        await settle(); p.resolve(2, nil); await settle()
        expect(!controller.allowed && p.rendered.last == false, "nil removes former USA availability")
        controller.refresh(); await settle(); p.resolve(3, "GBR"); await settle()
        expect(!controller.allowed, "non-US removes former USA availability")
    }
    do {
        let p = Probe(), controller = p.make(); controller.connected = true
        controller.refresh(); await settle(); controller.refresh(); await settle()
        p.resolve(2, "CAN"); await settle(); p.resolve(1, "USA"); await settle()
        expect(!controller.allowed && p.rendered.last == false, "late cancelled USA cannot overwrite newer non-US result")
    }
    do {
        let p = Probe(), controller = p.make(); controller.connected = true
        controller.refresh(); controller.invalidate(); await settle()
        expect(p.calls == 0 && !controller.allowed, "cancelled scheduled task never calls provider")
    }
    do {
        let p = Probe(), controller = p.make(); controller.connected = true
        controller.refresh(); await settle(); controller.invalidate(); p.resolve(1, "USA"); await settle()
        expect(!controller.allowed && p.rendered.last == false, "stopped lookup cannot restore support")
    }
    do {
        let p = Probe(), controller = p.make(); controller.connected = true
        controller.refresh(); await settle(); controller.connected = false; p.resolve(1, "USA"); await settle()
        expect(!controller.allowed, "connection loss during lookup fails closed")
        controller.connected = true; controller.refresh(); await settle(); p.present = false
        p.resolve(2, "USA"); await settle(); expect(!controller.allowed, "inactive page cannot receive stale support")
    }
    for jump in [6.0, -1.0] {
        let p = Probe(), controller = p.make(); controller.connected = true
        controller.refresh(); await settle(); p.clock += jump; p.resolve(1, "USA"); await settle()
        expect(!controller.allowed && p.rendered.last == false, "slow or backwards-clock result fails closed")
    }
    do {
        let p = Probe(), controller = p.make(); controller.connected = true
        controller.refresh(); await settle(); p.resolve(1, "USA"); await settle()
        controller.refresh(openAfterCheck: true); await settle()
        expect(!controller.allowed && p.calls == 2 && p.opens == 0, "click performs fresh lookup rather than trusting visible USA")
        p.resolve(2, "CAN"); await settle(); expect(p.opens == 0, "storefront change blocks pending browser handoff")
        p.clock += 2; controller.refresh(openAfterCheck: true); controller.refresh(openAfterCheck: true)
        await settle(); expect(p.calls == 3, "duplicate navigation callbacks produce one lookup")
        p.resolve(3, "USA"); await settle(); expect(p.opens == 1, "fresh click result opens once")
        p.clock += 2; controller.refresh(openAfterCheck: true); await settle(); p.resolve(4, nil); await settle()
        expect(p.opens == 1 && !controller.allowed, "missing storefront blocks subsequent click")
        p.clock += 2; controller.refresh(openAfterCheck: true); await settle(); controller.invalidate(); p.resolve(5, "USA"); await settle()
        expect(p.opens == 1, "leaving About cancels pending handoff")
        expect(p.pending.isEmpty, "all controlled asynchronous lookups completed")
    }
    print("PASS: \(assertions) extracted production policy assertions with fake storefront, clock, presentation and opener")
    exit(0)
}
Task { @MainActor in await run() }
RunLoop.main.run()
'''.replace("__CONTROLLER__", controller).replace("__LOCAL__", local)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generate-only", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/native-store-support-qa")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    swift = args.output / "main.swift"
    swift.write_text(program())
    if args.generate_only:
        print("Prepared fake-provider harness; no compiler or native execution was started.")
        return
    binary = args.output / "native-store-support"
    subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(args.output / "swift-cache"),
                    "-target", "arm64-apple-macos13.0", str(swift), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, timeout=15)


if __name__ == "__main__":
    main()
