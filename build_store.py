#!/usr/bin/env python3
"""Build and inspect a separate Mac App Store package. Never installs or uploads."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from build_support import (AppConfiguration, ROOT, STORE_EXCLUDED_MODULES, runtime_asset_mapping, validate_bundle,
                           SANDBOX_APP_ENTITLEMENTS as APP_ENTITLEMENTS,
                           SANDBOX_HELPER_ENTITLEMENTS as HELPER_ENTITLEMENTS,
                           validate_profile_free_capabilities, runtime_asset_content)
from install import RUNTIME_FILES, build_app


HELPER = Path("Contents/Helpers/AgentBoardCollector.app")


def checked(arguments, *, accepted=(0,)):
    result = subprocess.run(list(map(str, arguments)), capture_output=True, text=True)
    if result.returncode not in accepted:
        # Tool stderr can contain the local build path or certificate subject.
        # Keep errors useful without persisting either in distributable reports.
        raise ValueError(Path(arguments[0]).name + " failed; inspect the local build separately.")
    return result


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes():
    inputs = set(RUNTIME_FILES) | {
        "MacApp.swift", "CollectorRuntime.swift", "collector_entry.py", "build_store.py", "build_support.py", "install.py",
        "entitlements/sandbox-app.plist", "entitlements/sandbox-helper.plist", "entitlements/sandbox-collector-policy.json",
        "privacy/PrivacyInfo.xcprivacy", "privacy/CollectorPrivacyInfo.xcprivacy", "assets/AgentBoard.icns",
        "static/about/store.html", "store-listing/site/privacy.html",
        "requirements-build.txt", "THIRD_PARTY.md",
    }
    inputs |= {str(path.relative_to(ROOT)) for path in (ROOT / "licenses").rglob("*") if path.is_file()}
    result = {}
    for relative in sorted(inputs):
        path = ROOT / relative
        if (not path.is_file() or not path.resolve().is_relative_to(ROOT)
                or any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(ROOT))):
            raise ValueError("A build input is missing or substituted: " + relative)
        result[relative] = sha256(path)
    return result


def validate_profile(values, bundle_id, team_id, now=None):
    """Validate only an explicitly supplied public distribution profile payload."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(values, dict) or values.get("Platform") != ["OSX"]:
        raise ValueError("Use a macOS distribution provisioning profile.")
    if values.get("ProvisionedDevices") is not None or values.get("ProvisionsAllDevices"):
        raise ValueError("Development and Developer ID profiles cannot sign a Store build.")
    if values.get("TeamIdentifier") != [team_id]:
        raise ValueError("The profile does not match the requested team.")
    expires = values.get("ExpirationDate")
    if not isinstance(expires, datetime):
        raise ValueError("The profile has no valid expiration date.")
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now:
        raise ValueError("The provisioning profile has expired.")
    entitlements = values.get("Entitlements")
    if not isinstance(entitlements, dict):
        raise ValueError("The profile entitlements are invalid.")
    if any(entitlements.get(key) for key in ("get-task-allow", "com.apple.security.get-task-allow")):
        raise ValueError("A debugging-enabled profile cannot sign a Store build.")
    if entitlements.get("com.apple.application-identifier") != team_id + "." + bundle_id:
        raise ValueError("Use a profile for this exact registered bundle identifier.")
    if entitlements.get("com.apple.developer.team-identifier") != team_id:
        raise ValueError("The profile's team entitlement does not match.")
    certificates = values.get("DeveloperCertificates")
    if not isinstance(certificates, list) or not certificates or not all(isinstance(x, bytes) for x in certificates):
        raise ValueError("The profile has no valid distribution certificates.")
    return {"expires_utc": expires.isoformat(), "allowed_certificates": certificates}


def read_profile(path, configuration):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Supply a regular, bounded provisioning profile file.")
    decoded = checked(["/usr/bin/security", "cms", "-D", "-i", path]).stdout
    try:
        values = plistlib.loads(decoded.encode())
    except (ValueError, plistlib.InvalidFileException) as error:
        raise ValueError("The provisioning profile could not be decoded.") from error
    return validate_profile(values, configuration.bundle_id, configuration.team_id)


def signature_details(path):
    result = checked(["/usr/bin/codesign", "-d", "--verbose=4", path])
    text = result.stderr
    return {key: value for key, value in re.findall(r"^(Identifier|TeamIdentifier|Authority|Signature)=(.*)$", text, re.M)}


