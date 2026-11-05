"""Build-only helpers. None of these tools are needed by the downloaded app."""

from pathlib import Path
from dataclasses import dataclass
import os
import shutil
import subprocess
import sys
import plistlib
import platform
import re

ROOT = Path(__file__).resolve().parent
STORE_EXCLUDED_MODULES = ("usage_source", "claude_project_source", "privacy_network", "process_reader", "certifi")
SANDBOX_APP_ENTITLEMENTS = {
    "com.apple.security.app-sandbox": True,
    "com.apple.security.files.user-selected.read-only": True,
    "com.apple.security.files.bookmarks.app-scope": True,
    "com.apple.security.network.client": True,
    "com.apple.security.network.server": True,
}
SANDBOX_HELPER_ENTITLEMENTS = {"com.apple.security.app-sandbox": True, "com.apple.security.inherit": True}


def validate_profile_free_capabilities():
    """Permit only this reviewed unrestricted sandbox configuration, before signing.

    Apple's TN3125 permits these capabilities without provisioning for Store
    distribution. TestFlight or future restricted capabilities need their own
    provisioned configuration; this function does not make that claim.
    """
    for filename, expected in (("sandbox-app.plist", SANDBOX_APP_ENTITLEMENTS),
                               ("sandbox-helper.plist", SANDBOX_HELPER_ENTITLEMENTS)):
        path = ROOT / "entitlements" / filename
        if (path.is_symlink() or not path.is_file()
                or plistlib.dumps(plistlib.loads(path.read_bytes())) != plistlib.dumps(expected)):
            raise ValueError("Profile-free distribution permits only the reviewed unrestricted sandbox capabilities.")


@dataclass(frozen=True)
class AppConfiguration:
    """Explicit metadata for a separate build, without changing older outputs."""

    bundle_id: str
    version: str
    build_number: str
    display_name: str = "Agent Board"
    signing_channel: str = "direct"
    team_id: str | None = None
    provisioning_profile: Path | None = None
    store_support_strategy: str = "hidden"

    def __post_init__(self):
        if (not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+){2,}", self.bundle_id)
                or len(self.bundle_id) > 230):
            raise ValueError("Use a reverse-DNS bundle identifier with at least three components.")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", self.version):
            raise ValueError("The version must contain one to three numeric components.")
        if not re.fullmatch(r"[1-9][0-9]{0,8}", self.build_number):
            raise ValueError("Use a positive, increasing numeric build number.")
        if self.signing_channel not in {"direct", "store-preview", "store-distribution"}:
            raise ValueError("Unknown signing channel.")
        if self.store_support_strategy not in {"hidden", "us-external"}:
            raise ValueError("Unknown Store support strategy.")
        if not self.signing_channel.startswith("store-") and self.store_support_strategy != "hidden":
            raise ValueError("The US external support strategy requires a Store build.")
        if self.signing_channel == "store-distribution":
            if not self.team_id or not re.fullmatch(r"[A-Z0-9]{10}", self.team_id):
                raise ValueError("Store distribution requires an explicit signing team.")
        elif self.team_id or self.provisioning_profile:
            raise ValueError("Only distribution builds can contain signing-team/profile claims.")

    @property
    def helper_id(self):
        return self.bundle_id + ".collector"


def codesign_command(identity, channel="direct"):
    command = ["/usr/bin/codesign", "--force", "--sign", identity or "-"]
    if identity:
        if channel == "direct":
            command.extend(["--options", "runtime"])
        command.append("--timestamp")
    return command


