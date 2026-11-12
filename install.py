#!/usr/bin/env python3
"""Build a self-contained Mac app; optionally manage the legacy dev service."""

import argparse
import json
import os
import platform
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
import wave

ROOT = Path(__file__).resolve().parent
LABEL = "local.agent-board"
SUPPORT = Path.home() / "Library" / "Application Support" / "Agent Board"
INSTALLED_APP = Path("/Applications/Agent Board.app")
ALERT_SOUND_IDS = ("soft-ding", "chime", "bell", "glass", "marimba", "arcade", "radar", "space-signal", "pop", "fart")
RUNTIME_FILES = (
    "server.py", "board_store.py", "codex_source.py", "claude_source.py", "claude_project_source.py", "chat_links.py", "usage_source.py", "process_reader.py",
    "chatgpt_source.py", "chatgpt_status.py", "chatgpt_monitor.py", "chatgpt_reply_source.py",
    "claude_chat_source.py", "claude_chat_monitor.py",
    "privacy_network.py", "sandbox_sources.py", "source_access.py", "local_quota.py",
    "static/index.html", "static/app.js", "static/style.css", "static/palettes.css",
    "static/themes.js", "static/effects.js", "static/effects.css",
    "static/sounds/soft-ding.wav", "static/sounds/chime.wav", "static/sounds/bell.wav", "static/sounds/glass.wav",
    "static/sounds/marimba.wav", "static/sounds/arcade.wav", "static/sounds/radar.wav", "static/sounds/space-signal.wav",
    "static/sounds/pop.wav", "static/sounds/fart.wav",
    "static/icons/claude.svg", "static/icons/codex.svg",
    "static/designs/index.html", "static/designs/designs.css", "static/designs/designs.js",
    "static/colors/index.html", "static/colors/colors.css", "static/colors/colors.js",
    "static/about/index.html", "static/about/about.css", "static/about/about.js",
    "static/about/ko-fi.svg", "static/about/ko-fi.png",
    "integrations/.claude-plugin/marketplace.json",
    "integrations/claude-local-usage/.claude-plugin/plugin.json",
    "integrations/claude-local-usage/hooks/hooks.json",
    "integrations/claude-local-usage/hooks/register.js",
    "integrations/claude-local-usage/README.md",
)


def copy_alert_sounds(resources):
    """Stage fixed original PCM files in the main bundle for native notifications."""
    for sound_id in ALERT_SOUND_IDS:
        source = ROOT / "static/sounds" / (sound_id + ".wav")
        if (source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(ROOT)
                or any(parent.is_symlink() for parent in source.parents if parent.is_relative_to(ROOT))):
            raise ValueError("An original alert sound is missing or substituted.")
        with wave.open(str(source), "rb") as audio:
            if (audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 22050
                    or audio.getcomptype() != "NONE" or not 0 < audio.getnframes() / audio.getframerate() < 2
                    or len(audio.readframes(audio.getnframes())) != audio.getnframes() * 2):
                raise ValueError("Alert sounds must be short mono linear PCM WAV files.")
        target = resources / ("agent-board-" + sound_id + ".wav")
        shutil.copyfile(source, target)
        target.chmod(0o644)