def signed_entitlements(path):
    result = checked(["/usr/bin/codesign", "-d", "--entitlements", "-", "--xml", path])
    value = result.stdout.encode()
    if not value.strip():
        return {}
    try:
        parsed = plistlib.loads(value)
    except (ValueError, plistlib.InvalidFileException) as error:
        raise ValueError("A native component has unreadable entitlements.") from error
    if not isinstance(parsed, dict):
        raise ValueError("A native component has invalid entitlements.")
    return parsed


def bundle_snapshot(bundle):
    """Include file modes and link destinations, not just regular-file bytes."""
    snapshot = {}
    for path in sorted(bundle.rglob("*")):
        relative = str(path.relative_to(bundle))
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            snapshot[relative] = {"kind": "link", "target": str(path.readlink()), "mode": mode}
        elif path.is_file():
            snapshot[relative] = {"kind": "file", "sha256": sha256(path), "mode": mode}
        elif not path.is_dir():
            raise ValueError("The app contains a special file.")
    return snapshot


def validate_privacy(bundle, *, collector=False):
    path = bundle / "Contents/Resources/PrivacyInfo.xcprivacy"
    source = ROOT / "privacy" / ("CollectorPrivacyInfo.xcprivacy" if collector else "PrivacyInfo.xcprivacy")
    if path.is_symlink() or not path.is_file() or path.read_bytes() != source.read_bytes():
        raise ValueError("The app must contain the reviewed privacy manifest.")
    value = plistlib.loads(path.read_bytes())
    if (value.get("NSPrivacyTracking") is not False or value.get("NSPrivacyTrackingDomains") != []
            or value.get("NSPrivacyCollectedDataTypes") != []):
        raise ValueError("The privacy manifest must disclose no tracking or collected data.")
    return sha256(path)


def validate_store_asset_exclusions(app, support_strategy="hidden"):
    if support_strategy not in {"hidden", "us-external"}:
        raise ValueError("Unknown Store support strategy.")
    excluded = ("static/about/ko-fi.svg", "static/about/ko-fi.png", "static/about/store.html",
                "static/icons/claude.svg", "static/icons/codex.svg")
    for contents in (app / "Contents", app / HELPER / "Contents"):
        for directory in ("Resources", "Frameworks"):
            for relative in excluded:
                path = contents / directory / relative
                if (support_strategy == "us-external" and relative == "static/about/ko-fi.svg"
                        and contents == app / HELPER / "Contents"):
                    if directory == "Resources":
                        continue
                    primary = contents / "Resources" / relative
                    alias = contents / "Frameworks/static"
                    static = contents / "Resources/static"
                    if (alias.is_symlink() and alias.readlink() == Path("../Resources/static")
                            and static.is_dir() and not static.is_symlink()
                            and primary.parent.is_dir() and not primary.parent.is_symlink()
                            and primary.is_file() and not primary.is_symlink()
                            and path.resolve() == primary.resolve() and path.samefile(primary)):
                        continue
                if path.exists() or path.is_symlink():
                    raise ValueError("An excluded support or provider asset entered the Store bundle.")


