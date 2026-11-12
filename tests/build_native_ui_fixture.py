"""Build an isolated UI fixture from the sandbox candidate, without launching.

The product sources and candidate are untouched. The sole native source change
replaces the picker's suggested home with a disposable synthetic fixture home.
An independent bundle identity keeps preferences, grants and storage separate.
"""

import argparse
import difflib
import hashlib
import json
import platform
from pathlib import Path
import plistlib
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=ROOT / "build/sandbox-build9/Agent Board Sandbox.app")
    parser.add_argument("--fixture-home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = args.fixture_home.resolve(strict=True)
    allowed = ROOT / ".local/app-store-local-hardening"
    if not fixture.is_relative_to(allowed) or fixture == allowed:
        parser.error("The home must be inside the disposable App Store validation fixture area.")
    for relative in (".codex", ".claude", "Library/Application Support/Claude/claude-code-sessions"):
        path = fixture / relative
        if path.is_symlink() or not path.is_dir():
            parser.error("All three synthetic source directories must already exist.")
    output = args.output.resolve()
    if not output.is_relative_to(allowed) or output.exists():
        parser.error("Choose a new output directory inside the local fixture area.")
    output.mkdir(parents=True)
    candidate = args.candidate.resolve(strict=True)
    app = output / "Agent Board Sandbox Fixture.app"
    shutil.copytree(candidate, app, symlinks=True)
    original = (ROOT / "MacApp.swift").read_text()
    start = original.index("    private var sourceHome: URL {")
    end = original.index("\n    func grantFolder(", start)
    replacement = "    private var sourceHome: URL {\n        URL(fileURLWithPath: " + json.dumps(str(fixture)) + ", isDirectory: true)\n    }\n"
    modified = original[:start] + replacement + original[end:]
    source = output / "main.swift"
    source.write_text(modified)
    diff = "".join(difflib.unified_diff(original.splitlines(keepends=True), modified.splitlines(keepends=True),
                                         fromfile="product/MacApp.swift", tofile="fixture/main.swift"))
    (output / "test-only-source.diff").write_text(diff)
    binary = app / "Contents/MacOS/AgentBoard"
    subprocess.run(["/usr/bin/swiftc", "-O", "-module-cache-path", str(output / "swift-cache"),
                    "-target", platform.machine() + "-apple-macos13.0", "-framework", "AppKit", "-framework", "WebKit",
                    "-framework", "UserNotifications", "-framework", "ServiceManagement",
                    str(source), str(ROOT / "CollectorRuntime.swift"), str(ROOT / "ClaudeChatStatus.swift"),
                    str(ROOT / "ChatGPTChatStatus.swift"),
                    "-o", str(binary)], check=True)
    info_path = app / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    if not info.get("AgentBoardSandboxed"):
        raise SystemExit("The source candidate must be the strict sandbox build.")
    info.update({"CFBundleIdentifier": "local.agentboard.sandbox-ui-fixture", "CFBundleName": "Agent Board Sandbox Fixture",
                 "CFBundleDisplayName": "Agent Board Sandbox Fixture"})
    info_path.write_bytes(plistlib.dumps(info))
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--entitlements",
                    str(ROOT / "entitlements/sandbox-app.plist"), str(app)], check=True)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    helper_relative = "Contents/Helpers/AgentBoardCollector.app/Contents/MacOS/agent-board-collector"
    assert digest(app / helper_relative) == digest(candidate / helper_relative)
    resources_relative = "Contents/Helpers/AgentBoardCollector.app/Contents/Resources"
    resource_files = [path for path in (candidate / resources_relative).rglob("*") if path.is_file()]
    assert all(digest(path) == digest(app / path.relative_to(candidate)) for path in resource_files)
    report = {"app": str(app), "fixture_home": str(fixture), "bundle_id": info["CFBundleIdentifier"],
              "product_main_sha256": hashlib.sha256(original.encode()).hexdigest(),
              "fixture_main_sha256": digest(source), "runtime_sha256": digest(ROOT / "CollectorRuntime.swift"),
              "helper_sha256": digest(app / helper_relative), "identical_helper_resource_files": len(resource_files),
              "substitution": "Only sourceHome picker paths refer to the disposable fixture home.",
              "grants_created": False, "app_launched": False,
              "scope": "Actual native UI and dynamic grant mechanism with synthetic source files; real provider compatibility remains unverified."}
    (output / "fixture-build.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
