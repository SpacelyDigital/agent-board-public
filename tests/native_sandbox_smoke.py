"""Exercise a signed App Sandbox parent and the actual packaged collector.

Only disposable source-denial fixtures and the harness's own app container are
used. This does not select source folders, grant permissions, read accounts,
contact providers, or establish user-approved bookmark/real-provider support.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "local.agentboard.sandbox-proof-harness"


def wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for the sandbox proof result.")


def alive(pid):
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
    return bool(result.stdout.strip() and not result.stdout.lstrip().startswith("Z"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=ROOT / "build/sandbox-build9/Agent Board Sandbox.app")
    parser.add_argument("--modes", nargs="+", choices=("normal-stop", "forced-owner-death", "collector-crash-retry", "sample-mode-isolation"),
                        default=("normal-stop", "forced-owner-death", "collector-crash-retry"))
    args = parser.parse_args()
    candidate = args.candidate.resolve(strict=True)
    candidate_info = plistlib.loads((candidate / "Contents/Info.plist").read_bytes())
    assert candidate_info.get("AgentBoardSandboxed") is True
    runtime_hash = hashlib.sha256((ROOT / "CollectorRuntime.swift").read_bytes()).hexdigest()
    helper = candidate / "Contents/Helpers/AgentBoardCollector.app/Contents/MacOS/agent-board-collector"
    helper_hash = hashlib.sha256(helper.read_bytes()).hexdigest()
    package_report = candidate.parent / "STORE-PACKAGE-CHECKS.json"
    runtime_source_verified = False
    if package_report.is_file():
        package = json.loads(package_report.read_text())
        assert runtime_hash == package["source_sha256"]["CollectorRuntime.swift"], "Runtime source changed since this candidate was built."
        assert helper_hash == package["bundle_checks"]["helper_sha256"], "The packaged helper changed."
        runtime_source_verified = True
    run_id = uuid.uuid4().hex
    qa = ROOT / ".local/app-store-local-hardening" / ("sandbox-native-" + run_id)
    qa.mkdir(parents=True)
    outside = qa / "outside/private-fixture.txt"
    outside.parent.mkdir()
    outside.write_text("A disposable denial fixture. No account or chat data.\n")
    before = hashlib.sha256(outside.read_bytes()).hexdigest()
    program = outside.with_name("benign-program")
    source = program.with_suffix(".c")
    source.write_text("int main(void) { return 0; }\n")
    subprocess.run(["/usr/bin/clang", str(source), "-o", str(program)], check=True)
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(program)], check=True)
    app = qa / "Sandbox Runtime Proof.app"
    shutil.copytree(candidate, app, symlinks=True)
    executable = app / "Contents/MacOS/AgentBoard"
    main_source = qa / "main.swift"
    shutil.copy2(ROOT / "tests/SandboxRuntimeProof.swift", main_source)
    subprocess.run(["/usr/bin/swiftc", "-module-cache-path", str(qa / "swift-cache"),
                    str(main_source), str(ROOT / "CollectorRuntime.swift"),
                    "-o", str(executable)], check=True)
    info_path = app / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info.update({"CFBundleIdentifier": BUNDLE_ID, "CFBundleDisplayName": "Sandbox Runtime Proof",
                 "LSBackgroundOnly": True, "ProbeOutsideFile": str(outside), "ProbeOutsideExecutable": str(program)})
    container = Path.home() / "Library/Containers" / BUNDLE_ID / "Data/Library/Application Support/Agent Board"
    reports = []
    for mode in dict.fromkeys(args.modes):
        mode_id = run_id + "-" + mode
        info.update({"ProbeRunID": mode_id, "ProbeMode": mode})
        info_path.write_bytes(plistlib.dumps(info))
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--entitlements",
                        str(ROOT / "entitlements/sandbox-app.plist"), str(app)], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        # Launch Services supplies the app identity and creates its sandbox
        # container. No document URL or input path is passed to it as a grant.
        subprocess.run(["/usr/bin/open", "-n", "-g", str(app)], check=True)
        report_path = container / (("sandbox-running-" if mode == "forced-owner-death" else "sandbox-proof-") + mode_id + ".json")
        wait_for(report_path.exists, 90)
        report = json.loads(report_path.read_text())
        if mode == "forced-owner-death":
            assert all(report["checks"].values()), report
            os.kill(report["parent_pid"], signal.SIGKILL)
            wait_for(lambda: not alive(report["collector_pid"]), 10)
            report["checks"]["collector_stopped_after_forced_owner_death"] = True
            private_files = report["launch_artifact_names"]
            report["checks"]["private_launch_files_removed_after_forced_owner_death"] = all(
                not (container / "data" / name).exists() for name in private_files)
            report["passed"] = True
            report["passed"] = all(report["checks"].values())
        reports.append(report)
        (qa / "reports-so-far.json").write_text(json.dumps(reports, indent=2) + "\n")
        assert report.get("passed") is True, report
    assert hashlib.sha256(outside.read_bytes()).hexdigest() == before
    result = {"passed": True, "candidate": str(candidate), "reports": reports,
              "runtime_source_sha256": runtime_hash, "packaged_helper_sha256": helper_hash,
              "runtime_source_matches_recorded_candidate": runtime_source_verified,
              "outside_fixture_unchanged": True,
              "unverified": ["NSOpenPanel source grants", "bookmark persistence and helper transfer after restart",
                             "real Claude/Codex", "notifications", "login items", "App Store signing/review"]}
    (qa / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"passed": True, "checks": sum(len(report["checks"]) for report in reports), "evidence": str(qa / "result.json")}))


if __name__ == "__main__":
    main()
