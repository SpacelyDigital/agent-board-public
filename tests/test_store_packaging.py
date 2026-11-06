"""Reject wrong-channel signing and unsafe packaging inputs without accounts."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import os
import stat
import subprocess
import sys
import contextlib
from pathlib import Path
import plistlib
import shutil
import struct
import tempfile
import unittest
import uuid
from unittest.mock import patch

import build_store
from build_store import (ROOT, HELPER, bundle_snapshot, parse_arguments, validate_profile, validate_privacy,
                         validate_store_asset_exclusions, validate_recipient_readability, validate_no_quarantine,
                         validate_store_bundle, verify_package, APPLE_GENERIC_REQUIREMENT)
from build_support import (AppConfiguration, codesign_command, stage_runtime_assets,
                           validate_profile_free_capabilities, SANDBOX_APP_ENTITLEMENTS,
                           SANDBOX_HELPER_ENTITLEMENTS)
from install import build_app
from build_support import build_collector


class StoreSigningPolicyTests(unittest.TestCase):
    def test_legacy_sandbox_build_cannot_overwrite_a_preserved_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            checkpoint = root / "build/sandbox-build9"
            checkpoint.mkdir(parents=True)
            marker = checkpoint / "preserved-candidate"
            marker.write_bytes(b"unchanged")
            with patch("install.ROOT", root), patch("build_support.ROOT", root), \
                    patch("subprocess.run", side_effect=AssertionError("No build process may start")):
                with self.assertRaises(SystemExit):
                    build_app(sandboxed=True)
                with self.assertRaises(SystemExit):
                    build_collector(sandboxed=True)
            self.assertEqual(marker.read_bytes(), b"unchanged")

    def setUp(self):
        self.now = datetime(2026, 11, 4, tzinfo=timezone.utc)
        self.team = "ABCDEFGHIJ"
        self.identifier = "com.example.agentboard"
        self.profile = {"Platform": ["OSX"], "TeamIdentifier": [self.team],
                        "ExpirationDate": self.now + timedelta(days=30), "DeveloperCertificates": [b"public-der"],
                        "Entitlements": {"com.apple.application-identifier": self.team + "." + self.identifier,
                                         "com.apple.developer.team-identifier": self.team}}

    def test_public_distribution_profile_and_naive_plist_dates(self):
        value = validate_profile(self.profile, self.identifier, self.team, self.now)
        self.assertEqual(value["allowed_certificates"], [b"public-der"])
        naive = deepcopy(self.profile)
        naive["ExpirationDate"] = naive["ExpirationDate"].replace(tzinfo=None)
        self.assertEqual(validate_profile(naive, self.identifier, self.team, self.now), value)

    @unittest.skipUnless(sys.platform == "darwin", "Uses the actual macOS code requirement verifier")
    def test_apple_anchor_requirement_verifies_signed_copy_instead_of_opening_a_requirement_file(self):
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory) / "signed-system-control"
            shutil.copyfile("/bin/ls", control)
            shutil.copymode("/bin/ls", control)
            before = control.read_bytes()
            arguments = ["/usr/bin/codesign", "--verify", "--strict", "-R"]
            old = subprocess.run(arguments + ["anchor apple generic", str(control)],
                                 cwd=directory, capture_output=True, text=True)
            self.assertNotEqual(old.returncode, 0)
            self.assertIn("invalid requirement specification", old.stderr)
            current = subprocess.run(arguments + [APPLE_GENERIC_REQUIREMENT, str(control)],
                                     cwd=directory, capture_output=True, text=True)
            self.assertEqual(current.returncode, 0, current.stderr)
            self.assertEqual(control.read_bytes(), before)

    def test_development_enterprise_expired_other_app_other_team_profiles_fail(self):
        replacements = [
            {"Platform": ["iOS"]}, {"ProvisionedDevices": ["fake-device"]},
            {"ProvisionedDevices": []}, {"ProvisionsAllDevices": True},
            {"ExpirationDate": self.now}, {"ExpirationDate": "not-a-date"},
            {"TeamIdentifier": ["OTHERTEAM1"]}, {"DeveloperCertificates": []},
            {"DeveloperCertificates": ["not-der-bytes"]},
            {"Entitlements": {**self.profile["Entitlements"], "get-task-allow": True}},
            {"Entitlements": {**self.profile["Entitlements"], "com.apple.security.get-task-allow": True}},
            {"Entitlements": {**self.profile["Entitlements"], "com.apple.application-identifier": self.team + ".*"}},
            {"Entitlements": {**self.profile["Entitlements"], "com.apple.developer.team-identifier": "OTHERTEAM1"}},
        ]
        for replacement in replacements:
            with self.subTest(replacement=list(replacement)):
                value = {**self.profile, **replacement}
                with self.assertRaises(ValueError):
                    validate_profile(value, self.identifier, self.team, self.now)

    def arguments(self, *arguments):
        if "--output" not in arguments:
            arguments = (*arguments, "--output", str(ROOT / "build" / ("store-packaging-test-" + uuid.uuid4().hex)))
        return parse_arguments(["--bundle-id", self.identifier, *arguments])

    def test_preview_cannot_claim_or_discover_a_distribution_identity(self):
        with patch("subprocess.run", side_effect=AssertionError("No identity or account calls allowed")):
            args, configuration, _ = self.arguments("--preview", "--output", str(ROOT / "build/unused-store-test-output"))
            self.assertTrue(args.preview)
            self.assertEqual(configuration.signing_channel, "store-preview")
            for extra in ([], ["--app-signing-identity", "Developer ID Application: Example"],
                          ["--team-id", self.team], ["--provisioning-profile", "unread-file"]):
                flags = ["--distribution"] if not extra else ["--preview", *extra]
                with self.subTest(flags=flags), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                    self.arguments(*flags)

    def test_distribution_refuses_developer_id_without_reading_a_profile(self):
        with patch("subprocess.run", side_effect=AssertionError("No profile/account read")):
            for app, installer in (("Developer ID Application: Example", "3rd Party Mac Developer Installer: Example"),
                                   ("Apple Distribution: Example", "Developer ID Installer: Example")):
                with self.subTest(app=app), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                    self.arguments("--distribution", "--app-signing-identity", app,
                                   "--installer-signing-identity", installer, "--team-id", self.team,
                                   "--provisioning-profile", "unread-file")

    def test_distribution_requires_an_explicit_implemented_store_support_strategy(self):
        flags = ["--distribution", "--app-signing-identity", "Apple Distribution: Example",
                 "--installer-signing-identity", "3rd Party Mac Developer Installer: Example",
                 "--team-id", self.team, "--provisioning-profile", "unread-file"]
        with patch("subprocess.run", side_effect=AssertionError("No identity/profile reads during argument parsing")):
            with patch("sys.stderr", new_callable=io.StringIO) as stderr, self.assertRaises(SystemExit):
                self.arguments(*flags)
            self.assertIn("support-strategy hidden", stderr.getvalue())
            _, configuration, _ = self.arguments(*flags, "--support-strategy", "hidden")
            self.assertEqual(configuration.signing_channel, "store-distribution")
            self.assertEqual(configuration.store_support_strategy, "hidden")
            _, configuration, _ = self.arguments(*flags, "--support-strategy", "us-external")
            self.assertEqual(configuration.store_support_strategy, "us-external")
            with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                self.arguments(*flags, "--support-strategy", "worldwide")

    def test_profile_free_distribution_is_explicit_and_reads_no_profiles_or_accounts(self):
        flags = ["--distribution", "--app-signing-identity", "Apple Distribution: Example",
                 "--installer-signing-identity", "3rd Party Mac Developer Installer: Example",
                 "--team-id", self.team, "--support-strategy", "hidden"]
        with patch("subprocess.run", side_effect=AssertionError("No profile or identity reads")):
            with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                self.arguments(*flags)
            args, config, _ = self.arguments(*flags, "--profile-free")
            self.assertTrue(args.profile_free)
            self.assertIsNone(config.provisioning_profile)
            self.assertEqual(config.team_id, self.team)
            for wrong in (["--preview", "--profile-free"],
                          [*flags, "--profile-free", "--provisioning-profile", "unread-file"]):
                with self.subTest(wrong=wrong), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                    self.arguments(*wrong)

    def test_profile_free_capability_guard_rejects_restricted_or_malformed_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "entitlements").mkdir()
            main = root / "entitlements/sandbox-app.plist"
            helper = root / "entitlements/sandbox-helper.plist"
            main.write_bytes(plistlib.dumps(SANDBOX_APP_ENTITLEMENTS))
            helper.write_bytes(plistlib.dumps(SANDBOX_HELPER_ENTITLEMENTS))
            with patch("build_support.ROOT", root):
                validate_profile_free_capabilities()
                for values in ({**SANDBOX_APP_ENTITLEMENTS, "keychain-access-groups": ["FAKE.group"]},
                               {**SANDBOX_APP_ENTITLEMENTS, "com.apple.application-identifier": "FAKE.app"},
                               {**SANDBOX_APP_ENTITLEMENTS, "com.apple.security.app-sandbox": 1},
                               {**SANDBOX_APP_ENTITLEMENTS, "com.apple.security.network.client": False}):
                    with self.subTest(values=values):
                        main.write_bytes(plistlib.dumps(values))
                        with self.assertRaises(ValueError):
                            validate_profile_free_capabilities()
                main.write_bytes(plistlib.dumps(SANDBOX_APP_ENTITLEMENTS))
                helper.write_bytes(plistlib.dumps({**SANDBOX_HELPER_ENTITLEMENTS, "keychain-access-groups": ["FAKE.group"]}))
                with self.assertRaises(ValueError):
                    validate_profile_free_capabilities()

    def test_profile_free_build_rejects_unreviewed_capabilities_before_compiling_or_signing(self):
        config = AppConfiguration(self.identifier, "0.2.5", "14", signing_channel="store-distribution", team_id=self.team)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "must-not-be-created"
            with patch("build_support.validate_profile_free_capabilities", side_effect=ValueError("fixture capability rejection")), \
                    patch("subprocess.run", side_effect=AssertionError("No compiler, identity or signer may run")):
                with self.assertRaisesRegex(ValueError, "fixture capability rejection"):
                    build_app("Apple Distribution: Example", sandboxed=True, build_root=output, configuration=config)
            self.assertFalse(output.exists())

    def test_output_cannot_replace_checkpoints_escape_or_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            existing = root / "build/preserved-checkpoint"
            existing.mkdir(parents=True)
            marker = existing / "keep"
            marker.write_bytes(b"preserved fixture")
            outside = root / "outside"
            outside.mkdir()
            canary = outside / "keep"
            canary.write_bytes(b"outside fixture")
            link = existing / "outside-link"
            link.symlink_to(outside, target_is_directory=True)
            with patch("build_store.ROOT", root):
                for output in (existing, root / "build/../README.md", existing / "new-child", link / "new-child"):
                    with self.subTest(output=output.name), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                        self.arguments("--preview", "--output", str(output))
            self.assertEqual(marker.read_bytes(), b"preserved fixture")
            self.assertEqual(canary.read_bytes(), b"outside fixture")
            self.assertFalse((existing / "new-child").exists())
            self.assertFalse((outside / "new-child").exists())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "build").symlink_to(Path(tempfile.gettempdir()), target_is_directory=True)
            with patch("build_store.ROOT", root), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                self.arguments("--preview", "--output", str(root / "build/new-child"))

    def test_identifiers_versions_and_profile_claims_are_explicit(self):
        for values in (("com.example/escape", "0.2.5", "10"), ("com.example.app", "1.2.3.4", "10"),
                       ("com.example.app", "1.2.3", "0"), ("com.example.app", "1.2.3", "10/../old")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                AppConfiguration(*values)
        with self.assertRaises(ValueError):
            AppConfiguration(self.identifier, "0.2.5", "10", signing_channel="store-preview", team_id=self.team)
        self.assertNotIn("runtime", codesign_command("Apple Distribution: Example", "store-distribution"))
        self.assertIn("runtime", codesign_command("Developer ID Application: Example", "direct"))
        self.assertNotIn("--timestamp", codesign_command(None, "store-preview"))


class StoreQuarantineTests(unittest.TestCase):
    def fixture(self, root):
        app = root / "Fixture.app"
        resource = app / "Contents/Resources/fixture"
        resource.parent.mkdir(parents=True)
        resource.write_bytes(b"owned quarantine fixture")
        package = root / "fixture.pkg"
        package.write_bytes(b"owned package fixture")
        return app, resource, package

    def add_quarantine(self, path, value="0081;00000000;AgentBoard-fixture;"):
        subprocess.run(["/usr/bin/xattr", "-w", "com.apple.quarantine", value, str(path)],
                       check=True, capture_output=True)

    @unittest.skipUnless(sys.platform == "darwin", "Uses real macOS extended attributes")
    def test_real_clean_app_package_and_internal_symlink_are_inspected_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            app, resource, package = self.fixture(Path(directory))
            alias = app / "resource-alias"
            alias.symlink_to("Contents/Resources/fixture")
            before = bundle_snapshot(app)
            self.assertEqual(validate_no_quarantine(app), 5)
            self.assertEqual(validate_no_quarantine(package), 1)
            self.assertEqual(bundle_snapshot(app), before)
            self.assertEqual(package.read_bytes(), b"owned package fixture")
            self.assertEqual(resource.read_bytes(), b"owned quarantine fixture")

    @unittest.skipUnless(sys.platform == "darwin", "Uses real macOS extended attributes")
    def test_real_quarantine_on_app_directory_nested_file_or_package_is_rejected_and_preserved(self):
        for which, value in ((0, "0081;00000000;AgentBoard-fixture;"), (1, ""), (2, "fixture")):
            with self.subTest(which=which), tempfile.TemporaryDirectory() as directory:
                app, resource, package = self.fixture(Path(directory))
                target = (app, resource, package)[which]
                self.add_quarantine(target, value)
                before = bundle_snapshot(app)
                mode = target.lstat().st_mode
                with self.assertRaisesRegex(ValueError, "contains com.apple.quarantine"):
                    validate_no_quarantine(package if which == 2 else app)
                kept = subprocess.run(["/usr/bin/xattr", "-p", "com.apple.quarantine", str(target)],
                                      check=True, capture_output=True)
                self.assertEqual(kept.stdout.rstrip(b"\n"), value.encode())
                self.assertEqual(target.lstat().st_mode, mode)
                self.assertEqual(bundle_snapshot(app), before)
                self.assertEqual(package.read_bytes(), b"owned package fixture")

    def test_attribute_listing_is_nofollow_and_other_attribute_names_are_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            app, _, _ = self.fixture(Path(directory))
            alias = app / "dangling-alias"
            alias.symlink_to("not-present")
            calls = []

            def inspect(arguments, **kwargs):
                calls.append(arguments)
                self.assertEqual(arguments[:2], ["/usr/bin/xattr", "-s"])
                self.assertEqual(kwargs, {"capture_output": True, "timeout": 10})
                return subprocess.CompletedProcess(arguments, 0, b"com.apple.FinderInfo\n", b"")

            with patch("build_store.subprocess.run", side_effect=inspect):
                self.assertEqual(validate_no_quarantine(app), 5)
            self.assertEqual({Path(args[-1]) for args in calls}, {app, app / "Contents", app / "Contents/Resources",
                                                                 app / "Contents/Resources/fixture", alias})
            with self.assertRaisesRegex(ValueError, "real build artifact"):
                validate_no_quarantine(alias)

    def test_missing_tool_timeout_nonzero_and_stderr_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            app, _, _ = self.fixture(Path(directory))
            failures = [FileNotFoundError("fixture tool absent"), PermissionError("fixture denied"),
                        subprocess.TimeoutExpired("xattr", 10),
                        subprocess.CompletedProcess([], 1, b"", b"fixture denied"),
                        subprocess.CompletedProcess([], 0, b"", b"fixture incomplete inspection")]
            for failure in failures:
                with self.subTest(failure=type(failure).__name__):
                    arguments = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                    with patch("build_store.subprocess.run", **arguments), self.assertRaisesRegex(ValueError, "could not be inspected"):
                        validate_no_quarantine(app)

    def test_missing_special_or_unenumerable_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, _, _ = self.fixture(root)
            clean = subprocess.CompletedProcess([], 0, b"", b"")
            with self.assertRaisesRegex(ValueError, "could not be inspected"):
                validate_no_quarantine(root / "missing")
            fifo = app / "fifo"
            os.mkfifo(fifo)
            with patch("build_store.subprocess.run", return_value=clean), self.assertRaisesRegex(ValueError, "special file"):
                validate_no_quarantine(app)
            fifo.unlink()
            with patch("build_store.subprocess.run", return_value=clean), \
                    patch("build_store.os.scandir", side_effect=PermissionError("fixture denied")), \
                    self.assertRaisesRegex(ValueError, "could not be inspected"):
                validate_no_quarantine(app)

    def test_path_change_during_inspection_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            _, _, package = self.fixture(Path(directory))

            def changed(arguments, **kwargs):
                package.write_bytes(b"fixture changed during inspection")
                return subprocess.CompletedProcess(arguments, 0, b"", b"")

            with patch("build_store.subprocess.run", side_effect=changed), self.assertRaisesRegex(ValueError, "changed during"):
                validate_no_quarantine(package)

    @unittest.skipUnless(sys.platform == "darwin", "Uses real macOS extended attributes")
    def test_app_and_package_quarantine_stop_before_signer_or_installer_tools(self):
        config = AppConfiguration("com.example.quarantine", "0.2.5", "1", signing_channel="store-preview")
        with tempfile.TemporaryDirectory() as directory:
            app, resource, package = self.fixture(Path(directory))
            for path, operation in ((resource, lambda: validate_store_bundle(app, config)),
                                    (package, lambda: verify_package(package, app, config))):
                self.add_quarantine(path)
                with self.subTest(path=path.name), \
                        patch("build_store.checked", side_effect=AssertionError("No signature or package tool may run")), \
                        self.assertRaisesRegex(ValueError, "contains com.apple.quarantine"):
                    operation()

    @unittest.skipUnless(sys.platform == "darwin", "Uses real macOS extended attributes")
    def test_quarantine_inside_expanded_payload_fails_despite_matching_bytes_and_modes(self):
        config = AppConfiguration("com.example.quarantine", "0.2.5", "1", signing_channel="store-preview")
        with tempfile.TemporaryDirectory() as directory:
            app, _, package = self.fixture(Path(directory))

            def tools(arguments, **kwargs):
                if arguments[:2] == ["/usr/sbin/pkgutil", "--check-signature"]:
                    return subprocess.CompletedProcess(arguments, 1, "No signature", "")
                self.assertEqual(arguments[:2], ["/usr/sbin/pkgutil", "--expand-full"])
                component = Path(arguments[-1]) / "Fixture.pkg"
                restored = component / "Payload" / app.name
                shutil.copytree(app, restored)
                (component / "PackageInfo").write_text('<pkg-info install-location="/Applications"/>')
                self.add_quarantine(restored / "Contents/Resources/fixture")
                self.assertEqual(bundle_snapshot(restored), bundle_snapshot(app))
                return subprocess.CompletedProcess(arguments, 0, "", "")

            with patch("build_store.checked", side_effect=tools), \
                    self.assertRaisesRegex(ValueError, "contains com.apple.quarantine"):
                verify_package(package, app, config)

    @unittest.skipUnless(sys.platform == "darwin", "Uses real macOS extended attributes")
    def test_late_package_quarantine_prevents_build_success_report(self):
        config = AppConfiguration("com.example.quarantine", "0.2.5", "1", signing_channel="store-preview")
        args = type("Arguments", (), {"distribution": False, "profile_free": False,
                                      "app_signing_identity": None, "provisioning_profile": None})()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app, _, _ = self.fixture(root)
            output = root / "new-build-output"

            def productbuild(arguments, **kwargs):
                Path(arguments[-1]).write_bytes(b"owned package fixture")

            def late_attribute(package, *args):
                self.add_quarantine(package)
                return {}

            with patch("build_store.parse_arguments", return_value=(args, config, output)), \
                    patch("build_store.source_hashes", return_value={}), patch("build_store.build_app", return_value=app), \
                    patch("build_store.validate_store_bundle", return_value={}), \
                    patch("build_store.checked", side_effect=productbuild), \
                    patch("build_store.verify_package", side_effect=late_attribute), \
                    self.assertRaisesRegex(ValueError, "contains com.apple.quarantine"):
                build_store.main()
            self.assertFalse((output / "STORE-PACKAGE-CHECKS.json").exists())
            package = next(output.glob("*.pkg"))
            self.assertIn(b"com.apple.quarantine", subprocess.run(["/usr/bin/xattr", str(package)],
                                                                  check=True, capture_output=True).stdout.splitlines())


class StoreArtifactTests(unittest.TestCase):

    def test_license_staging_normalizes_private_source_permissions_before_signing(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            nested = source / "licenses/nested"
            nested.mkdir(parents=True)
            (source / "assets").mkdir()
            (source / "assets/AgentBoard.icns").write_bytes(b"icns-fixture")
            (source / "MacApp.swift").write_text("fixture native source")
            (source / "THIRD_PARTY.md").write_bytes(b"Fixture notices")
            license_file = nested / "LICENSE.txt"
            license_file.write_bytes(b"Fixture license")
            for path in (source / "licenses", nested):
                path.chmod(0o700)
            for path in (license_file, source / "THIRD_PARTY.md"):
                path.chmod(0o600)
            source_modes = {path: stat.S_IMODE(path.stat().st_mode) for path in
                            (source / "licenses", nested, license_file, source / "THIRD_PARTY.md")}
            signed = []

            def copied_helper(destination, *args, **kwargs):
                destination.mkdir(parents=True)

            def fake_process(arguments, **kwargs):
                if arguments[0] == "/usr/bin/swiftc":
                    executable = Path(arguments[-1])
                    executable.write_bytes(b"fixture executable")
                    executable.chmod(0o755)
                else:
                    app = Path(arguments[-1])
                    staged = app / "Contents/Resources/licenses"
                    for path in (staged, staged / "nested"):
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
                    for path in (staged / "nested/LICENSE.txt", app / "Contents/Resources/THIRD_PARTY.md"):
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
                    self.assertEqual((staged / "nested/LICENSE.txt").read_bytes(), b"Fixture license")
                    self.assertEqual((app / "Contents/MacOS/AgentBoard").stat().st_mode & 0o777, 0o755)
                    signed.append(True)
                return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

            with patch("install.ROOT", source), patch("build_support.copy_collector", side_effect=copied_helper), \
                    patch("build_support.copy_privacy_manifest"), patch("build_support.validate_bundle", return_value=[]), \
                    patch("install.copy_alert_sounds") as stage_sounds, \
                    patch("subprocess.run", side_effect=fake_process), contextlib.redirect_stdout(io.StringIO()):
                build_app(build_root=Path(directory) / "output")
            stage_sounds.assert_called_once_with(Path(directory) / "output/Agent Board.app/Contents/Resources")
            self.assertEqual(signed, [True])
            self.assertEqual({path: stat.S_IMODE(path.stat().st_mode) for path in source_modes}, source_modes)
            self.assertEqual(license_file.read_bytes(), b"Fixture license")

    def test_recipient_readability_guard_rejects_private_license_directories_without_changing_any_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Fixture.app"
            licenses = app / "Contents/Resources/licenses/nested"
            licenses.mkdir(parents=True)
            file = licenses / "LICENSE.txt"
            file.write_bytes(b"Fixture license")
            for path in (app, *[p for p in app.rglob("*") if p.is_dir()]):
                path.chmod(0o755)
            file.chmod(0o644)
            executable = app / "executable"
            executable.write_bytes(b"Fixture executable")
            executable.chmod(0o755)
            alias = app / "license-alias"
            alias.symlink_to("Contents/Resources/licenses/nested/LICENSE.txt")
            validate_recipient_readability(app)
            for mode in (0o700, 0o710, 0o744, 0o750, 0o754):
                with self.subTest(mode=oct(mode)):
                    licenses.chmod(mode)
                    before = bundle_snapshot(app)
                    with self.assertRaisesRegex(ValueError, "recipient-readable"):
                        validate_recipient_readability(app)
                    self.assertEqual(bundle_snapshot(app), before)
            licenses.chmod(0o755)
            validate_recipient_readability(app)
            self.assertEqual(executable.stat().st_mode & 0o777, 0o755)
            self.assertTrue(alias.is_symlink())

    def test_recipient_readability_guard_rejects_private_regular_files_and_special_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Fixture.app"
            app.mkdir(mode=0o755)
            file = app / "resource"
            file.write_bytes(b"Fixture")
            for mode in (0o600, 0o640, 0o700, 0o750):
                file.chmod(mode)
                with self.subTest(mode=oct(mode)), self.assertRaisesRegex(ValueError, "recipient-readable"):
                    validate_recipient_readability(app)
                self.assertEqual(file.stat().st_mode & 0o777, mode)
            file.chmod(0o644)
            validate_recipient_readability(app)
            fifo = app / "not-a-bundled-resource"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, "recipient-readable"):
                validate_recipient_readability(app)

    def test_actual_store_payload_rejects_excluded_assets_and_dangling_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Agent Board.app"
            validate_store_asset_exclusions(app)
            for contents in (app / "Contents", app / HELPER / "Contents"):
                for folder in ("Resources", "Frameworks"):
                    for relative in ("static/about/ko-fi.svg", "static/about/ko-fi.png", "static/about/store.html",
                                     "static/icons/claude.svg", "static/icons/codex.svg"):
                        with self.subTest(bundle=contents.relative_to(app), folder=folder, asset=relative):
                            asset = contents / folder / relative
                            asset.parent.mkdir(parents=True, exist_ok=True)
                            asset.write_bytes(b"excluded fixture")
                            with self.assertRaises(ValueError):
                                validate_store_asset_exclusions(app)
                            asset.unlink()
                            asset.symlink_to("missing-fixture")
                            with self.assertRaises(ValueError):
                                validate_store_asset_exclusions(app)
                            asset.unlink()
            validate_store_asset_exclusions(app)

    def test_store_staging_uses_explicit_template_and_omits_external_tipping_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source"
            about = source / "static/about"
            about.mkdir(parents=True)
            (about / "index.html").write_text("direct support content")
            (about / "store.html").write_text("reviewed Store content")
            (about / "ko-fi.svg").write_text("external QR fixture")
            (about / "ko-fi.png").write_bytes(b"external QR fixture")
            policy = source / "store-listing/site/privacy.html"
            policy.parent.mkdir(parents=True)
            policy.write_text("<main><h1>Privacy</h1><p>Local policy fixture.</p></main><footer>Fixture date.</footer>")
            files = tuple("static/about/" + name for name in ("index.html", "store.html", "ko-fi.svg", "ko-fi.png"))
            with patch("build_support.ROOT", source), patch("install.RUNTIME_FILES", files):
                staged = stage_runtime_assets(root / "output", store_profile=True)
                self.assertEqual({str(x.relative_to(staged)) for x in staged.rglob("*") if x.is_file()}, {"static/about/index.html", "static/about/privacy.html"})
                self.assertEqual((staged / "static/about/index.html").read_text(), "reviewed Store content")
                self.assertEqual((about / "index.html").read_text(), "direct support content")

    def test_only_explicit_assets_and_hidden_plugin_metadata_are_staged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source"
            (source / "static").mkdir(parents=True)
            (source / "integrations/.claude-plugin").mkdir(parents=True)
            (source / "static/app.js").write_text("fixture app")
            (source / "integrations/.claude-plugin/marketplace.json").write_text('{"fixture":true}')
            (source / "static/raw-recipient-log.txt").write_text("must not be shipped")
            files = ("static/app.js", "integrations/.claude-plugin/marketplace.json")
            with patch("build_support.ROOT", source), patch("install.RUNTIME_FILES", files):
                staged = stage_runtime_assets(root / "output")
                self.assertEqual({str(x.relative_to(staged)) for x in staged.rglob("*") if x.is_file()}, set(files))
                (source / "static/app.js").unlink()
                (source / "static/app.js").symlink_to(source / "static/raw-recipient-log.txt")
                with self.assertRaises(ValueError):
                    stage_runtime_assets(root / "rejected-output")

    def test_snapshot_detects_byte_mode_and_symlink_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "app-executable"
            binary.write_bytes(b"fixture")
            binary.chmod(0o755)
            link = root / "framework-alias"
            link.symlink_to(binary.name)
            original = bundle_snapshot(root)
            binary.chmod(0o644)
            self.assertNotEqual(original, bundle_snapshot(root))
            binary.chmod(0o755)
            binary.write_bytes(b"changed")
            self.assertNotEqual(original, bundle_snapshot(root))
            binary.write_bytes(b"fixture")
            link.unlink()
            link.symlink_to("different-target")
            self.assertNotEqual(original, bundle_snapshot(root))

    def test_privacy_manifests_match_executable_usage_and_reject_substitution(self):
        for collector in (False, True):
            with self.subTest(collector=collector), tempfile.TemporaryDirectory() as directory:
                bundle = Path(directory)
                resources = bundle / "Contents/Resources"
                resources.mkdir(parents=True)
                source = ROOT / "privacy" / ("CollectorPrivacyInfo.xcprivacy" if collector else "PrivacyInfo.xcprivacy")
                destination = resources / "PrivacyInfo.xcprivacy"
                destination.write_bytes(source.read_bytes())
                validate_privacy(bundle, collector=collector)
                values = plistlib.loads(source.read_bytes())
                self.assertIs(values["NSPrivacyTracking"], False)
                self.assertEqual(values["NSPrivacyCollectedDataTypes"], [])
                declarations = {x["NSPrivacyAccessedAPIType"]: x["NSPrivacyAccessedAPITypeReasons"]
                                for x in values["NSPrivacyAccessedAPITypes"]}
                self.assertEqual("NSPrivacyAccessedAPICategoryUserDefaults" in declarations, not collector)
                self.assertEqual(declarations["NSPrivacyAccessedAPICategoryFileTimestamp"], ["C617.1", "3B52.1"])
                values["NSPrivacyTracking"] = True
                destination.write_bytes(plistlib.dumps(values))
                with self.assertRaises(ValueError):
                    validate_privacy(bundle, collector=collector)

    def test_native_icon_contains_required_large_and_small_sizes(self):
        content = (ROOT / "assets/AgentBoard.icns").read_bytes()
        self.assertEqual(content[:4], b"icns")
        self.assertEqual(struct.unpack(">I", content[4:8])[0], len(content))
        offset, chunks = 8, []
        while offset < len(content):
            kind, size = content[offset:offset + 4], struct.unpack(">I", content[offset + 4:offset + 8])[0]
            self.assertGreaterEqual(size, 8)
            chunks.append(kind)
            offset += size
        self.assertEqual(offset, len(content))
        self.assertIn(b"ic10", chunks)  # 1024px artwork
        self.assertTrue({b"ic04", b"icp4"}.intersection(chunks))  # 16px artwork


if __name__ == "__main__":
    unittest.main()
