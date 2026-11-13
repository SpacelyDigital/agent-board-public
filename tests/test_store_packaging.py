"""Reject wrong-channel signing and unsafe packaging inputs without accounts."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import hashlib
import json
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
                 "--team-id", self.team, "--provisioning-profile", "unread-file", "--uses-non-exempt-encryption", "no"]
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
                 "--team-id", self.team, "--support-strategy", "hidden", "--uses-non-exempt-encryption", "no"]
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
        config = AppConfiguration(self.identifier, "0.2.5", "14", signing_channel="store-distribution", team_id=self.team,
                                  uses_non_exempt_encryption=False)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "must-not-be-created"
            with patch("build_support.validate_profile_free_capabilities", side_effect=ValueError("fixture capability rejection")), \
                    patch("subprocess.run", side_effect=AssertionError("No compiler, identity or signer may run")):
                with self.assertRaisesRegex(ValueError, "fixture capability rejection"):
                    build_app("Apple Distribution: Example", sandboxed=True, build_root=output, configuration=config)
            self.assertFalse(output.exists())

    def test_encryption_answer_is_explicit_and_missing_answer_stops_distribution(self):
        config = AppConfiguration(self.identifier, "0.2.5", "62", signing_channel="store-distribution", team_id=self.team)
        with patch("subprocess.run", side_effect=AssertionError("No build may start")):
            with self.assertRaisesRegex(ValueError, "owner-approved encryption answer"):
                build_app(configuration=config, sandboxed=True)
        from dataclasses import replace
        self.assertEqual(replace(config, uses_non_exempt_encryption=False).encryption_metadata(),
                         {"ITSAppUsesNonExemptEncryption": False})
        self.assertEqual(replace(config, uses_non_exempt_encryption=True, encryption_compliance_code="TEST-CODE").encryption_metadata(),
                         {"ITSAppUsesNonExemptEncryption": True, "ITSEncryptionExportComplianceCode": "TEST-CODE"})
        with self.assertRaises(ValueError):
            replace(config, uses_non_exempt_encryption=False, encryption_compliance_code="TEST-CODE")
        self.assertEqual(AppConfiguration(self.identifier, "0.2.5", "62", signing_channel="store-preview").encryption_metadata(), {})

    def upload_flags(self):
        return ["--distribution", "--upload-candidate", "--app-signing-identity", "Apple Distribution: Example",
                "--installer-signing-identity", "3rd Party Mac Developer Installer: Example",
                "--team-id", self.team, "--support-strategy", "hidden"]

    def test_upload_candidate_requires_explicit_opt_in_and_preserves_signing_inputs(self):
        with patch("subprocess.run", side_effect=AssertionError("No identity, profile or account reads")):
            for provisioning in (["--profile-free"], ["--provisioning-profile", "unread-fixture"]):
                with self.subTest(provisioning=provisioning):
                    flags = self.upload_flags() + provisioning
                    args, config, _ = self.arguments(*flags)
                    self.assertTrue(args.upload_candidate)
                    self.assertEqual(config.signing_channel, "store-distribution")
                    self.assertEqual(config.team_id, self.team)
                    self.assertTrue(config.defer_encryption_classification)
                    self.assertIsNone(config.uses_non_exempt_encryption)
                    self.assertEqual(config.encryption_metadata(), {})
                    flags.remove("--upload-candidate")
                    with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                        self.arguments(*flags)

    def test_testflight_intent_requires_distribution_and_supplied_profile(self):
        from dataclasses import replace
        valid = self.upload_flags() + ["--provisioning-profile", "unread-fixture", "--require-testflight-profile"]
        with patch("subprocess.run", side_effect=AssertionError("No profile or signing access during argument parsing")):
            args, config, _ = self.arguments(*valid)
            self.assertTrue(args.require_testflight_profile)
            self.assertTrue(config.require_testflight_profile)
            self.assertTrue(config.defer_encryption_classification)
            self.assertEqual(config.encryption_metadata(), {})
            for invalid in (["--preview", "--require-testflight-profile"],
                            self.upload_flags() + ["--require-testflight-profile"],
                            self.upload_flags() + ["--profile-free", "--require-testflight-profile"]):
                with self.subTest(arguments=invalid), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                    self.arguments(*invalid)
        for changes in ({"provisioning_profile": None}, {"require_testflight_profile": 1},
                        {"require_testflight_profile": "yes"},
                        {"signing_channel": "store-preview", "team_id": None, "defer_encryption_classification": False}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(config, **changes)

    def test_upload_candidate_rejects_conflicts_and_missing_or_wrong_signing_inputs(self):
        flags = self.upload_flags() + ["--profile-free"]
        wrong = [["--preview", "--upload-candidate"], self.upload_flags(),
                 flags + ["--provisioning-profile", "unread-fixture"],
                 flags + ["--uses-non-exempt-encryption", "no"],
                 flags + ["--uses-non-exempt-encryption", "yes"],
                 flags + ["--encryption-compliance-code", "TEST-CODE"]]
        for key, value in (("--app-signing-identity", "Developer ID Application: Example"),
                           ("--installer-signing-identity", "Developer ID Installer: Example"),
                           ("--team-id", "INVALID"), ("--support-strategy", "worldwide")):
            changed = list(flags)
            changed[changed.index(key) + 1] = value
            wrong.append(changed)
            missing = list(flags)
            index = missing.index(key)
            del missing[index:index + 2]
            wrong.append(missing)
        with patch("subprocess.run", side_effect=AssertionError("No compile, signing or account calls")):
            for arguments in wrong:
                with self.subTest(arguments=arguments), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                    self.arguments(*arguments)

    def test_deferred_configuration_cannot_claim_an_answer_or_bypass_sandbox_capabilities(self):
        from dataclasses import replace
        config = AppConfiguration(self.identifier, "0.2.5", "70", signing_channel="store-distribution",
                                  team_id=self.team, defer_encryption_classification=True)
        for changes in ({"uses_non_exempt_encryption": False}, {"uses_non_exempt_encryption": True},
                        {"encryption_compliance_code": "TEST-CODE"}, {"encryption_compliance_code": ""},
                        {"defer_encryption_classification": 1}, {"defer_encryption_classification": "yes"},
                        {"signing_channel": "store-preview", "team_id": None},
                        {"signing_channel": "direct", "team_id": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(config, **changes)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "must-not-exist"
            with patch("subprocess.run", side_effect=AssertionError("No compiler or signer may run")):
                with self.assertRaisesRegex(ValueError, "App Sandbox"):
                    build_app(configuration=config, build_root=output)
                with patch("build_support.validate_profile_free_capabilities", side_effect=ValueError("unreviewed capability")):
                    with self.assertRaisesRegex(ValueError, "unreviewed capability"):
                        build_app(configuration=config, sandboxed=True, build_root=output)
            self.assertFalse(output.exists())

    def test_encryption_metadata_requires_exact_keys_and_boolean_types(self):
        from dataclasses import replace
        config = AppConfiguration(self.identifier, "0.2.5", "70", signing_channel="store-distribution",
                                  team_id=self.team, defer_encryption_classification=True)
        build_store.validate_encryption_metadata({}, config)
        for extra in ({"ITSAppUsesNonExemptEncryption": False}, {"ITSAppUsesNonExemptEncryption": None},
                      {"ITSEncryptionExportComplianceCode": ""}, {"ITSEncryptionExportComplianceCode": "TEST-CODE"}):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "encryption declaration"):
                build_store.validate_encryption_metadata(extra, config)
        declared = replace(config, defer_encryption_classification=False, uses_non_exempt_encryption=False)
        build_store.validate_encryption_metadata({"ITSAppUsesNonExemptEncryption": False}, declared)
        for metadata in ({}, {"ITSAppUsesNonExemptEncryption": 0}, {"ITSAppUsesNonExemptEncryption": True},
                         {"ITSAppUsesNonExemptEncryption": False, "ITSEncryptionExportComplianceCode": "TEST-CODE"}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                build_store.validate_encryption_metadata(metadata, declared)

    def test_deferred_bundle_rejects_encryption_keys_in_main_or_helper(self):
        config = AppConfiguration(self.identifier, "0.2.5", "70", signing_channel="store-distribution",
                                  team_id=self.team, defer_encryption_classification=True)
        main_info = {"CFBundleIdentifier": config.bundle_id, "CFBundleVersion": config.build_number,
                     "CFBundleShortVersionString": config.version, "CFBundlePackageType": "APPL",
                     "AgentBoardSandboxed": True, "AgentBoardDistributionChannel": "store-distribution",
                     "AgentBoardStoreDistribution": True, "AgentBoardStoreSupportStrategy": "hidden",
                     "CFBundleSupportedPlatforms": ["MacOSX"], "DTPlatformName": "macosx",
                     "LSApplicationCategoryType": "public.app-category.developer-tools", "CFBundleIconFile": "AgentBoard.icns"}
        helper_info = {"CFBundleIdentifier": config.helper_id, "CFBundleVersion": config.build_number,
                       "CFBundleShortVersionString": config.version, "AgentBoardSandboxed": True,
                       "AgentBoardStoreDistribution": True, "AgentBoardStoreSupportStrategy": "hidden", "LSBackgroundOnly": True}
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Fixture.app"
            for contents in (app / "Contents", app / HELPER / "Contents"):
                (contents / "Resources").mkdir(parents=True)
            main = app / "Contents/Info.plist"
            helper = app / HELPER / "Contents/Info.plist"
            main.write_bytes(plistlib.dumps(main_info))
            helper.write_bytes(plistlib.dumps(helper_info))
            (app / "Contents/Resources/AgentBoard.icns").write_bytes(b"icns-fixture")
            policy = app / HELPER / "Contents/Resources/sandbox-collector-policy.json"
            policy.write_text(json.dumps({"version": 1, "sandbox_required": True}))
            with patch("build_store.checked"), patch("build_store.validate_store_fixture_exclusion"), \
                    patch("build_store.signed_entitlements", side_effect=lambda path: SANDBOX_APP_ENTITLEMENTS if path == app else SANDBOX_HELPER_ENTITLEMENTS), \
                    patch("build_store.validate_privacy", side_effect=RuntimeError("Reached unchanged privacy checks")):
                with self.assertRaisesRegex(RuntimeError, "unchanged privacy checks"):
                    validate_store_bundle(app, config)
                for path, original in ((main, main_info), (helper, helper_info)):
                    for key, value in (("ITSAppUsesNonExemptEncryption", False), ("ITSEncryptionExportComplianceCode", "TEST-CODE")):
                        path.write_bytes(plistlib.dumps({**original, key: value}))
                        with self.subTest(bundle=path.parent, key=key), self.assertRaisesRegex(ValueError, "encryption declaration"):
                            validate_store_bundle(app, config)
                        path.write_bytes(plistlib.dumps(original))
                # An ordinary declared build also rejects independent helper
                # answers; the main app remains the sole declaration source.
                from dataclasses import replace
                for answer in (False, True):
                    declared = replace(config, defer_encryption_classification=False,
                                       uses_non_exempt_encryption=answer)
                    main.write_bytes(plistlib.dumps({**main_info, **declared.encryption_metadata()}))
                    with self.assertRaisesRegex(RuntimeError, "unchanged privacy checks"):
                        validate_store_bundle(app, declared)
                    for key, value in (("ITSAppUsesNonExemptEncryption", answer),
                                       ("ITSAppUsesNonExemptEncryption", not answer),
                                       ("ITSEncryptionExportComplianceCode", "TEST-CODE")):
                        helper.write_bytes(plistlib.dumps({**helper_info, key: value}))
                        with self.subTest(answer=answer, key=key, value=value), self.assertRaisesRegex(ValueError, "encryption declaration"):
                            validate_store_bundle(app, declared)
                        helper.write_bytes(plistlib.dumps(helper_info))

    def test_upload_candidate_receipt_keeps_submission_and_upload_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "Fixture.app"
            app.mkdir()
            args, config, _ = self.arguments(*self.upload_flags(), "--profile-free")
            output = root / "candidate"
            commands = []

            def package_command(arguments):
                commands.append(arguments)
                self.assertEqual(arguments[:2], ["/usr/bin/productbuild", "--component"])
                self.assertIn("--sign", arguments)
                Path(arguments[-1]).write_bytes(b"fixture package")

            with patch("build_store.parse_arguments", return_value=(args, config, output)), \
                    patch("build_store.validate_profile_free_capabilities") as capabilities, \
                    patch("build_store.read_profile", side_effect=AssertionError("No profile read in profile-free mode")), \
                    patch("build_store.source_hashes", return_value={"fixture": "hash"}), \
                    patch("build_store.build_app", return_value=app) as build, \
                    patch("build_store.validate_store_bundle", return_value={}) as bundle_check, \
                    patch("build_store.checked", side_effect=package_command), \
                    patch("build_store.verify_package", return_value={}) as package_check, \
                    patch("build_store.validate_no_quarantine") as quarantine_check, \
                    patch("subprocess.run", side_effect=AssertionError("No real tools or account calls")), \
                    contextlib.redirect_stdout(io.StringIO()):
                build_store.main()
            report = json.loads((output / "STORE-PACKAGE-CHECKS.json").read_text())
            self.assertTrue(report["upload_candidate"])
            self.assertEqual(report["build_intent"], "store-upload-candidate")
            self.assertIsNone(report["provisioning_profile"])
            self.assertEqual(report["encryption_classification"], "deferred")
            self.assertFalse(report["submission_ready"])
            self.assertFalse(report["uploaded"])
            self.assertFalse(report["testflight_validated"])
            self.assertIn("Owner-approved encryption classification", report["remaining_gates"][0])
            self.assertEqual(report["channel"], "store-distribution")
            self.assertEqual(report["provisioning_mode"], "profile-free-unrestricted")
            capabilities.assert_called_once_with()
            build.assert_called_once_with(args.app_signing_identity, sandboxed=True, build_root=output, configuration=config)
            bundle_check.assert_called_once_with(app, config, None)
            self.assertEqual(package_check.call_count, 1)
            self.assertEqual(quarantine_check.call_count, 2)
            self.assertEqual(len(commands), 1)

    def test_store_fixture_binary_marker_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Fixture.app"
            executable = app / "Contents/MacOS/AgentBoard"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"binary\x00--board-url\x00")
            with self.assertRaisesRegex(ValueError, "fixture mode"):
                build_store.validate_store_fixture_exclusion(app)
            executable.write_bytes(b"binary without fixture support")
            build_store.validate_store_fixture_exclusion(app)

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


class StoreProfileSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="agent-board-profile-fixture-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.external = self.root / "external.provisionprofile"
        self.original = b"disposable signed profile A"
        self.replacement = b"different unvalidated profile B"
        self.external.write_bytes(self.original)
        self.config = AppConfiguration("com.example.profile", "0.2.5", "71", signing_channel="store-distribution",
                                       team_id="ABCDEFGHIJ", provisioning_profile=self.external,
                                       require_testflight_profile=True, uses_non_exempt_encryption=False)
        self.payload = {"Platform": ["OSX"], "TeamIdentifier": ["ABCDEFGHIJ"],
                        "ExpirationDate": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30),
                        "DeveloperCertificates": [b"public certificate A"],
                        "Entitlements": {"com.apple.application-identifier": "ABCDEFGHIJ.com.example.profile",
                                         "com.apple.developer.team-identifier": "ABCDEFGHIJ"}}

    def cms(self, arguments):
        self.assertEqual(arguments[:4], ["/usr/bin/security", "cms", "-D", "-i"])
        snapshot = Path(arguments[4])
        self.assertNotEqual(snapshot, self.external)
        self.assertEqual(snapshot.read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(snapshot.parent.stat().st_mode), 0o700)
        return subprocess.CompletedProcess(arguments, 0, plistlib.dumps(self.payload).decode(), "")

    def read(self, side_effect=None):
        with patch("build_store.checked", side_effect=side_effect or self.cms), \
                patch("subprocess.run", side_effect=AssertionError("No real CMS, compiler, or signer")):
            return build_store.read_profile(self.external, self.config)

    def test_profile_replaced_after_preflight_cannot_change_validated_or_embedded_input(self):
        snapshots = []

        def replacement_during_cms(arguments):
            response = self.cms(arguments)
            snapshots.append(Path(arguments[4]))
            self.external.write_bytes(self.replacement)
            return response

        profile = self.read(replacement_during_cms)
        self.assertEqual(profile["data"], self.original)
        self.assertEqual(profile["sha256"], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(self.external.read_bytes(), self.replacement)
        self.assertTrue(all(not path.exists() for path in snapshots))
        app = self.root / "Fixture.app"
        (app / "Contents").mkdir(parents=True)
        embedded = app / "Contents/embedded.provisionprofile"
        embedded.write_bytes(self.original)
        build_store.validate_embedded_profile(app, self.config, profile)
        # Matching the current external path is insufficient. Only pinned A is valid.
        embedded.write_bytes(self.replacement)
        with self.assertRaisesRegex(ValueError, "validated snapshot"):
            build_store.validate_embedded_profile(app, self.config, profile)
        embedded.unlink()
        embedded.symlink_to(self.external)
        with self.assertRaises(ValueError):
            build_store.validate_embedded_profile(app, self.config, profile)

    def test_profile_snapshot_changed_during_cms_is_rejected(self):
        def modified_snapshot(arguments):
            response = self.cms(arguments)
            Path(arguments[4]).write_bytes(self.replacement)
            return response

        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            self.read(modified_snapshot)

    def test_profile_input_change_during_read_is_rejected_before_cms(self):
        original_fstat = os.fstat
        calls = []

        def changing_stat(descriptor):
            calls.append(descriptor)
            if len(calls) == 2:
                self.external.write_bytes(self.replacement)
            return original_fstat(descriptor)

        with patch("build_store.os.fstat", side_effect=changing_stat), \
                patch("build_store.checked", side_effect=AssertionError("Unstable profile must not reach CMS")), \
                self.assertRaisesRegex(ValueError, "changed while"):
            build_store.read_profile(self.external, self.config)

    def test_profile_links_empty_oversized_and_special_inputs_are_rejected_before_cms(self):
        linked = self.root / "linked.provisionprofile"
        linked.symlink_to(self.external)
        empty = self.root / "empty.provisionprofile"
        empty.touch()
        oversized = self.root / "oversized.provisionprofile"
        with oversized.open("wb") as handle:
            handle.truncate(build_store.MAX_PROFILE_BYTES + 1)
        fifo = self.root / "fifo.provisionprofile"
        os.mkfifo(fifo)
        with patch("build_store.checked", side_effect=AssertionError("Unsafe input must not reach CMS")):
            for path in (linked, empty, oversized, fifo, self.root):
                with self.subTest(input=path.name), self.assertRaises(ValueError):
                    build_store.read_profile(path, self.config)

    def test_invalid_cms_profile_and_bad_snapshot_digest_fail_closed(self):
        self.payload["TeamIdentifier"] = ["WRONGTEAM1"]
        with self.assertRaisesRegex(ValueError, "requested team"):
            self.read()
        self.payload["TeamIdentifier"] = ["ABCDEFGHIJ"]
        profile = self.read()
        for changed in ({**profile, "data": self.replacement}, {**profile, "sha256": "0" * 64},
                        {**profile, "data": bytearray(self.original)}, {}, None):
            with self.subTest(profile_type=type(changed).__name__), self.assertRaises(ValueError):
                build_store.validated_profile_bytes(changed)

    def test_build_app_embeds_pinned_profile_after_external_replacement(self):
        profile = self.read()
        self.external.write_bytes(self.replacement)
        source = self.root / "source"
        (source / "assets").mkdir(parents=True)
        (source / "assets/AgentBoard.icns").write_bytes(b"icns-fixture")
        (source / "MacApp.swift").write_text("fixture native source")
        (source / "entitlements").mkdir()
        (source / "entitlements/sandbox-app.plist").write_bytes(plistlib.dumps(SANDBOX_APP_ENTITLEMENTS))
        commands = []

        def fake_tool(arguments, **kwargs):
            commands.append(arguments)
            if arguments[0] == "/usr/bin/swiftc":
                Path(arguments[-1]).write_bytes(b"fixture executable")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        missing = self.root / "missing-snapshot-output"
        with patch("subprocess.run", side_effect=AssertionError("Missing snapshot must fail before any tools")), \
                self.assertRaisesRegex(ValueError, "validated profile snapshot"):
            build_app(configuration=self.config, sandboxed=True, build_root=missing)
        self.assertFalse(missing.exists())
        with patch("install.ROOT", source), patch("install.copy_license_notices"), patch("install.copy_alert_sounds"), \
                patch("build_support.copy_collector"), patch("build_support.copy_privacy_manifest"), \
                patch("build_support.validate_bundle", return_value=[]), \
                patch("build_support.xcode_build_metadata", return_value={}), \
                patch("subprocess.run", side_effect=fake_tool), contextlib.redirect_stdout(io.StringIO()):
            app = build_app("Apple Distribution: Fixture", configuration=self.config, sandboxed=True,
                            build_root=self.root / "output", provisioning_profile_bytes=profile["data"])
        self.assertEqual((app / "Contents/embedded.provisionprofile").read_bytes(), self.original)
        build_store.validate_embedded_profile(app, self.config, profile)
        self.assertEqual(self.external.read_bytes(), self.replacement)
        self.assertTrue(any("--entitlements" in command for command in commands))

    def test_provisioned_receipt_binds_private_snapshot_without_claiming_testflight(self):
        args = type("Arguments", (), {"distribution": True, "preview": False, "profile_free": False,
                                      "app_signing_identity": "Apple Distribution: Fixture",
                                      "installer_signing_identity": "3rd Party Mac Developer Installer: Fixture",
                                      "provisioning_profile": self.external})()
        output = self.root / "package-output"
        app = self.root / "Fixture.app"
        (app / "Contents").mkdir(parents=True)

        def tools(arguments):
            if arguments[0] == "/usr/bin/security":
                response = self.cms(arguments)
                self.external.write_bytes(self.replacement)
                return response
            self.assertEqual(arguments[:2], ["/usr/bin/productbuild", "--component"])
            Path(arguments[-1]).write_bytes(b"fixture signed package")

        def fixture_build(identity, **kwargs):
            self.assertEqual(kwargs["provisioning_profile_bytes"], self.original)
            (app / "Contents/embedded.provisionprofile").write_bytes(kwargs["provisioning_profile_bytes"])
            return app

        def verify_profile(bundle, config, profile):
            build_store.validate_embedded_profile(bundle, config, profile)
            return {"native_signer_certificate_sha256": {"Contents/MacOS/AgentBoard": "a" * 64}}

        with patch("build_store.parse_arguments", return_value=(args, self.config, output)), \
                patch("build_store.source_hashes", return_value={}), patch("build_store.checked", side_effect=tools), \
                patch("build_store.build_app", side_effect=fixture_build), \
                patch("build_store.validate_store_bundle", side_effect=verify_profile), \
                patch("build_store.verify_package", return_value={}), patch("build_store.validate_no_quarantine"), \
                patch("subprocess.run", side_effect=AssertionError("No real signing or account operations")), \
                contextlib.redirect_stdout(io.StringIO()):
            build_store.main()
        text = (output / "STORE-PACKAGE-CHECKS.json").read_text()
        report = json.loads(text)
        self.assertEqual(report["build_intent"], "testflight-profile-required")
        self.assertEqual(report["provisioning_profile"]["sha256"], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(report["provisioning_profile"]["authorized_certificate_sha256"],
                         [hashlib.sha256(b"public certificate A").hexdigest()])
        self.assertTrue(report["provisioning_profile"]["embedded_snapshot_verified"])
        self.assertNotIn(self.original.decode(), text)
        self.assertNotIn(str(self.external), text)
        for claim in ("testflight_validated", "submission_ready", "uploaded", "installed", "provider_calls"):
            self.assertFalse(report[claim])

    def test_profile_binding_checks_actual_main_signer_and_records_every_native_fingerprint(self):
        profile = self.read()
        app = self.root / "SignedFixture.app"
        main = app / "Contents/MacOS/AgentBoard"
        helper = app / HELPER / "Contents/MacOS/agent-board-collector"
        library = app / HELPER / "Contents/Frameworks/libpython3.12.dylib"
        for path in (main, helper, library):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"unsigned disposable native fixture")
        resources = app / "Contents/Resources"
        resources.mkdir()
        (resources / "AgentBoard.icns").write_bytes(b"icns-fixture")
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": self.config.bundle_id, "CFBundleVersion": "71", "CFBundleShortVersionString": "0.2.5",
            "CFBundlePackageType": "APPL", "AgentBoardSandboxed": True, "AgentBoardDistributionChannel": "store-distribution",
            "AgentBoardStoreDistribution": True, "AgentBoardStoreSupportStrategy": "hidden",
            "CFBundleSupportedPlatforms": ["MacOSX"], "DTPlatformName": "macosx", "DTSDKName": "macosx27.2",
            "LSApplicationCategoryType": "public.app-category.developer-tools", "CFBundleIconFile": "AgentBoard.icns",
            "LSMinimumSystemVersion": "13.0", "ITSAppUsesNonExemptEncryption": False}))
        (app / HELPER / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": self.config.helper_id, "CFBundleVersion": "71", "CFBundleShortVersionString": "0.2.5",
            "AgentBoardSandboxed": True, "AgentBoardStoreDistribution": True, "AgentBoardStoreSupportStrategy": "hidden",
            "LSBackgroundOnly": True}))
        policy = app / HELPER / "Contents/Resources/sandbox-collector-policy.json"
        policy.parent.mkdir()
        policy.write_text(json.dumps({"version": 1, "sandbox_required": True}))
        privacy_target = "static/about/privacy.html"
        privacy_source = "store-listing/site/privacy.html"
        privacy = policy.parent / privacy_target
        privacy.parent.mkdir(parents=True)
        privacy.write_bytes(build_store.runtime_asset_content(privacy_target, (ROOT / privacy_source).read_bytes()))
        embedded = app / "Contents/embedded.provisionprofile"
        embedded.write_bytes(profile["data"])
        certificates = {main: b"public certificate A", helper: b"public certificate B", library: b"public certificate C"}
        components = [{"path": str(path.relative_to(app)), "linked_sdk": ["27.2"]} for path in certificates]
        identifiers = {app: self.config.bundle_id, main: self.config.bundle_id,
                       app / HELPER: self.config.helper_id, helper: self.config.helper_id, library: "library"}
        corrupt_during_verification = []

        def public_signature_tools(arguments):
            path = Path(arguments[-1])
            if "--extract-certificates" in arguments:
                prefix = Path(arguments[-2])
                Path(str(prefix) + "0").write_bytes(certificates[path])
                if corrupt_during_verification and path == helper:
                    embedded.write_bytes(self.replacement)
            return subprocess.CompletedProcess(arguments, 0, "", "Identifier=" + identifiers[path] +
                "\nTeamIdentifier=ABCDEFGHIJ\nAuthority=Apple Distribution: Fixture\n")

        main_entitlements = {**SANDBOX_APP_ENTITLEMENTS,
                             "com.apple.application-identifier": "ABCDEFGHIJ." + self.config.bundle_id,
                             "com.apple.developer.team-identifier": "ABCDEFGHIJ"}
        from types import SimpleNamespace
        archive_reader = type(sys)("PyInstaller.archive.readers")
        archive_reader.CArchiveReader = lambda _: SimpleNamespace(
            open_embedded_archive=lambda _: SimpleNamespace(toc={}))
        with patch("build_store.checked", side_effect=public_signature_tools), patch("build_store.validate_no_quarantine"), \
                patch("build_store.validate_store_fixture_exclusion"), patch("build_store.validate_privacy", return_value="fixture"), \
                patch("build_store.signed_entitlements", side_effect=lambda path: main_entitlements if path == app else
                      SANDBOX_HELPER_ENTITLEMENTS if path == app / HELPER else {}), \
                patch("build_store.runtime_asset_mapping", return_value={privacy_target: privacy_source}), patch("build_store.ALERT_SOUND_IDS", ()), \
                patch("build_store.validate_bundle", return_value=components), \
                patch.dict(sys.modules, {"PyInstaller.archive.readers": archive_reader}), \
                patch("subprocess.run", side_effect=AssertionError("No real signing or key access")):
            proof = validate_store_bundle(app, self.config, profile)
            self.assertEqual(proof["native_signer_certificate_sha256"],
                             {str(path.relative_to(app)): hashlib.sha256(data).hexdigest() for path, data in certificates.items()})
            certificates[main] = b"unauthorized certificate"
            with self.assertRaisesRegex(ValueError, "not authorized by the supplied profile"):
                validate_store_bundle(app, self.config, profile)
            certificates[main] = b"public certificate A"
            corrupt_during_verification.append(True)
            with self.assertRaisesRegex(ValueError, "validated snapshot"):
                validate_store_bundle(app, self.config, profile)


class StoreArtifactTests(unittest.TestCase):

    def test_upload_candidate_app_plist_omits_both_unanswered_encryption_keys(self):
        config = AppConfiguration("com.example.uploadcandidate", "0.2.5", "70", signing_channel="store-distribution",
                                  team_id="ABCDEFGHIJ", defer_encryption_classification=True)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            (source / "assets").mkdir(parents=True)
            (source / "assets/AgentBoard.icns").write_bytes(b"icns-fixture")
            (source / "MacApp.swift").write_text("fixture native source")
            commands = []

            def fake_process(arguments, **kwargs):
                commands.append(arguments)
                if arguments[0] == "/usr/bin/swiftc":
                    Path(arguments[-1]).write_bytes(b"fixture executable")
                return subprocess.CompletedProcess(arguments, 0, stdout="", stderr="")

            with patch("install.ROOT", source), patch("install.copy_license_notices"), patch("install.copy_alert_sounds"), \
                    patch("build_support.copy_collector"), patch("build_support.copy_privacy_manifest"), \
                    patch("build_support.validate_bundle", return_value=[]), \
                    patch("build_support.xcode_build_metadata", return_value={"CFBundleSupportedPlatforms": ["MacOSX"]}), \
                    patch("subprocess.run", side_effect=fake_process), contextlib.redirect_stdout(io.StringIO()):
                app = build_app("Apple Distribution: Example", sandboxed=True, build_root=Path(directory) / "output", configuration=config)
            info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
            self.assertNotIn("ITSAppUsesNonExemptEncryption", info)
            self.assertNotIn("ITSEncryptionExportComplianceCode", info)
            self.assertTrue(info["AgentBoardSandboxed"])
            self.assertTrue(info["AgentBoardStoreDistribution"])
            self.assertEqual(info["AgentBoardDistributionChannel"], "store-distribution")
            self.assertNotIn("AGENT_BOARD_DIRECT", commands[0])
            self.assertIn("--entitlements", commands[1])
            self.assertNotIn("runtime", commands[1])

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
                                     "static/icons/claude.svg", "static/icons/codex.svg", "integrations"):
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


class DirectReleaseIdentityTests(unittest.TestCase):
    def test_download_requires_explicit_version_and_build(self):
        from build_release import parse_arguments

        with contextlib.redirect_stderr(io.StringIO()):
            for arguments in ([], ["--version", "0.2.5"], ["--build-number", "71"]):
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                    parse_arguments(arguments)
        _, configuration = parse_arguments(["--version", "0.2.5", "--build-number", "71"])
        self.assertEqual((configuration.version, configuration.build_number), ("0.2.5", "71"))

    def test_download_refuses_an_older_collector_in_the_selected_app(self):
        from build_release import verify_release_identity

        configuration = AppConfiguration("local.agentboard.display", "0.2.5", "71")
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Agent Board.app"
            for bundle, identifier in ((app, configuration.bundle_id),
                                       (app / HELPER, configuration.helper_id)):
                contents = bundle / "Contents"
                contents.mkdir(parents=True, exist_ok=True)
                (contents / "Info.plist").write_bytes(plistlib.dumps({
                    "CFBundleIdentifier": identifier, "CFBundleVersion": "71",
                    "CFBundleShortVersionString": "0.2.5",
                }))
            self.assertEqual(verify_release_identity(app, configuration)["CFBundleVersion"], "71")
            helper_info = app / HELPER / "Contents/Info.plist"
            metadata = plistlib.loads(helper_info.read_bytes())
            metadata["CFBundleVersion"] = "9"
            helper_info.write_bytes(plistlib.dumps(metadata))
            with self.assertRaisesRegex(SystemExit, "match the requested direct release identity"):
                verify_release_identity(app, configuration)


if __name__ == "__main__":
    unittest.main()