def copy_license_notices(resources):
    """Stage public license notices with recipient-readable permissions before signing."""
    notices = ROOT / "THIRD_PARTY.md"
    licenses = ROOT / "licenses"
    if not notices.is_file() or not licenses.is_dir():
        raise SystemExit("The bundled-runtime license notices are missing.")
    notice_target = resources / "THIRD_PARTY.md"
    shutil.copy2(notices, notice_target)
    notice_target.chmod(0o644)
    target = resources / "licenses"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(licenses, target)
    for path in (target, *target.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            path.chmod(0o755)
        elif stat.S_ISREG(mode):
            path.chmod(0o644)
        else:
            raise SystemExit("The bundled-runtime license notices are invalid.")


def build_app(signing_identity=None, *, sandboxed=False, build_root=None, configuration=None):
    encryption_metadata = configuration.encryption_metadata() if configuration else {}
    if configuration and configuration.signing_channel.startswith("store-") and not sandboxed:
        raise ValueError("Store builds must enable App Sandbox.")
    if configuration and configuration.signing_channel == "store-distribution" and not configuration.provisioning_profile:
        from build_support import validate_profile_free_capabilities
        validate_profile_free_capabilities()
    output = Path(build_root) if build_root is not None else ROOT / "build" / ("sandbox-build9" if sandboxed else "")
    if sandboxed and configuration is None and output.exists():
        raise SystemExit("This sandbox checkpoint already exists and will not be replaced. Use build_store.py with a new --output directory and build number.")
    app = output / ("Agent Board.app" if configuration else "Agent Board Sandbox.app" if sandboxed else "Agent Board.app")
    contents = app / "Contents"
    binaries = contents / "MacOS"
    binaries.mkdir(parents=True, exist_ok=True)
    cache = output / "swift-cache"
    cache.mkdir(parents=True, exist_ok=True)
    native = output / "native"
    native.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "MacApp.swift", native / "main.swift")
    subprocess.run(["/usr/bin/swiftc", "-O", "-module-cache-path", str(cache),
                    "-target", platform.machine() + "-apple-macos13.0",
                    *(["-D", "AGENT_BOARD_DIRECT"] if not sandboxed else []),
                    "-framework", "AppKit", "-framework", "WebKit", "-framework", "UserNotifications", "-framework", "ServiceManagement",
                    "-framework", "StoreKit", "-framework", "Network",
                    str(native / "main.swift"), str(ROOT / "CollectorRuntime.swift"), str(ROOT / "ClaudeChatStatus.swift"),
                    str(ROOT / "ChatGPTChatStatus.swift"),
                    "-o", str(binaries / "AgentBoard")], check=True)
    from build_support import copy_collector, validate_bundle, codesign_command, copy_privacy_manifest, xcode_build_metadata
    legacy_collector = contents / "Helpers" / "collector"
    if legacy_collector.exists():
        shutil.rmtree(legacy_collector)
    copy_collector(contents / "Helpers" / "AgentBoardCollector.app", signing_identity,
                   sandboxed=sandboxed, build_root=output, configuration=configuration)
    resources = contents / "Resources"
    resources.mkdir(exist_ok=True)
    copy_license_notices(resources)
    copy_alert_sounds(resources)
    copy_privacy_manifest(resources)
    icon = ROOT / "assets/AgentBoard.icns"
    if icon.is_symlink() or not icon.is_file():
        raise SystemExit("The app icon is missing. Rebuild it with tools/build_icon.swift and iconutil.")
    shutil.copy2(icon, resources / "AgentBoard.icns")
    info = {
        "CFBundleExecutable": "AgentBoard", "CFBundleIdentifier": "local.agentboard.display",
        "CFBundleName": "Agent Board", "CFBundleDisplayName": "Agent Board",
        "CFBundleShortVersionString": "0.2.5", "CFBundleVersion": "9",
        "CFBundlePackageType": "APPL", "CFBundleIconFile": "AgentBoard.icns", "NSHighResolutionCapable": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSAppDataUsageDescription": "Agent Board reads Codex and Claude’s local session files to show which chats are working or need your attention.",
        "LSMinimumSystemVersion": "13.0",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
        "NSHumanReadableCopyright": "Copyright © 2026 Spacely Digital.",
    }
    if sandboxed:
        info.update({"CFBundleIdentifier": "local.agentboard.sandbox-proof", "CFBundleDisplayName": "Agent Board Sandbox",
                     "AgentBoardSandboxed": True})
    if configuration:
        info.update({"CFBundleIdentifier": configuration.bundle_id,
                     "CFBundleDisplayName": configuration.display_name,
                     "CFBundleShortVersionString": configuration.version,
                     "CFBundleVersion": configuration.build_number})
        if configuration.signing_channel.startswith("store-"):
            info["AgentBoardDistributionChannel"] = configuration.signing_channel
            info["AgentBoardStoreDistribution"] = True
            info["AgentBoardStoreSupportStrategy"] = configuration.store_support_strategy
            info.update(encryption_metadata)
            info.update(xcode_build_metadata())
    if configuration and configuration.provisioning_profile:
        shutil.copy2(configuration.provisioning_profile, contents / "embedded.provisionprofile")
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    verified = validate_bundle(app, info["LSMinimumSystemVersion"])
    (output / "native-compatibility.json").write_text(json.dumps(verified, indent=2) + "\n")
    signature = codesign_command(signing_identity, configuration.signing_channel if configuration else "direct")
    if sandboxed:
        entitlements = ROOT / "entitlements/sandbox-app.plist"
        if configuration and configuration.signing_channel == "store-distribution" and configuration.provisioning_profile:
            values = plistlib.loads(entitlements.read_bytes())
            values["com.apple.application-identifier"] = configuration.team_id + "." + configuration.bundle_id
            values["com.apple.developer.team-identifier"] = configuration.team_id
            entitlements = output / "store-main-entitlements.plist"
            entitlements.write_bytes(plistlib.dumps(values))
        signature.extend(["--entitlements", str(entitlements)])
    # PyInstaller signs its embedded native libraries before the outer app.
    result = subprocess.run(signature + [str(app)], capture_output=True, text=True)
    if result.returncode:
        raise SystemExit("The app could not be signed: " + result.stderr.strip())
    print(f"Verified {len(verified)} native components for macOS {info['LSMinimumSystemVersion']} and {platform.machine()}.")
    print(f"Built {app}")
    return app