def xcode_build_metadata():
    def read(*arguments):
        return subprocess.run(arguments, capture_output=True, text=True, check=True).stdout.strip()
    sdk_version = read("/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-version")
    sdk_build = read("/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-build-version")
    xcode = read("/usr/bin/xcodebuild", "-version")
    matched = re.fullmatch(r"Xcode (\d+)\.(\d+)(?:\.(\d+))?\nBuild version (\S+)", xcode)
    if not matched or not re.fullmatch(r"\d+(?:\.\d+){1,2}", sdk_version):
        raise ValueError("The selected production Xcode/SDK metadata could not be verified.")
    major, minor, patch, build = matched.groups()
    return {"CFBundleSupportedPlatforms": ["MacOSX"], "DTPlatformName": "macosx",
            "DTPlatformVersion": sdk_version, "DTPlatformBuild": sdk_build,
            "DTSDKName": "macosx" + sdk_version, "DTSDKBuild": sdk_build,
            "DTXcode": f"{int(major):02d}{int(minor)}{int(patch or 0)}", "DTXcodeBuild": build}


def copy_privacy_manifest(resources, *, collector=False):
    source = ROOT / "privacy" / ("CollectorPrivacyInfo.xcprivacy" if collector else "PrivacyInfo.xcprivacy")
    if source.is_symlink() or not source.is_file():
        raise SystemExit("The reviewed privacy manifest is missing.")
    shutil.copy2(source, resources / "PrivacyInfo.xcprivacy")


def sign_native_libraries(bundle, identity, channel):
    """Libraries have no sandbox or executable entitlements. Sign inside out."""
    magic = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce",
             b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
    executable = (bundle / "Contents/MacOS/agent-board-collector").resolve()
    seen = {executable}
    for path in sorted(bundle.rglob("*"), key=lambda value: len(value.parts), reverse=True):
        if path.is_symlink() or not path.is_file() or path.resolve() in seen:
            continue
        seen.add(path.resolve())
        with path.open("rb") as handle:
            native = handle.read(4) in magic
        if native:
            subprocess.run(codesign_command(identity, channel) + [str(path)], check=True)


def runtime_asset_mapping(*, store_profile=False, support_strategy="hidden"):
    if support_strategy not in {"hidden", "us-external"} or (not store_profile and support_strategy != "hidden"):
        raise ValueError("Use an implemented Store support strategy.")
    from install import RUNTIME_FILES
    mapping = {relative: relative for relative in RUNTIME_FILES if relative.startswith(("static/", "integrations/"))
               and relative != "static/about/store.html"}
    if store_profile:
        if support_strategy == "hidden":
            mapping.pop("static/about/ko-fi.svg", None)
        mapping.pop("static/about/ko-fi.png", None)
        mapping.pop("static/icons/claude.svg", None)
        mapping.pop("static/icons/codex.svg", None)
        mapping["static/about/index.html"] = "static/about/store.html"
        mapping["static/about/privacy.html"] = "store-listing/site/privacy.html"
    return mapping


def render_store_privacy_page(content):
    """Use the same reviewed policy as the public page, with local navigation."""
    if any(content.count(marker) != 1 for marker in (b"<main>", b"</main>", b"<footer>", b"</footer>")):
        raise ValueError("The bundled privacy policy format is invalid.")
    policy = content.split(b"<main>", 1)[1].split(b"</main>", 1)[0].strip()
    heading = b"<h1>Privacy</h1>"
    if not policy.startswith(heading):
        raise ValueError("The bundled privacy policy heading is invalid.")
    policy = policy[len(heading):].strip()
    footer = content.split(b"<footer>", 1)[1].split(b"</footer>", 1)[0].strip()
    return b"""<!doctype html>
<html lang="en" data-theme="dark" data-palette="terminal">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#050805">
  <meta http-equiv="Content-Security-Policy" content="connect-src 'none'; form-action 'none'">
  <title>Privacy policy | Agent Board</title>
  <link rel="stylesheet" href="/style.css">
  <link rel="stylesheet" href="/palettes.css">
  <link rel="stylesheet" href="/about/about.css">
  <script src="/themes.js" defer></script>
  <script src="/about/about.js" defer></script>
</head>
<body>
  <main class="about-page">
    <header class="about-header">
      <h1>Privacy policy</h1>
      <a class="back-link" href="/about/">Back to About</a>
    </header>
""" + policy + b"""
    <footer class="about-footer"><p>""" + footer + b"""</p></footer>
  </main>
</body>
</html>
"""