def validate_recipient_readability(app):
    """Reject bundle content a different local recipient cannot read or traverse."""
    for path in (app, *app.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            continue  # The existing bundle validator checks link destinations.
        if stat.S_ISDIR(mode):
            valid = mode & 0o005 == 0o005
        elif stat.S_ISREG(mode):
            valid = bool(mode & 0o004)
        else:
            valid = False
        if not valid:
            raise ValueError("A bundled path is not recipient-readable: " + str(path.relative_to(app)))


def validate_store_bundle(app, configuration, profile=None):
    validate_recipient_readability(app)
    checked(["/usr/bin/codesign", "--verify", "--deep", "--strict", app])
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    expected = {"CFBundleIdentifier": configuration.bundle_id, "CFBundleVersion": configuration.build_number,
                "CFBundleShortVersionString": configuration.version, "CFBundlePackageType": "APPL",
                "AgentBoardSandboxed": True, "AgentBoardDistributionChannel": configuration.signing_channel,
                "AgentBoardStoreDistribution": True, "AgentBoardStoreSupportStrategy": configuration.store_support_strategy,
                "CFBundleSupportedPlatforms": ["MacOSX"], "DTPlatformName": "macosx",
                "LSApplicationCategoryType": "public.app-category.developer-tools"}
    if any(info.get(key) != value for key, value in expected.items()):
        raise ValueError("The app metadata does not match the requested Store configuration.")
    if info.get("CFBundleIconFile") != "AgentBoard.icns":
        raise ValueError("The native icon name is invalid.")
    icon = app / "Contents/Resources/AgentBoard.icns"
    if icon.is_symlink() or not icon.is_file() or icon.read_bytes()[:4] != b"icns":
        raise ValueError("The app must include its native icon.")
    receipt = app / "Contents/_MASReceipt"
    if receipt.exists() or receipt.is_symlink():
        raise ValueError("Build from clean source; never redistribute a recipient's Store receipt.")
    main_entitlements = dict(APP_ENTITLEMENTS)
    distribution = configuration.signing_channel == "store-distribution"
    if distribution and configuration.provisioning_profile:
        main_entitlements.update({"com.apple.application-identifier": configuration.team_id + "." + configuration.bundle_id,
                                  "com.apple.developer.team-identifier": configuration.team_id})
    if (plistlib.dumps(signed_entitlements(app)) != plistlib.dumps(main_entitlements)
            or plistlib.dumps(signed_entitlements(app / HELPER)) != plistlib.dumps(HELPER_ENTITLEMENTS)):
        raise ValueError("The main/helper sandbox entitlements are not the reviewed exact sets.")
    helper_info = plistlib.loads((app / HELPER / "Contents/Info.plist").read_bytes())
    if (helper_info.get("CFBundleIdentifier") != configuration.helper_id
            or helper_info.get("CFBundleVersion") != configuration.build_number
            or helper_info.get("CFBundleShortVersionString") != configuration.version
            or helper_info.get("AgentBoardSandboxed") is not True
            or helper_info.get("AgentBoardStoreDistribution") is not True
            or helper_info.get("AgentBoardStoreSupportStrategy") != configuration.store_support_strategy
            or helper_info.get("LSBackgroundOnly") is not True):
        raise ValueError("The helper bundle identity or sandbox mode is wrong.")
    policy = app / HELPER / "Contents/Resources/sandbox-collector-policy.json"
    if policy.is_symlink() or not policy.is_file() or json.loads(policy.read_text()) != {"version": 1, "sandbox_required": True}:
        raise ValueError("The immutable collector sandbox policy is missing.")
    privacy_hashes = [validate_privacy(app), validate_privacy(app / HELPER, collector=True)]
    assets = runtime_asset_mapping(store_profile=True, support_strategy=configuration.store_support_strategy)
    for target, source in assets.items():
        asset = app / HELPER / "Contents/Resources" / target
        if (not asset.is_file() or not asset.resolve().is_relative_to(app.resolve())
                or asset.read_bytes() != runtime_asset_content(target, (ROOT / source).read_bytes())):
            raise ValueError("A reviewed runtime asset is missing or changed: " + target)
    validate_store_asset_exclusions(app, configuration.store_support_strategy)
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(app / HELPER / "Contents/MacOS/agent-board-collector"))
    modules = archive.open_embedded_archive("PYZ.pyz").toc
    if any(name == excluded or name.startswith(excluded + ".") for name in modules for excluded in STORE_EXCLUDED_MODULES):
        raise ValueError("A direct-only account/network helper entered the Store collector archive.")
    native = validate_bundle(app, info["LSMinimumSystemVersion"])
    main_sdk = next((value["linked_sdk"] for value in native if value["path"] == "Contents/MacOS/AgentBoard"), [])
    if len(main_sdk) != 1 or info.get("DTSDKName") != "macosx" + main_sdk[0]:
        raise ValueError("The declared SDK must match the main executable's actual linked SDK.")
    executable_paths = {"Contents/MacOS/AgentBoard", str(HELPER / "Contents/MacOS/agent-board-collector")}
    for component in native:
        path = app / component["path"]
        details = signature_details(path)
        expected_identifier = (configuration.bundle_id if component["path"] == "Contents/MacOS/AgentBoard" else
                               configuration.helper_id if component["path"] == str(HELPER / "Contents/MacOS/agent-board-collector") else None)
        if expected_identifier and details.get("Identifier") != expected_identifier:
            raise ValueError("A signed executable identifier differs from its bundle metadata.")
        if distribution:
            # A certificate common name alone is not evidence of Apple issuance.
            checked(["/usr/bin/codesign", "--verify", "--strict", "-R", "anchor apple generic", path])
            # Multiple Authority lines are returned leaf first; inspect leaf separately.
            text = checked(["/usr/bin/codesign", "-d", "--verbose=4", path]).stderr
            authorities = re.findall(r"^Authority=(.*)$", text, re.M)
            if (not authorities or not authorities[0].startswith(("Apple Distribution:", "3rd Party Mac Developer Application:"))
                    or details.get("TeamIdentifier") != configuration.team_id):
                raise ValueError("Every native component must use the requested Apple Distribution team.")
        elif details.get("Signature") != "adhoc":
            raise ValueError("A local Store preview must remain explicitly ad-hoc signed.")
        if component["path"] not in executable_paths and signed_entitlements(path):
            raise ValueError("Embedded libraries must not carry executable entitlements.")
    if distribution and configuration.provisioning_profile:
        with tempfile.TemporaryDirectory(prefix="agent-board-public-cert-") as directory:
            prefix = Path(directory) / "public-cert-"
            checked(["/usr/bin/codesign", "-d", "--extract-certificates", prefix, app])
            if not profile or (Path(str(prefix) + "0").read_bytes() not in profile["allowed_certificates"]):
                raise ValueError("The signing certificate is not authorized by the supplied profile.")
        if (app / "Contents/embedded.provisionprofile").read_bytes() != configuration.provisioning_profile.read_bytes():
            raise ValueError("The embedded profile differs from the validated profile.")
    elif (app / "Contents/embedded.provisionprofile").exists() or (app / "Contents/embedded.provisionprofile").is_symlink():
        raise ValueError("This build must not contain an unexpected provisioning profile.")
    return {"native_components": native, "privacy_manifest_sha256": privacy_hashes,
            "excluded_python_modules": list(STORE_EXCLUDED_MODULES), "asset_source_mapping": assets,
            "generated_assets": {"static/about/privacy.html": {
                "source": assets["static/about/privacy.html"],
                "source_sha256": sha256(ROOT / assets["static/about/privacy.html"]),
                "sha256": sha256(app / HELPER / "Contents/Resources/static/about/privacy.html")}},
            "main_sha256": sha256(app / "Contents/MacOS/AgentBoard"),
            "helper_sha256": sha256(app / HELPER / "Contents/MacOS/agent-board-collector")}