def backup_directory():
    backup = ROOT / ".local" / "install-backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns()))
    backup.mkdir(parents=True, mode=0o700)
    return backup


def atomic_copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name("." + destination.name + "." + str(time.time_ns()) + ".tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_plist(destination, config):
    temporary = destination.with_name("." + destination.name + "." + str(time.time_ns()) + ".tmp")
    try:
        temporary.write_bytes(plistlib.dumps(config))
        temporary.chmod(0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def install_app(app, backup=None):
    backup = backup or backup_directory()
    INSTALLED_APP.parent.mkdir(parents=True, exist_ok=True)
    if INSTALLED_APP.exists():
        shutil.copytree(INSTALLED_APP, backup / "Agent Board.app", symlinks=True)
    suffix = str(time.time_ns())
    staged = INSTALLED_APP.with_name(".Agent Board.installing-" + suffix + ".app")
    previous = INSTALLED_APP.with_name(".Agent Board.previous-" + suffix + ".app")
    try:
        shutil.copytree(app, staged, symlinks=True)
        if INSTALLED_APP.exists():
            os.replace(INSTALLED_APP, previous)
        try:
            os.replace(staged, INSTALLED_APP)
        except OSError:
            if previous.exists():
                os.replace(previous, INSTALLED_APP)
            raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    if previous.exists():
        shutil.rmtree(previous)
    print(f"Installed {INSTALLED_APP}")
    return INSTALLED_APP


def install_service(app, replace_app=True):
    missing = [relative for relative in RUNTIME_FILES if not (ROOT / relative).is_file()]
    if missing:
        raise SystemExit("The runtime files are missing: " + ", ".join(missing))
    runtime = SUPPORT / "runtime"
    data = SUPPORT / "data"
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    data.chmod(0o700)
    backup = backup_directory()
    if runtime.exists():
        shutil.copytree(runtime, backup / "runtime", symlinks=True)
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / (LABEL + ".plist")
    if plist.exists():
        shutil.copy2(plist, backup / plist.name)
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], capture_output=True)
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime.chmod(0o700)
    for relative in RUNTIME_FILES:
        atomic_copy(ROOT / relative, runtime / relative)
    installed_app = install_app(app, backup) if replace_app else INSTALLED_APP
    config = {
        "Label": LABEL,
        "ProgramArguments": [str(Path(sys.executable).resolve()), "-E", "-s", "-u",
                             str(runtime / "server.py"), "--data-dir", str(data)],
        "WorkingDirectory": str(runtime), "RunAtLoad": True, "KeepAlive": True,
        "ProcessType": "Background", "ThrottleInterval": 10,
        "StandardOutPath": str(data / "service.log"), "StandardErrorPath": str(data / "service.log"),
    }
    atomic_plist(plist, config)
    print(f"Saved installation backups to {backup}")
    result = subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"The service could not be started: {result.stderr.strip()}")
    for attempt in range(40):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8769/api/health", timeout=1) as response:
                if response.status == 200:
                    print("The local collector is running at http://127.0.0.1:8769")
                    return installed_app
        except OSError:
            time.sleep(0.5)
    raise SystemExit(f"The collector did not become ready. Check {data / 'service.log'}")


def uninstall_service():
    plist = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
    if plist.exists():
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], capture_output=True)
        plist.unlink()
    print(f"Removed the login service. The installed app and dashboard history in {SUPPORT} remain.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", action="store_true", help="Install the legacy developer collector service; unnecessary for downloaded app builds.")
    parser.add_argument("--open", action="store_true", help="Install and open the self-contained Mac app after building.")
    parser.add_argument("--uninstall", action="store_true", help="Remove only the login service.")
    parser.add_argument("--runtime-only", action="store_true", help="Update the installed collector and web assets without replacing the Mac window app.")
    parser.add_argument("--sandbox", action="store_true", help="Build a separate, uninstalled App Sandbox proof candidate with account features disabled.")
    args = parser.parse_args()
    if args.sandbox and (args.open or args.service or args.runtime_only or args.uninstall):
        parser.error("The sandbox proof build is separate and cannot install, open, or change the existing app/service.")
    if args.uninstall:
        uninstall_service()
        return
    if args.runtime_only:
        if not INSTALLED_APP.is_dir():
            parser.error("Install the Mac app with --service first.")
        app = install_service(INSTALLED_APP, replace_app=False)
    else:
        app = build_app(sandboxed=args.sandbox)
    if args.service and not args.runtime_only:
        app = install_service(app)
    elif args.open and not args.runtime_only:
        app = install_app(app)
    if args.open:
        subprocess.run(["/usr/bin/open", str(app)], check=True)


if __name__ == "__main__":
    main()