def runtime_asset_content(target_name, source_content):
    if target_name == "static/about/privacy.html":
        return render_store_privacy_page(source_content)
    return source_content


def stage_runtime_assets(output, *, store_profile=False, support_strategy="hidden"):
    """Freeze only explicitly distributable assets, including hidden plugin files."""
    mapping = runtime_asset_mapping(store_profile=store_profile, support_strategy=support_strategy)
    destination = output / "runtime-assets"
    if destination.exists():
        shutil.rmtree(destination)
    for target_name, relative in mapping.items():
        source = ROOT / relative
        if (not source.is_file() or not source.resolve().is_relative_to(ROOT)
                or any(path.is_symlink() for path in (source, *source.parents) if path.is_relative_to(ROOT))):
            raise ValueError("A bundled asset is missing or substituted: " + relative)
        target = destination / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target_name == "static/about/privacy.html":
            target.write_bytes(runtime_asset_content(target_name, source.read_bytes()))
            shutil.copymode(source, target)
        else:
            shutil.copy2(source, target)
    return destination


def validate_bundle(app, minimum_macos="13.0"):
    """Reject a bundle that quietly depends on a newer OS or build-host files."""
    magic = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce",
             b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}
    def version(value):
        parts = tuple(map(int, value.split(".")))
        return parts + (0,) * (3 - len(parts))
    ceiling = version(minimum_macos)
    seen = set()
    checked = []
    app = app.resolve()
    for path in app.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(app):
            raise SystemExit(f"The app contains an external file link: {path.relative_to(app)}")
        if not path.is_file():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(app):
            raise SystemExit(f"The app contains an external file link: {path.relative_to(app)}")
        if resolved in seen:
            continue
        seen.add(resolved)
        with resolved.open("rb") as handle:
            if handle.read(4) not in magic:
                continue
        relative = str(path.relative_to(app))
        architectures = subprocess.run(["/usr/bin/lipo", "-archs", str(path)], capture_output=True, text=True, check=True).stdout.split()
        if platform.machine() not in architectures:
            raise SystemExit(f"The app has an incompatible native component: {relative}")
        loads = subprocess.run(["/usr/bin/otool", "-l", str(path)], capture_output=True, text=True, check=True).stdout
        versions = re.findall(r"\bminos\s+([\d.]+)", loads)
        versions += re.findall(r"cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version ([\d.]+)", loads)
        if not versions:
            raise SystemExit(f"The minimum OS could not be verified for {relative}")
        if any(version(value) > ceiling for value in versions):
            raise SystemExit(f"{relative} requires macOS {max(versions, key=version)}, newer than the declared {minimum_macos}")
        for rpath in re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset", loads):
            if rpath.startswith("/") and not rpath.startswith(("/System/Library/", "/usr/lib/")):
                raise SystemExit(f"{relative} has a build-host library search path: {rpath}")
        dependencies = subprocess.run(["/usr/bin/otool", "-L", str(path)], capture_output=True, text=True, check=True).stdout.splitlines()[1:]
        for line in dependencies:
            library = line.strip().split(" (", 1)[0]
            if library.startswith("/") and not library.startswith(("/System/Library/", "/usr/lib/")):
                raise SystemExit(f"{relative} depends on a build-host library: {library}")
        checked.append({"path": relative, "minimum_macos": max(versions, key=version), "architectures": architectures,
                        "linked_sdk": sorted(set(re.findall(r"\bsdk\s+([\d.]+)", loads)), key=version)})
    if not checked:
        raise SystemExit("The app contains no verifiable native executable.")
    return checked


