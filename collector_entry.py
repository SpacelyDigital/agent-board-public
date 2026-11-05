"""Frozen collector entry point, including its isolated credential-read helpers."""

import runpy
import sys
import os
import json
import plistlib
from pathlib import Path


def sandbox_build_required():
    """Read the policy embedded in the signed sandbox helper, not its argv."""
    if not getattr(sys, "frozen", False):
        return False
    contents = Path(sys.executable).parent.parent
    bundle_requires_sandbox = False
    if contents.name == "Contents" and (contents / "Info.plist").is_file():
        try:
            info = plistlib.loads((contents / "Info.plist").read_bytes())
            bundle_requires_sandbox = info.get("AgentBoardSandboxed") is True
        except (OSError, ValueError, TypeError, AttributeError):
            raise SystemExit("The packaged collector policy is invalid.") from None
        # PyInstaller's _MEIPASS aliases this resource through Frameworks. Read
        # its regular signed resource directly, without weakening link checks.
        policy = contents / "Resources/sandbox-collector-policy.json"
    else:
        policy = Path(sys._MEIPASS) / "sandbox-collector-policy.json"
    if not policy.exists() and not policy.is_symlink():
        if bundle_requires_sandbox:
            raise SystemExit("The packaged collector policy is missing.")
        return False
    if policy.is_symlink() or not policy.is_file() or policy.stat().st_size > 1024:
        raise SystemExit("The packaged collector policy is invalid.")
    try:
        value = json.loads(policy.read_text(encoding="utf-8"))
        if (value != {"version": 1, "sandbox_required": True}
                or type(value["version"]) is not int
                or value["sandbox_required"] is not True):
            raise ValueError
    except (OSError, ValueError, UnicodeError):
        raise SystemExit("The packaged collector policy is invalid.") from None
    return True


def main():
    if sandbox_build_required():
        os.environ["AGENT_BOARD_SANDBOX"] = "1"
    # Store presentation is also signed configuration. An inherited shell flag
    # cannot change which support/payment controls this bundle exposes.
    os.environ.pop("AGENT_BOARD_STORE_PROFILE", None)
    if getattr(sys, "frozen", False):
        info_path = Path(sys.executable).parent.parent / "Info.plist"
        try:
            info = plistlib.loads(info_path.read_bytes())
            if info.get("AgentBoardStoreDistribution") is True:
                if (info.get("AgentBoardSandboxed") is not True
                        or info.get("AgentBoardStoreSupportStrategy") not in {"hidden", "us-external"}):
                    raise ValueError
                os.environ["AGENT_BOARD_STORE_PROFILE"] = "1"
                os.environ["AGENT_BOARD_SANDBOX"] = "1"
        except (OSError, ValueError, TypeError, AttributeError):
            raise SystemExit("The packaged collector distribution profile is invalid.") from None
    if getattr(sys, "frozen", False) and os.environ.get("AGENT_BOARD_STORE_PROFILE") != "1":
        import certifi
        # The build machine's OpenSSL paths may not exist on the recipient Mac.
        os.environ["SSL_CERT_FILE"] = certifi.where()
    helper = {
        "--read-claude-usage": "usage_source",
        "--read-projects": "claude_project_source",
    }
    if os.environ.get("AGENT_BOARD_SANDBOX") == "1" and sys.argv[1:2] in (
            ["--read-claude-usage"], ["--read-projects"], ["--authorize-claude"],
            ["--reader-watchdog"], ["--reader-launch"]):
        # A sandbox build must never fall back to saved credentials or spawn
        # a provider executable, even if a helper mode is invoked directly.
        raise SystemExit("This account helper is unavailable in the sandbox preview.")
    if len(sys.argv) == 3 and sys.argv[1] == "--reader-watchdog":
        from process_reader import watch_reader_groups
        watch_reader_groups(int(sys.argv[2]))
    elif len(sys.argv) >= 5 and sys.argv[1] == "--reader-launch" and sys.argv[3] == "--":
        from process_reader import launch_reader
        launch_reader(int(sys.argv[2]), sys.argv[4:])
    elif sys.argv[1:] == ["--authorize-claude"]:
        from usage_source import authorize_claude_account
        try:
            print(json.dumps(authorize_claude_account()))
        except Exception:
            print(json.dumps({"ok": False, "error": "Claude account access is unavailable."}))
    elif len(sys.argv) == 2 and sys.argv[1] in helper:
        runpy.run_module(helper[sys.argv[1]], run_name="__main__")
    else:
        # The native app owns this group, including isolated usage/project
        # helpers. Quit and privacy-setting changes can stop every owned read.
        if os.getpgid(0) != os.getpid():
            os.setsid()
        from server import main as run_server
        run_server()


if __name__ == "__main__":
    main()
