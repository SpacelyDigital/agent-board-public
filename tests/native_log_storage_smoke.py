"""Exercise the actual native log helper with only owned disposable files.

No app GUI, collector, source grant, account, signing key or network request is
used. Compile the complete production Runtime; call only its log helper and
Sample storage validation. This is supporting native file-I/O evidence, not an
installed-product or App Sandbox escape test.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    qa = args.output or ROOT / ".local/native-log-storage-qa" / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8])
    qa.mkdir(parents=True, mode=0o700, exist_ok=False)
    runtime = ROOT / "CollectorRuntime.swift"
    before = runtime.read_bytes()
    program = r'''import Foundation
import Darwin

let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
var results: [[String: Any]] = []
func check(_ condition: @autoclosure () throws -> Bool, _ label: String) throws {
    let passed = try condition()
    results.append(["case": label, "passed": passed])
    if !passed { throw NSError(domain: "DisposableLogFixture", code: 1, userInfo: [NSLocalizedDescriptionKey: label]) }
}
func folder(_ name: String) throws -> URL {
    let value = root.appendingPathComponent(name, isDirectory: true)
    try FileManager.default.createDirectory(at: value, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    return value
}
func mode(_ path: URL) throws -> Int { (try FileManager.default.attributesOfItem(atPath: path.path)[.posixPermissions] as! NSNumber).intValue }
func append(_ directory: URL, text: String = "new log entry\n") throws {
    let handle = try CollectorRuntime.openCollectorLog(in: directory)
    let descriptor = handle.fileDescriptor
    defer { try? handle.close() }
    let bits = fcntl(descriptor, F_GETFD)
    try check(bits >= 0 && bits & FD_CLOEXEC != 0, "returned log descriptor is close-on-exec")
    try check(fcntl(descriptor, F_GETFL) & O_APPEND != 0, "returned log descriptor appends")
    try handle.write(contentsOf: Data(text.utf8))
    try handle.close()
    try check(fcntl(descriptor, F_GETFD) == -1 && errno == EBADF, "closing returned log closes its descriptor")
}
func denied(_ directory: URL, _ label: String) throws {
    let started = Date()
    var rejected = false
    do { let handle = try CollectorRuntime.openCollectorLog(in: directory); try handle.close() }
    catch { rejected = true }
    try check(rejected, label)
    try check(Date().timeIntervalSince(started) < 1, label + " without blocking")
}
func noOwnedDescriptors() -> Bool {
    for descriptor: Int32 in 3..<1024 {
        var path = [CChar](repeating: 0, count: Int(MAXPATHLEN))
        if fcntl(descriptor, F_GETPATH, &path) == 0,
           String(cString: path).hasPrefix(root.path + "/") { return false }
    }
    return true
}

do {
    let ordinary = try folder("ordinary")
    try append(ordinary, text: "first\n")
    try check(try mode(ordinary.appendingPathComponent("collector.log")) == 0o600, "new log is private 0600")
    try append(ordinary, text: "second\n")
    try check(try String(contentsOf: ordinary.appendingPathComponent("collector.log"), encoding: .utf8) == "first\nsecond\n", "reuse preserves earlier data and appends")

    let publicMode = try folder("public-mode")
    let publicLog = publicMode.appendingPathComponent("collector.log")
    try Data("original\n".utf8).write(to: publicLog)
    try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: publicLog.path)
    try append(publicMode)
    try check(try mode(publicLog) == 0o600, "existing regular log is tightened to 0600")

    for kind in ["symbolic", "hard", "fifo", "directory"] {
        let directory = try folder("current-" + kind)
        let target = directory.appendingPathComponent("owned-unrelated-canary")
        let log = directory.appendingPathComponent("collector.log")
        let canary = Data("must remain unchanged\n".utf8)
        try canary.write(to: target)
        try FileManager.default.setAttributes([.posixPermissions: 0o640], ofItemAtPath: target.path)
        if kind == "symbolic" { try FileManager.default.createSymbolicLink(at: log, withDestinationURL: target) }
        if kind == "hard" { try FileManager.default.linkItem(at: target, to: log) }
        if kind == "fifo" { guard mkfifo(log.path, 0o600) == 0 else { fatalError("fixture FIFO") } }
        if kind == "directory" { try FileManager.default.createDirectory(at: log, withIntermediateDirectories: true); try canary.write(to: log.appendingPathComponent("keep")) }
        try denied(directory, "current " + kind + " is rejected")
        try check(try Data(contentsOf: target) == canary && mode(target) == 0o640, "current " + kind + " preserves unrelated bytes and permissions")
        if kind == "directory" { try check(try Data(contentsOf: log.appendingPathComponent("keep")) == canary, "current directory contents survive") }
    }

    for kind in ["symbolic", "hard", "fifo", "directory"] {
        let directory = try folder("rotation-" + kind)
        let target = directory.appendingPathComponent("owned-unrelated-canary")
        let previous = directory.appendingPathComponent("collector.previous.log")
        let log = directory.appendingPathComponent("collector.log")
        let canary = Data("rotation must preserve this\n".utf8)
        let large = Data(repeating: 65, count: 2_000_001)
        try canary.write(to: target)
        try large.write(to: log)
        if kind == "symbolic" { try FileManager.default.createSymbolicLink(at: previous, withDestinationURL: target) }
        if kind == "hard" { try FileManager.default.linkItem(at: target, to: previous) }
        if kind == "fifo" { guard mkfifo(previous.path, 0o600) == 0 else { fatalError("fixture FIFO") } }
        if kind == "directory" { try FileManager.default.createDirectory(at: previous, withIntermediateDirectories: true); try canary.write(to: previous.appendingPathComponent("keep")) }
        try denied(directory, "rotation " + kind + " is rejected")
        try check(try Data(contentsOf: target) == canary && Data(contentsOf: log) == large, "rotation " + kind + " preserves canary and current log bytes")
        if kind == "directory" { try check(try Data(contentsOf: previous.appendingPathComponent("keep")) == canary, "rotation never recursively removes an unexpected directory") }
    }

    let rotating = try folder("normal-rotation")
    let log = rotating.appendingPathComponent("collector.log")
    let previous = rotating.appendingPathComponent("collector.previous.log")
    let firstLarge = Data(repeating: 65, count: 2_000_001)
    try firstLarge.write(to: log)
    try append(rotating, text: "fresh\n")
    try check(try Data(contentsOf: previous) == firstLarge && String(contentsOf: log, encoding: .utf8) == "fresh\n", "rotation retains earlier log and starts a new one")
    try check(try mode(previous) == 0o600 && mode(log) == 0o600, "rotated and new logs are 0600")
    let secondLarge = Data(repeating: 66, count: 2_000_001)
    try secondLarge.write(to: log)
    try append(rotating, text: "fresh again\n")
    try check(try Data(contentsOf: previous) == secondLarge, "second rotation safely replaces the validated previous regular log")
    try check(try Set(FileManager.default.contentsOfDirectory(atPath: rotating.path)) == Set(["collector.log", "collector.previous.log"]), "rotation creates exactly two named logs")

    let foreign = try folder("directory-alias-target")
    let alias = root.appendingPathComponent("directory-alias")
    try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: foreign)
    try denied(alias, "linked log directory is rejected")
    try check(try FileManager.default.contentsOfDirectory(atPath: foreign.path).isEmpty, "linked directory target receives no files")

    let bundleURL = try folder("Dummy.app/Contents")
    let info: [String: Any] = ["CFBundleIdentifier": "local.agentboard.dummy-log-fixture", "AgentBoardSandboxed": true, "AgentBoardStoreDistribution": true]
    try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0).write(to: bundleURL.appendingPathComponent("Info.plist"))
    let bundle = Bundle(url: bundleURL.deletingLastPathComponent())!
    let support = try folder("sample-support")
    let sampleData = try folder("sample-support/Samples/data")
    let runtime = CollectorRuntime(bundle: bundle, support: support)
    try runtime.setSampleMode(true)
    try check(runtime.sampleMode && runtime.support.path == support.appendingPathComponent("Samples").path, "actual Sample mode selects isolated storage")
    try append(sampleData)
    try check(!FileManager.default.fileExists(atPath: support.appendingPathComponent("data/collector.log").path), "Sample append does not create a live log")
    try runtime.setSampleMode(false)
    let liveData = try folder("sample-support/data")
    try append(liveData)
    try check(try mode(liveData.appendingPathComponent("collector.log")) == 0o600 && mode(sampleData.appendingPathComponent("collector.log")) == 0o600, "live and Sample logs remain independently private")

    let launches = try folder("launches")
    func privateEntry(_ name: String, in directory: URL, text: String = "") throws -> URL {
        let path = directory.appendingPathComponent(name)
        try Data(text.utf8).write(to: path)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        return path
    }
    let liveID = UUID().uuidString
    let lease = try NativeLaunchLease(in: launches, instance: liveID)
    let liveMarker = launches.appendingPathComponent(".collector-launch-" + liveID + ".lock")
    let liveToken = try privateEntry(".collector-access-" + liveID, in: launches, text: String(repeating: "a", count: 64))
    let liveManifest = try privateEntry(".source-bookmarks-" + liveID + ".json", in: launches, text: "{}")
    try check(try NativeLaunchLease.sweep(in: launches).isEmpty, "a second launch preserves the live owner's shared lock")
    let childLock = open(liveMarker.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
    guard childLock >= 0, flock(childLock, LOCK_SH | LOCK_NB) == 0 else { fatalError("fixture child lock") }
    lease.finish()
    try check(FileManager.default.fileExists(atPath: liveToken.path), "owner exit preserves secrets while its collector holds the lease")
    try check(try NativeLaunchLease.sweep(in: launches).isEmpty, "collector lease independently prevents startup sweeping")
    Darwin.close(childLock)
    try check(try NativeLaunchLease.sweep(in: launches) == [liveID], "orphaned lock permits only its launch's cleanup")
    try check(!FileManager.default.fileExists(atPath: liveToken.path)
              && !FileManager.default.fileExists(atPath: liveManifest.path)
              && !FileManager.default.fileExists(atPath: liveMarker.path), "orphan token, bookmark and marker are removed")
    let legacy = try privateEntry(".collector-access-" + UUID().uuidString, in: launches, text: String(repeating: "b", count: 64))
    let unrelated = try privateEntry("keep-this-file", in: launches, text: "Preserve the fixture.")
    try check(try NativeLaunchLease.sweep(in: launches).isEmpty
              && FileManager.default.fileExists(atPath: legacy.path)
              && FileManager.default.fileExists(atPath: unrelated.path), "unmarked legacy and unrelated files are preserved")
    for kind in ["symlink", "hardlink", "public", "directory"] {
        let directory = try folder("launch-invalid-" + kind)
        let id = UUID().uuidString
        let marker = try privateEntry(".collector-launch-" + id + ".lock", in: directory)
        let target = try privateEntry("private-canary", in: directory, text: "Preserve this content.")
        let token = directory.appendingPathComponent(".collector-access-" + id)
        if kind == "symlink" { try FileManager.default.createSymbolicLink(at: token, withDestinationURL: target) }
        if kind == "hardlink" { try FileManager.default.linkItem(at: target, to: token) }
        if kind == "public" { try Data("keep".utf8).write(to: token); try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: token.path) }
        if kind == "directory" { try FileManager.default.createDirectory(at: token, withIntermediateDirectories: false) }
        try check(try NativeLaunchLease.sweep(in: directory).isEmpty, "unsafe " + kind + " launch entry is not swept")
        try check(FileManager.default.fileExists(atPath: token.path) && FileManager.default.fileExists(atPath: marker.path)
                  && (try String(contentsOf: target, encoding: .utf8)) == "Preserve this content.", "unsafe " + kind + " preserves marker and target")
    }
    let raced = try folder("launch-created-marker-race")
    let markerName = ".collector-launch-" + UUID().uuidString + ".lock"
    let markerPath = try privateEntry(markerName, in: raced)
    let directoryFD = open(raced.path, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
    let markerFD = open(markerPath.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
    guard directoryFD >= 0, markerFD >= 0 else { fatalError("fixture race descriptors") }
    try check(NativeLaunchLease.markerMatchesDescriptor(markerFD, directory: directoryFD, name: markerName, path: raced), "new named marker validates before ownership")
    try FileManager.default.removeItem(at: markerPath)
    guard flock(markerFD, LOCK_SH | LOCK_NB) == 0 else { fatalError("fixture late shared lock") }
    try check(!NativeLaunchLease.markerMatchesDescriptor(markerFD, directory: directoryFD, name: markerName, path: raced), "a sweep between create and lock cannot authorize an unlinked launch")
    _ = try privateEntry(markerName, in: raced)
    try check(!NativeLaunchLease.markerMatchesDescriptor(markerFD, directory: directoryFD, name: markerName, path: raced), "a replacement marker cannot inherit the removed descriptor's lock")
    Darwin.close(markerFD); Darwin.close(directoryFD)

    try check(noOwnedDescriptors(), "normal and rejected log operations leave no owned file descriptors")
    let encoded = try JSONSerialization.data(withJSONObject: ["passed": true, "assertions": results.count, "results": results], options: [.prettyPrinted, .sortedKeys])
    try encoded.write(to: root.appendingPathComponent("RESULT.json"))
} catch {
    let encoded = try! JSONSerialization.data(withJSONObject: ["passed": false, "results": results, "error": error.localizedDescription], options: [.prettyPrinted, .sortedKeys])
    try! encoded.write(to: root.appendingPathComponent("RESULT.json"))
    exit(1)
}
'''
    main_file = qa / "main.swift"
    main_file.write_text(program)
    binary = qa / "native-log-storage"
    compile_command = ["/usr/bin/xcrun", "swiftc", "-module-cache-path", str(qa / "swift-cache"), str(runtime), str(main_file), "-o", str(binary)]
    compiled = subprocess.run(compile_command, capture_output=True, text=True, timeout=90)
    (qa / "compile.stdout").write_text(compiled.stdout)
    (qa / "compile.stderr").write_text(compiled.stderr)
    if compiled.returncode:
        raise RuntimeError("Native fixture compile failed; evidence retained at " + str(qa))
    ran = subprocess.run([str(binary), str(qa)], capture_output=True, text=True, timeout=20)
    (qa / "run.stdout").write_text(ran.stdout)
    (qa / "run.stderr").write_text(ran.stderr)
    provenance = {"runtime_sha256": hashlib.sha256(before).hexdigest(), "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "swift_fixture_sha256": hashlib.sha256(main_file.read_bytes()).hexdigest(), "compile_exit": compiled.returncode, "run_exit": ran.returncode, "source_unchanged": runtime.read_bytes() == before, "scope": "Actual production log, launch-lock cleanup and Sample storage helpers with dummy files; no collector, GUI, provider/account/grant, key or networking."}
    if (qa / "RESULT.json").is_file():
        provenance["result_sha256"] = hashlib.sha256((qa / "RESULT.json").read_bytes()).hexdigest()
    if args.baseline:
        old = args.baseline.read_text()
        start = old.index('            let data = support.appendingPathComponent("data")', old.index('    func start(completion:'))
        end = old.index('            let child = Process()', start)
        block = old[start:end]
        control = r'''import Foundation
let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
var results: [[String: Any]] = []
for kind in ["symbolic", "hard", "rotation-directory"] {
    let folder = root.appendingPathComponent(kind)
    let support = folder.appendingPathComponent("support")
    let fixtureData = support.appendingPathComponent("data")
    try FileManager.default.createDirectory(at: fixtureData, withIntermediateDirectories: true)
    let target = folder.appendingPathComponent("unrelated-canary")
    let logURL = fixtureData.appendingPathComponent("collector.log")
    let original = Data("unchanged\n".utf8)
    try original.write(to: target)
    var preserved = target
    if kind == "symbolic" { try FileManager.default.createSymbolicLink(at: logURL, withDestinationURL: target) }
    if kind == "hard" { try FileManager.default.linkItem(at: target, to: logURL) }
    if kind == "rotation-directory" {
        let previous = fixtureData.appendingPathComponent("collector.previous.log")
        try FileManager.default.createDirectory(at: previous, withIntermediateDirectories: true)
        preserved = previous.appendingPathComponent("must-remain")
        try original.write(to: preserved)
        try Data(repeating: 65, count: 2_000_001).write(to: logURL)
    }
    var logHandle: FileHandle?
__BLOCK__
    try logHandle?.write(contentsOf: Data("appended\n".utf8))
    try logHandle?.close()
    let retained = (try? Data(contentsOf: preserved)) == original
    results.append(["case": kind, "preserved": retained])
    if retained { fatalError("Baseline control did not reproduce known defect") }
}
try JSONSerialization.data(withJSONObject: ["expected_defects_reproduced": true, "results": results], options: [.prettyPrinted, .sortedKeys]).write(to: root.appendingPathComponent("RESULT.json"))
'''.replace("__BLOCK__", block)
        control_dir = qa / "baseline"
        control_dir.mkdir(mode=0o700)
        control_file = control_dir / "main.swift"
        control_file.write_text(control)
        control_build = subprocess.run(["/usr/bin/xcrun", "swiftc", "-module-cache-path", str(qa / "swift-cache"), str(control_file), "-o", str(control_dir / "control")], capture_output=True, text=True, timeout=90)
        (control_dir / "compile.stdout").write_text(control_build.stdout)
        (control_dir / "compile.stderr").write_text(control_build.stderr)
        provenance["baseline_compile_exit"] = control_build.returncode
        if control_build.returncode == 0:
            control_run = subprocess.run([str(control_dir / "control"), str(control_dir)], capture_output=True, text=True, timeout=20)
            (control_dir / "run.stdout").write_text(control_run.stdout)
            (control_dir / "run.stderr").write_text(control_run.stderr)
            provenance["baseline_run_exit"] = control_run.returncode
            if (control_dir / "RESULT.json").is_file():
                provenance["baseline_result_sha256"] = hashlib.sha256((control_dir / "RESULT.json").read_bytes()).hexdigest()
        provenance["baseline_source_sha256"] = hashlib.sha256(args.baseline.read_bytes()).hexdigest()
    (qa / "PROVENANCE.json").write_text(json.dumps(provenance, indent=2) + "\n")
    if ran.returncode or not provenance["source_unchanged"]:
        raise RuntimeError("Native log fixture failed; evidence retained at " + str(qa))
    if args.baseline and (provenance.get("baseline_compile_exit") != 0 or provenance.get("baseline_run_exit") != 0):
        raise RuntimeError("Baseline control failed; evidence retained at " + str(qa))
    print(json.dumps({"qa": str(qa), "assertions": json.loads((qa / "RESULT.json").read_text())["assertions"], "provenance": provenance}, indent=2))


if __name__ == "__main__":
    main()
