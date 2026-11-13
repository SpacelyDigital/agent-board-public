#!/usr/bin/env python3
"""Create a self-contained Mac download without installing or opening it."""

import argparse
import hashlib
import json
import platform
from pathlib import Path
import plistlib
import shutil
import subprocess

from install import ROOT, build_app
from build_support import AppConfiguration, validate_bundle


def run(*arguments):
    subprocess.run(list(map(str, arguments)), check=True)


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-number", required=True)
    parser.add_argument("--sign", metavar="DEVELOPER_ID", help="Developer ID Application signing identity; omission creates a local preview only.")
    parser.add_argument("--notarize-profile", metavar="KEYCHAIN_PROFILE", help="Explicitly submit the signed app to Apple, then staple the ticket. Requires --sign.")
    parser.add_argument("--skip-build", action="store_true", help="Package the already built app, useful after a local validation pass.")
    args = parser.parse_args(argv)
    if args.notarize_profile and not args.sign:
        parser.error("Notarization requires a Developer ID Application identity with --sign.")
    try:
        configuration = AppConfiguration("local.agentboard.display", args.version, args.build_number)
    except ValueError as exc:
        parser.error(str(exc))
    return args, configuration


def verify_release_identity(app, configuration):
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    helper = app / "Contents/Helpers/AgentBoardCollector.app/Contents/Info.plist"
    helper_info = plistlib.loads(helper.read_bytes())
    for metadata, identifier in ((info, configuration.bundle_id), (helper_info, configuration.helper_id)):
        if (metadata.get("CFBundleIdentifier"), metadata.get("CFBundleShortVersionString"),
                metadata.get("CFBundleVersion")) != (identifier, configuration.version, configuration.build_number):
            raise SystemExit("The app and collector must match the requested direct release identity.")
        if metadata.get("AgentBoardSandboxed") or metadata.get("AgentBoardDistributionChannel", "").startswith("store-"):
            raise SystemExit("Store/sandbox candidates use build_store.py.")
    return info


def main():
    args, configuration = parse_arguments()
    release_name = f"direct-{configuration.version}-build{configuration.build_number}"
    build_root = ROOT / "build" / release_name
    destination = ROOT / "dist" / release_name
    if any(path.exists() or path.is_symlink() for path in (build_root, destination)):
        raise SystemExit("Use a fresh build number. A previous output has that identity.")
    if args.skip_build:
        app = ROOT / "build/Agent Board.app"
    else:
        build_root.mkdir(parents=True)
        app = build_app(args.sign, build_root=build_root, configuration=configuration)
    if not app.is_dir():
        raise SystemExit("Build the app before using --skip-build.")
    info = verify_release_identity(app, configuration)
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", app)
    if args.sign:
        signature = subprocess.run(["/usr/bin/codesign", "-d", "--verbose=4", str(app)], capture_output=True, text=True, check=True)
        if "Authority=Developer ID Application:" not in signature.stderr:
            raise SystemExit("The built app is not signed with a Developer ID Application certificate.")
    if (app / "Contents/_MASReceipt").exists():
        raise SystemExit("Build from clean source; a recipient's App Store receipt cannot be redistributed.")
    validate_bundle(app, info["LSMinimumSystemVersion"])
    destination.mkdir(parents=True)
    stem = f"Agent-Board-{configuration.version}-build{configuration.build_number}-macOS-{platform.machine()}"
    if not args.notarize_profile:
        stem += "-local-preview"
    staging = build_root / "download"
    staging.mkdir(parents=True)
    bundled_app = staging / app.name
    shutil.copytree(app, bundled_app, symlinks=True)
    zip_path = destination / f"{stem}.zip"
    run("/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", bundled_app, zip_path)
    if args.notarize_profile:
        run("/usr/bin/xcrun", "notarytool", "submit", zip_path, "--keychain-profile", args.notarize_profile, "--wait")
        run("/usr/bin/xcrun", "stapler", "staple", bundled_app)
        run("/usr/bin/xcrun", "stapler", "validate", bundled_app)
        run("/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=2", bundled_app)
        zip_path.unlink()
        run("/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", bundled_app, zip_path)
    (staging / "Applications").symlink_to("/Applications", target_is_directory=True)
    instructions = "Agent Board\n\nDrag Agent Board.app to Applications, then open it.\nRequires macOS 13 or later and a Mac matching this download's architecture.\nNo Python, Xcode, Terminal commands, or separate server installation are required.\n\nOn first open, review local monitoring and choose optional account reads, notifications, and opening at login. Local tracking works without enabling those options.\n\nClose the window to keep monitoring in the menu bar. Choose Quit Agent Board to stop monitoring. Your saved board stays in your user Library.\n"
    if not args.notarize_profile:
        signature_state = "Developer ID signed" if args.sign else "ad-hoc signed"
        instructions += f"\nLOCAL PREVIEW: This build is {signature_state} and has not been notarized. It is for local validation, not a public download. The normal downloaded-app Gatekeeper flow has not been approved.\n"
    (staging / "Read me.txt").write_text(instructions)
    dmg_path = destination / f"{stem}.dmg"
    run("/usr/bin/hdiutil", "create", "-volname", "Agent Board", "-srcfolder", staging, "-format", "UDZO", "-fs", "HFS+", dmg_path)
    if args.sign:
        run("/usr/bin/codesign", "--force", "--sign", args.sign, "--timestamp", dmg_path)
    if args.notarize_profile:
        run("/usr/bin/xcrun", "notarytool", "submit", dmg_path, "--keychain-profile", args.notarize_profile, "--wait")
        run("/usr/bin/xcrun", "stapler", "staple", dmg_path)
        run("/usr/bin/xcrun", "stapler", "validate", dmg_path)
    manifest = {
        "version": configuration.version, "build": int(configuration.build_number),
        "bundle_id": configuration.bundle_id, "architecture": platform.machine(), "minimum_macos": info["LSMinimumSystemVersion"],
        "developer_id_signed": bool(args.sign), "notarized": bool(args.notarize_profile),
        "gatekeeper_artifacts_prepared": bool(args.sign and args.notarize_profile),
        "artifacts": [{"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
                      for path in (zip_path, dmg_path)],
    }
    (destination / f"{stem}.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
