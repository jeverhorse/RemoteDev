#!/usr/bin/env python3
"""
RemoteDev Command Bridge CLI (`win-run`).
Allows developers and AI agents in the Linux environment to execute commands
on the Windows host in real-time, stream stdout/stderr, handle Ctrl+C cancellation,
and inspect bridge diagnostics.
"""

import os
import sys
import yaml
import time
import json
import signal
import asyncio
import argparse
from pathlib import Path
from typing import Optional
import aiohttp

# Add parent directory to sys.path
current_dir = Path(__file__).resolve().parent
infra_dir = current_dir.parent
root_dir = infra_dir.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
if str(infra_dir) not in sys.path:
    sys.path.insert(0, str(infra_dir))

from bridge.protocol import (
    MSG_AUTH_REQ,
    MSG_AUTH_RESP,
    MSG_EXECUTE_COMMAND,
    MSG_CANCEL_COMMAND,
    MSG_COMMAND_OUTPUT,
    MSG_COMMAND_COMPLETED,
    MSG_STATUS_REQ,
    MSG_STATUS_RESP,
    create_message,
    parse_message,
)


def load_config(config_path_opt: Optional[str] = None) -> dict:
    candidates = [
        config_path_opt,
        os.environ.get("REMOTEDEV_CONFIG"),
        str(infra_dir / "config" / "config.yaml"),
        "/workspace/infra/config/config.yaml",
        "./infra/config/config.yaml",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            with open(c, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
    return {
        "server": {"host": "host.docker.internal", "port": 8765, "token": "remotedev-secret-hybrid-token-2026"},
        "execution": {"default_timeout_sec": 120},
    }


async def run_command_remote(
    command_str: str,
    shell: str,
    cwd: Optional[str],
    timeout: Optional[int],
    server_url: str,
    token: str,
) -> int:
    """Connects to Windows executor bridge, executes command, streams output, returns exit code."""
    command_id = f"cmd_{int(time.time() * 1000)}"
    cancelled = False

    async with aiohttp.ClientSession() as session:
        try:
            async with session.ws_connect(server_url, max_msg_size=64 * 1024 * 1024) as ws:
                # 1. Authenticate
                await ws.send_str(create_message(
                    MSG_AUTH_REQ,
                    token=token,
                    client_id=f"cli_{os.getpid()}",
                    role="cli",
                ))

                auth_msg = await ws.receive()
                if auth_msg.type != aiohttp.WSMsgType.TEXT:
                    sys.stderr.write("[win-run] Failed to authenticate: no response\n")
                    return 1

                auth_resp = parse_message(auth_msg.data)
                if not auth_resp.get("success"):
                    sys.stderr.write(f"[win-run] Authentication failed: {auth_resp.get('message')}\n")
                    return 1

                # 2. Setup cancellation handler for Ctrl+C
                loop = asyncio.get_running_loop()

                def handle_sigint():
                    nonlocal cancelled
                    if not cancelled:
                        cancelled = True
                        sys.stderr.write("\n[win-run] Interrupt received. Cancelling command on Windows...\n")
                        asyncio.create_task(ws.send_str(create_message(MSG_CANCEL_COMMAND, command_id=command_id)))

                # Register signal handler if on POSIX
                if sys.platform != "win32":
                    try:
                        loop.add_signal_handler(signal.SIGINT, handle_sigint)
                    except (ValueError, NotImplementedError):
                        pass

                # 3. Send execute request
                await ws.send_str(create_message(
                    MSG_EXECUTE_COMMAND,
                    command_id=command_id,
                    command=command_str,
                    shell=shell,
                    cwd=cwd,
                    timeout=timeout,
                ))

                # 4. Stream output chunks until completion
                exit_code = 0
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = parse_message(msg.data)
                        m_type = data.get("type")

                        if m_type == MSG_COMMAND_OUTPUT:
                            stream_name = data.get("stream", "stdout")
                            chunk = data.get("chunk", "")
                            if stream_name == "stderr":
                                sys.stderr.write(chunk)
                                sys.stderr.flush()
                            else:
                                sys.stdout.write(chunk)
                                sys.stdout.flush()

                        elif m_type == MSG_COMMAND_COMPLETED:
                            exit_code = data.get("exit_code", 0)
                            status = data.get("status", "success")
                            duration = data.get("duration_ms", 0)
                            if status == "cancelled":
                                sys.stderr.write(f"[win-run] Command cancelled by user (duration: {duration}ms)\n")
                                exit_code = 130
                            elif status == "timeout":
                                sys.stderr.write(f"[win-run] Command timed out after {timeout}s\n")
                                exit_code = 124
                            break

                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        sys.stderr.write("[win-run] Bridge connection lost unexpectedly.\n")
                        return 1

                return exit_code

        except aiohttp.ClientConnectorError as ce:
            sys.stderr.write(f"[win-run] Error: Could not connect to Windows bridge at {server_url} ({ce})\n")
            sys.stderr.write("Make sure the Windows Host Executor daemon is running.\n")
            return 1


async def fetch_status(server_url: str, token: str) -> int:
    """Requests and prints formatted diagnostics status."""
    http_url = server_url.replace("ws://", "http://").replace("wss://", "https://").replace("/ws", "/status")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(http_url, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                if resp.status != 200:
                    sys.stderr.write(f"[win-run] Status endpoint returned HTTP {resp.status}\n")
                    return 1
                data = await resp.json()

                print("=" * 65)
                print("         REMOTEDEV HYBRID BRIDGE DIAGNOSTICS")
                print("=" * 65)
                conn_str = "CONNECTED" if data.get("is_connected") else "DISCONNECTED"
                print(f"Windows Host Status : {conn_str}")
                print(f"Peer Address        : {data.get('peer_info', 'None')}")
                print(f"Uptime              : {data.get('uptime_seconds', 0)}s")
                print("-" * 65)
                sync = data.get("sync", {})
                last_evt = sync.get("last_event")
                last_evt_str = f"[{last_evt['time_str']}] {last_evt['direction'].upper()} {last_evt['action']} {last_evt['path']}" if last_evt else "None"
                print(f"Sync - Last Event   : {last_evt_str}")
                print(f"Sync - Sent/Recv    : {sync.get('total_sent', 0)} sent / {sync.get('total_received', 0)} received")
                print(f"Sync - Pending      : {sync.get('pending_operations', 0)} pending operations")
                print(f"Sync - Conflicts    : {sync.get('conflicts_count', 0)} detected")
                for c in sync.get("recent_conflicts", []):
                    print(f"   * [{c['time_str']}] {c['path']} (Backup: {c.get('backup_path')})")

                print("-" * 65)
                cmds = data.get("commands", {})
                print(f"Active Commands     : {cmds.get('currently_running_count', 0)}")
                for r in cmds.get("running", []):
                    print(f"   * PID {r['pid']}: {r['cmd']} (started {r['start_str']})")

                history = cmds.get("recent_history", [])
                if history:
                    print("-" * 65)
                    print(f"Recent Executions (Last {len(history)}):")
                    for h in history[-5:]:
                        print(f"   [{h['completed_str']}] (Exit: {h['exit_code']}) {h['cmd']} ({h['duration_ms']}ms) - {h['status']}")
                print("=" * 65)
                return 0
        except Exception as e:
            sys.stderr.write(f"[win-run] Failed to fetch status from {http_url}: {e}\n")
            return 1


def main():
    # Handle status shortcut
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        config = load_config()
        host = os.environ.get("WINDOWS_HOST", config["server"].get("host", "host.docker.internal"))
        if host == "0.0.0.0":
            host = "127.0.0.1"
        port = config["server"].get("port", 8765)
        server_url = f"ws://{host}:{port}/ws"
        token = config["server"].get("token", "")
        code = asyncio.run(fetch_status(server_url, token))
        sys.exit(code)

    parser = argparse.ArgumentParser(
        prog="win-run",
        description="Execute commands on the Windows host from the Linux agent environment in real-time.",
    )
    parser.add_argument("command", nargs="?", default=None, help="The command line string to run on Windows")
    parser.add_argument("--shell", choices=["powershell", "cmd", "bash"], default="powershell", help="Target shell")
    parser.add_argument("--cwd", default=None, help="Relative or absolute working directory on Windows")
    parser.add_argument("--timeout", type=int, default=None, help="Command timeout in seconds")
    parser.add_argument("--config", default=None, help="Path to config.yaml")

    args, remaining = parser.parse_known_args()

    config = load_config(args.config)
    host = os.environ.get("WINDOWS_HOST", config["server"].get("host", "host.docker.internal"))
    if host == "0.0.0.0":
        host = "127.0.0.1"
    port = config["server"].get("port", 8765)
    server_url = f"ws://{host}:{port}/ws"
    token = config["server"].get("token", "")

    if not args.command and not remaining:
        parser.print_help()
        sys.exit(1)

    full_cmd = args.command
    if remaining:
        full_cmd = f"{args.command} {' '.join(remaining)}" if args.command else " ".join(remaining)

    timeout = args.timeout or config["execution"].get("default_timeout_sec", 120)

    try:
        exit_code = asyncio.run(run_command_remote(
            command_str=full_cmd,
            shell=args.shell,
            cwd=args.cwd,
            timeout=timeout,
            server_url=server_url,
            token=token,
        ))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
