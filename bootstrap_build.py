#!/usr/bin/env python3
"""Install pinned build tools and a portable Python inside .local only."""

import json
from pathlib import Path
import platform
import subprocess
import sys
import time
import venv

ROOT = Path(__file__).resolve().parent
PYTHON_VERSION = "3.12.16"
UV_VERSION = "0.13.6"


def run(*command, **kwargs):
    subprocess.run(list(map(str, command)), cwd=ROOT, check=True, **kwargs)


def main():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("This release build is configured for Apple silicon Macs. Intel builds are not configured.")
    local = ROOT / ".local"
    local.mkdir(exist_ok=True)
    tools = local / "build-tools"
    if not (tools / "bin/python").is_file():
        venv.EnvBuilder(with_pip=True).create(tools)
    run(tools / "bin/python", "-m", "pip", "install", f"uv=={UV_VERSION}")
    uv = tools / "bin/uv"
    managed = local / "managed-python"
    common = ["--cache-dir", local / "uv-cache", "--no-config"]
    run(uv, "python", "install", PYTHON_VERSION, "--install-dir", managed, "--no-bin", *common)
    python = managed / f"cpython-{PYTHON_VERSION}-macos-aarch64-none/bin/python3.12"
    target = local / "build-venv"
    if target.exists():
        result = subprocess.run([str(target / "bin/python"), "-c",
                                 "import sys; print(sys.version.split()[0]); print(sys.base_prefix)"],
                                capture_output=True, text=True)
        lines = result.stdout.splitlines()
        if result.returncode or lines != [PYTHON_VERSION, str(python.parent.parent)]:
            target.rename(local / ("previous-build-venv-" + str(time.time_ns())))
    if not target.exists():
        # uv installs packages directly; ensurepip is absent from this portable
        # distribution and isn't needed by either the builder or recipient.
        run(python, "-m", "venv", "--without-pip", target)
    run(uv, "pip", "install", "--python", target / "bin/python", "-r", ROOT / "requirements-build.txt", *common)
    print(json.dumps({"build_python": str(target / "bin/python"), "python_version": PYTHON_VERSION,
                      "uv_version": UV_VERSION, "recipient_setup_required": False}))


if __name__ == "__main__":
    main()
