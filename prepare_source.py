#!/usr/bin/env python3
"""Create an inspected, private source snapshot. Never uploads anything."""

import hashlib
import json
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parent
TOP_LEVEL = (
    ".gitignore", "README.md", "PRIVACY.md", "DISTRIBUTION.md", "HARDENING.md", "REAL_ENVIRONMENT_TEST_PLAN.md",
    "APP_STORE_READINESS.md", "APP_STORE_PACKAGING.md", "ENGINEERING_CHECKPOINT.json",
    "MacApp.swift", "CollectorRuntime.swift", "server.py", "board_store.py",
    "codex_source.py", "claude_source.py", "claude_project_source.py",
    "chat_links.py", "usage_source.py", "collector_entry.py", "install.py",
    "privacy_network.py", "source_access.py", "sandbox_sources.py",
    "entitlements/sandbox-app.plist", "entitlements/sandbox-helper.plist", "entitlements/sandbox-collector-policy.json",
    "build_support.py", "build_release.py", "build_store.py", "bootstrap_build.py", "requirements-build.txt", "prepare_source.py", "THIRD_PARTY.md",
    "privacy/PrivacyInfo.xcprivacy", "privacy/CollectorPrivacyInfo.xcprivacy",
    "assets/AgentBoard.icns", "tools/build_icon.swift", "tools/review_samples.py", "static/about/store.html",
    "tools/generate_alert_sounds.py", "static/sounds/README.md",
    "store-listing/README.md", "store-listing/APP_STORE_METADATA.md", "store-listing/PRIVACY_ANSWERS.md",
    "store-listing/REVIEW_NOTES.md", "store-listing/REVIEW_SAMPLES.md", "store-listing/site/privacy.html", "store-listing/site/support.html", "store-listing/site/site.css",
)
TESTS = (
    "test_board.py", "test_chat_links.py", "test_claude.py", "test_claude_project.py",
    "test_codex.py", "test_usage.py", "test_display_access.py", "fixture_control.py", "native_runtime_smoke.py",
    "test_adapter_redteam.py", "test_http_security.py", "test_store_redteam.py",
    "reader_lifetime_smoke.py", "privacy_network_smoke.py", "stress_browser.py", "stress_browser.cjs",
    "stress_browser.md", "native_state_smoke.py", "native_notification_smoke.py",
    "native_parent_lifetime_smoke.py", "native_navigation_smoke.py", "native_menu_recovery_smoke.py",
    "native_sample_login_smoke.py", "native_sample_preferences_smoke.py",
    "packaged_workflow_smoke.py", "packaged_workflow_smoke.cjs",
    "test_history_notifications.py", "native_history_notification_smoke.py",
    "test_privacy_network.py", "test_sandbox_policy.py", "test_claude_large_records.py",
    "native_sandbox_smoke.py", "SandboxRuntimeProof.swift",
    "native_chat_link_smoke.py",
    "build_native_ui_fixture.py",
    "test_native_launch_cleanup.py", "test_source_opt_in.py",
    "test_sandbox_grant_health.py",
    "test_store_support.py", "native_store_support_smoke.py",
    "test_store_packaging.py", "test_store_branding.py", "test_store_privacy.py", "test_sample_board.py", "test_codex_history_store.py", "test_codex_history_verification.py", "test_codex_sqlite_snapshot.py", "test_source_discovery.py",
    "test_local_quota.py", "test_local_quota_workflow.py", "local_quota_mod_smoke.mjs", "local_quota_ui_smoke.cjs", "source_revocation_ui_smoke.cjs", "test_review_samples.py",
    "focus_retention_ui_smoke.cjs", "theme_compatibility_ui_smoke.cjs",
    "native_log_storage_smoke.py",
    "native_startup_wait_smoke.py",
    "test_alert_sounds.py", "native_alert_sounds_smoke.py", "alert_sounds_ui_smoke.cjs",
)
SENSITIVE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}|"
    r"\bsk-(?:ant-|proj-)[A-Za-z0-9_-]{20,}|" + "/" + r"Users/[A-Za-z0-9][A-Za-z0-9._-]{0,80}/"
)


def main():
    from install import RUNTIME_FILES
    notices = {str(path.relative_to(ROOT)) for path in (ROOT / "licenses").rglob("*") if path.is_file()}
    files = sorted(set(TOP_LEVEL) | set(RUNTIME_FILES) | {"tests/" + name for name in TESTS}
                   | {"static/icons/README.md"} | notices)
    manifest = []
    captured = {}
    for name in files:
        source = ROOT / name
        if (not source.is_file() or not source.resolve().is_relative_to(ROOT)
                or any(path.is_symlink() for path in (source, *source.parents) if path.is_relative_to(ROOT))):
            raise SystemExit(f"Missing or substituted source file: {name}")
        content = source.read_bytes()
        if SENSITIVE.search(content.decode("utf-8", errors="ignore")):
            raise SystemExit(f"Review potential private content before export: {name}")
        mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
        captured[name] = (content, mode)
        manifest.append({"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(), "mode": mode})
    destination = ROOT / ".local/source-exports" / (time.strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns()))
    destination.mkdir(parents=True, mode=0o700)
    for item in manifest:
        target = destination / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        # Export exactly the bytes that passed the scan, even if a source file
        # changes before this write. No user metadata or extended attributes.
        content, mode = captured[item["path"]]
        target.write_bytes(content)
        target.chmod(mode)
    result = {"source_files": len(files), "automated_private_content_scan": "passed",
              "publication": "not uploaded", "open_source_license": "not selected", "files": manifest}
    (destination / "SOURCE-MANIFEST.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"path": str(destination), "source_files": len(files), "scan": "passed", "uploaded": False}))


if __name__ == "__main__":
    main()