def verify_package(package, app, configuration):
    distribution = configuration.signing_channel == "store-distribution"
    signature = checked(["/usr/sbin/pkgutil", "--check-signature", package], accepted=(0, 1))
    if distribution:
        text = signature.stdout + signature.stderr
        subjects = re.findall(r"^\s*1\. (.+)$", text, re.M)
        if (signature.returncode != 0 or not subjects or
                not subjects[0].startswith("3rd Party Mac Developer Installer:") or
                "(" + configuration.team_id + ")" not in subjects[0]):
            raise ValueError("The package is not signed by this team's Mac Installer Distribution identity.")
    elif signature.returncode != 1 or "no signature" not in (signature.stdout + signature.stderr).lower():
        raise ValueError("The local package preview must be unsigned.")
    with tempfile.TemporaryDirectory(prefix="agent-board-pkg-proof-") as directory:
        expanded = Path(directory) / "expanded"
        checked(["/usr/sbin/pkgutil", "--expand-full", package, expanded])
        if any(path.name.lower() == "scripts" for path in expanded.rglob("*")):
            raise ValueError("The Store package must not run installer scripts.")
        package_infos = list(expanded.rglob("PackageInfo"))
        if len(package_infos) != 1:
            raise ValueError("The Store package must contain exactly one component.")
        component = ET.fromstring(package_infos[0].read_text())
        if component.get("install-location") != "/Applications":
            raise ValueError("The Store package must install only into Applications.")
        if component.find("scripts") is not None:
            raise ValueError("Installer script metadata is not permitted.")
        payloads = list(expanded.rglob("Payload"))
        if len(payloads) != 1 or not payloads[0].is_dir():
            raise ValueError("The installer payload cannot be inspected.")
        entries = list(payloads[0].iterdir())
        if len(entries) != 1 or entries[0].name != app.name or entries[0].is_symlink():
            raise ValueError("The package payload must contain only this app.")
        restored = entries[0]
        if bundle_snapshot(restored) != bundle_snapshot(app):
            raise ValueError("Packaging changed app bytes, executable modes, or symbolic links.")
        checked(["/usr/bin/codesign", "--verify", "--deep", "--strict", restored])
    return {"signature": "mac-installer-distribution" if distribution else "unsigned-local-preview",
            "payload_exact": True, "installer_scripts": False, "install_location": "/Applications"}


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="Ad-hoc app and unsigned package for local engineering only.")
    mode.add_argument("--distribution", action="store_true", help="Create a locally signed Store package. Does not upload it.")
    parser.add_argument("--bundle-id", required=True, help="Explicit product identifier; registration is a separate owner step.")
    parser.add_argument("--version", default="0.2.5")
    parser.add_argument("--build-number", default="10")
    parser.add_argument("--output", type=Path, default=ROOT / "build/store-build10")
    parser.add_argument("--app-signing-identity")
    parser.add_argument("--installer-signing-identity")
    parser.add_argument("--team-id")
    provisioning = parser.add_mutually_exclusive_group()
    provisioning.add_argument("--provisioning-profile", type=Path)
    provisioning.add_argument("--profile-free", action="store_true",
                              help="Use only the reviewed unrestricted sandbox capabilities, without a profile. Not a TestFlight build.")
    parser.add_argument("--support-strategy", choices=["hidden", "us-external"], help="Hidden support by default; us-external permits optional support only after native US storefront checks.")
    args = parser.parse_args(argv)
    signing = [args.app_signing_identity, args.installer_signing_identity, args.team_id, args.provisioning_profile]
    if args.preview and (any(signing) or args.profile_free):
        parser.error("A preview cannot claim a distribution identity, team, or profile.")
    if args.distribution:
        if not all(signing[:3]) or not (args.provisioning_profile or args.profile_free):
            parser.error("Distribution needs explicit app/installer identities, team, and either --provisioning-profile or --profile-free; no credentials are discovered.")
        if not args.app_signing_identity.startswith(("Apple Distribution:", "3rd Party Mac Developer Application:")):
            parser.error("Use an Apple Distribution application identity, not Developer ID.")
        if not args.installer_signing_identity.startswith("3rd Party Mac Developer Installer:"):
            parser.error("Use a Mac Installer Distribution identity, not Developer ID Installer.")
        if args.support_strategy not in {"hidden", "us-external"}:
            parser.error("Distribution requires an explicit --support-strategy hidden or us-external.")
    try:
        configuration = AppConfiguration(args.bundle_id, args.version, args.build_number,
                                         signing_channel="store-distribution" if args.distribution else "store-preview",
                                         team_id=args.team_id, provisioning_profile=args.provisioning_profile,
                                         store_support_strategy=args.support_strategy or "hidden")
    except ValueError as error:
        parser.error(str(error))
    output = args.output.absolute()
    # This command must never replace a prior checkpoint or write into the owner install.
    if ((ROOT / "build").is_symlink() or output.exists() or output.is_symlink() or output.parent != ROOT / "build"
            or output.resolve().parent != (ROOT / "build").resolve()
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", output.name)):
        parser.error("Choose a new, immediate child directory within this project's build directory.")
    return args, configuration, output


def main():
    args, configuration, output = parse_arguments()
    if args.distribution and args.profile_free:
        validate_profile_free_capabilities()
    profile = read_profile(args.provisioning_profile, configuration) if args.distribution and args.provisioning_profile else None
    source = source_hashes()
    output.mkdir(parents=True, exist_ok=False)
    app = build_app(args.app_signing_identity, sandboxed=True, build_root=output, configuration=configuration)
    proof = validate_store_bundle(app, configuration, profile)
    if source_hashes() != source:
        raise ValueError("Source files changed during the build; preserve this partial checkpoint and rebuild into a new directory.")
    package = output / ("Agent-Board-" + configuration.version + "-build" + configuration.build_number
                        + ("-store.pkg" if args.distribution else "-unsigned-store-preview.pkg"))
    command = ["/usr/bin/productbuild", "--component", app, "/Applications"]
    if args.distribution:
        command += ["--sign", args.installer_signing_identity]
    checked(command + [package])
    package_proof = verify_package(package, app, configuration)
    report = {"version": configuration.version, "build": configuration.build_number,
              "bundle_id": configuration.bundle_id, "channel": configuration.signing_channel,
              "store_support_strategy": configuration.store_support_strategy,
              "provisioning_mode": "profile-free-unrestricted" if args.profile_free else "supplied-profile" if args.provisioning_profile else "preview",
              "testflight_validated": False,
              "submission_ready": False, "app_store_registration_verified": False,
              "uploaded": False, "installed": False, "provider_calls": False,
              "receipt": "No recipient receipt is bundled; Store purchase validation is not exercised by this local build.",
              "remaining_gates": ["Owner-confirmed registered identifier and App Store Connect product metadata",
                                  "Distribution signing and installer validation; a profile is needed for TestFlight or restricted capabilities" if args.preview else
                                  "Apple processing and TestFlight on a clean recipient Mac",
                                  "Actual provider compatibility, accessibility and offline/recovery evidence",
                                  "Owner-approved privacy labels, commercial terms, screenshots and App Review"],
              "bundle_checks": proof, "package_checks": package_proof,
              "source_sha256": source,
              "artifacts": [{"name": package.name, "bytes": package.stat().st_size, "sha256": sha256(package)}]}
    (output / "STORE-PACKAGE-CHECKS.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error))