def build_collector(signing_identity=None, *, sandboxed=False, build_root=None, configuration=None):
    output = Path(build_root) if build_root is not None else ROOT / "build" / ("sandbox-build9" if sandboxed else "")
    if sandboxed and build_root is None and output.exists():
        raise SystemExit("The preserved sandbox checkpoint cannot be replaced. Choose a new explicit build root.")
    output.mkdir(parents=True, exist_ok=True)
    python = Path(os.environ.get("AGENT_BOARD_BUILD_PYTHON", ROOT / ".local/build-venv/bin/python"))
    if not python.is_file():
        raise SystemExit("Create the pinned portable build environment first: python3 bootstrap_build.py")
    channel = configuration.signing_channel if configuration else "direct"
    store_profile = channel.startswith("store-")
    assets = stage_runtime_assets(output, store_profile=store_profile,
                                 support_strategy=configuration.store_support_strategy if configuration else "hidden")
    command = [str(python), "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
               "--name", "agent-board-collector", "--distpath", str(output / "frozen"),
               "--workpath", str(output / "pyinstaller"), "--specpath", str(output),
               "--add-data", str(assets / "static") + os.pathsep + "static",
               "--add-data", str(assets / "integrations") + os.pathsep + "integrations",
               "--hidden-import", "ctypes", "--hidden-import", "sqlite3", "--hidden-import", "sandbox_sources",
               "--hidden-import", "local_quota",
               "--osx-bundle-identifier",
               configuration.helper_id if configuration else "local.agentboard.collector"]
    if store_profile:
        for module in STORE_EXCLUDED_MODULES:
            command.extend(["--exclude-module", module])
    else:
        command.extend(["--hidden-import", "usage_source", "--hidden-import", "claude_project_source",
                        "--hidden-import", "privacy_network", "--collect-data", "certifi"])
    if signing_identity and channel == "direct":
        command.extend(["--codesign-identity", signing_identity])
    if sandboxed:
        command.extend(["--osx-entitlements-file", str(ROOT / "entitlements/sandbox-helper.plist")])
        command.extend(["--add-data", str(ROOT / "entitlements/sandbox-collector-policy.json") + os.pathsep + "."])
    command.append(str(ROOT / "collector_entry.py"))
    environment = os.environ.copy()
    environment["PYINSTALLER_CONFIG_DIR"] = str(output / "pyinstaller-cache")
    subprocess.run(command, cwd=ROOT, env=environment, check=True)
    bundle = output / "frozen/agent-board-collector.app"
    info = bundle / "Contents/Info.plist"
    values = plistlib.loads(info.read_bytes())
    values["LSBackgroundOnly"] = True
    values["CFBundleSupportedPlatforms"] = ["MacOSX"]
    values["LSMinimumSystemVersion"] = "13.0"
    values["CFBundleDisplayName"] = "Agent Board Collector"
    values["NSAppDataUsageDescription"] = "Agent Board reads Codex and Claude’s local session files to show which chats are working or need your attention."
    if sandboxed:
        values["AgentBoardSandboxed"] = True
    if configuration:
        values["CFBundleShortVersionString"] = configuration.version
        values["CFBundleVersion"] = configuration.build_number
        if configuration.signing_channel.startswith("store-"):
            values["AgentBoardStoreDistribution"] = True
            values["AgentBoardStoreSupportStrategy"] = configuration.store_support_strategy
    info.write_bytes(plistlib.dumps(values))
    copy_privacy_manifest(bundle / "Contents/Resources", collector=True)
    if channel.startswith("store-"):
        sign_native_libraries(bundle, signing_identity, channel)
    signature = codesign_command(signing_identity, channel)
    if sandboxed:
        signature.extend(["--entitlements", str(ROOT / "entitlements/sandbox-helper.plist")])
    subprocess.run(signature + [str(bundle)], check=True)
    return bundle


def copy_collector(destination, signing_identity=None, *, sandboxed=False, build_root=None, configuration=None):
    collector = build_collector(signing_identity, sandboxed=sandboxed, build_root=build_root, configuration=configuration)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(collector, destination, symlinks=True)
    return destination
