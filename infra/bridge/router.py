#!/usr/bin/env python3
"""
Transparent Command Router for RemoteDev Hybrid Environment.
Automatically dispatches commands between the Linux remote container
and the local Windows host based on toolchain and networking heuristics.
"""

import sys
import os
import argparse
from pathlib import Path

# Add paths
infra_dir = Path(__file__).resolve().parent.parent
root_dir = infra_dir.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
if str(infra_dir) not in sys.path:
    sys.path.insert(0, str(infra_dir))

from bridge.mcp_server import execute_in_linux_container, execute_on_windows_host

# Heuristics for commands that should execute in Linux (fast internet, Linux tools)
LINUX_KEYWORDS = {
    "curl", "wget", "apt", "apt-get", "dpkg", "pip", "pip3", "git clone",
    "bash", "sh", "uname", "cat", "grep", "sed", "awk", "tar", "gzip"
}

# Heuristics for commands that must execute on Windows (native SDKs, compilers)
WINDOWS_KEYWORDS = {
    "flutter", "dart", "powershell", "pwsh", "cmd", "gradle", "gradlew",
    "msbuild", "dotnet", "choco", "winget"
}


def decide_target(cmd: str, forced_target: str = "auto") -> str:
    """Determines whether command should run in 'linux' or 'windows'."""
    if forced_target in ("linux", "windows"):
        return forced_target

    cmd_lower = cmd.lower().strip()
    first_token = cmd_lower.split()[0] if cmd_lower.split() else ""

    # Check Windows first for Flutter / Dart
    for kw in WINDOWS_KEYWORDS:
        if first_token == kw or kw in cmd_lower:
            return "windows"

    # Check Linux keywords
    for kw in LINUX_KEYWORDS:
        if first_token == kw or kw in cmd_lower:
            return "linux"

    # Default to windows for local dev
    return "windows"


def main():
    parser = argparse.ArgumentParser(description="Transparent Command Router")
    parser.add_argument("command", help="Command string to execute")
    parser.add_argument("--target", choices=["auto", "linux", "windows"], default="auto")
    parser.add_argument("--cwd", default=None, help="Working directory")
    args = parser.parse_args()

    target = decide_target(args.command, args.target)

    if target == "linux":
        sys.stderr.write(f"[Transparent Router] -> Routing to Linux Agent Environment: {args.command}\n")
        res = execute_in_linux_container(args.command, args.cwd)
    else:
        sys.stderr.write(f"[Transparent Router] -> Routing to Windows Host Executor: {args.command}\n")
        res = execute_on_windows_host(args.command, args.cwd)

    if res["stdout"]:
        sys.stdout.write(res["stdout"])
        sys.stdout.flush()
    if res["stderr"]:
        sys.stderr.write(res["stderr"])
        sys.stderr.flush()

    sys.exit(res["exit_code"])


if __name__ == "__main__":
    main()
