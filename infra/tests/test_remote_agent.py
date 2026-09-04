#!/usr/bin/env python3
"""
Test Suite for Inverted RemoteDev Hybrid Agent Architecture.
Validates:
1. Config & Antigravity IDE policy synchronization (Windows -> Linux)
2. Prompt dispatch (Windows -> Linux)
3. Real-time thinking stream (Linux -> Windows)
4. Real-time file access notifications (Linux -> Windows)
5. Review policy enforcement (suspension, approval prompt, and response)
6. Review policy rejection handling
7. Automated file sync of agent-generated files to Windows workspace
"""

import os
import sys
import time
import json
import asyncio
import shutil
from pathlib import Path
import aiohttp

# Setup paths
test_dir = Path(__file__).resolve().parent
infra_dir = test_dir.parent
root_dir = infra_dir.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
if str(infra_dir) not in sys.path:
    sys.path.insert(0, str(infra_dir))

from bridge.protocol import (
    MSG_AUTH_REQ,
    MSG_AUTH_RESP,
    MSG_CONFIG_SYNC,
    MSG_AGENT_START_TASK,
    MSG_AGENT_CANCEL_TASK,
    MSG_AGENT_THOUGHT,
    MSG_AGENT_TOOL_CALL,
    MSG_AGENT_TOOL_RESULT,
    MSG_AGENT_FILE_ACCESS,
    MSG_AGENT_APPROVAL_REQ,
    MSG_AGENT_APPROVAL_RESP,
    MSG_AGENT_TOKEN,
    MSG_AGENT_TASK_COMPLETED,
    create_message,
    parse_message,
)
from bridge.service import WindowsBridgeServer
from bridge.client import LinuxBridgeClient

TEST_PORT = 8799
TEST_TOKEN = "test-secret-token"
TEMP_DIR = test_dir / "temp_agent_test"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def setup_temp_workspace():
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    win_ws = TEMP_DIR / "win_ws"
    linux_ws = TEMP_DIR / "linux_ws"
    win_ws.mkdir()
    linux_ws.mkdir()

    cfg_path = TEMP_DIR / "config.yaml"
    cfg = {
        "server": {"host": "127.0.0.1", "port": TEST_PORT, "token": TEST_TOKEN},
        "workspace": {"windows_path": str(win_ws), "linux_path": str(linux_ws)},
        "sync": {"debounce_ms": 50, "max_file_size_mb": 10, "ignore_patterns": [".git/**"]},
        "execution": {"default_timeout_sec": 30, "allowed_shells": ["powershell", "cmd"]},
        "agent": {"review_policy": "request-review", "model": "gemini-2.5-flash"},
        "diagnostics": {"history_limit": 20},
    }
    import yaml
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f)

    return str(cfg_path), win_ws, linux_ws


async def run_suite():
    print(f"\n{CYAN}{BOLD}=================================================================={RESET}")
    print(f"{CYAN}{BOLD} Starting Inverted Hybrid Remote Agent Test Suite {RESET}")
    print(f"{CYAN}{BOLD}=================================================================={RESET}\n")

    cfg_path, win_ws, linux_ws = setup_temp_workspace()

    # 1. Start Windows Bridge Server
    server = WindowsBridgeServer(cfg_path)
    await server.start()
    print(f"{GREEN}[OK] Windows Host Service started on port {TEST_PORT}{RESET}")

    # 2. Start Linux Client with AgentRunner
    os.environ["REMOTEDEV_MOCK_AGENT"] = "1"
    os.environ["LINUX_WORKSPACE"] = str(linux_ws)
    client = LinuxBridgeClient(cfg_path, server_url=f"ws://127.0.0.1:{TEST_PORT}/ws")
    client_task = asyncio.create_task(client.run())

    # Wait for connection and auth
    for _ in range(50):
        if client.diagnostics.is_connected:
            break
        await asyncio.sleep(0.1)

    assert client.diagnostics.is_connected, "Linux Client failed to connect to Windows Bridge"
    print(f"{GREEN}[OK] Linux Bridge Client connected & authenticated{RESET}")


    # 3. Test Config Synchronization
    await asyncio.sleep(0.5)
    assert client.agent_runner is not None, "Linux Agent Runner was not initialized"
    assert client.agent_runner.review_policy == "request-review", f"Review policy not synced: {client.agent_runner.review_policy}"
    print(f"{GREEN}[OK] Test 1 Passed: Antigravity IDE review policy & config synced to Linux{RESET}")

    # 4. Connect a Windows Frontend WebSocket (representing CLI / IDE)
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"ws://127.0.0.1:{TEST_PORT}/ws") as frontend_ws:
            await frontend_ws.send_str(create_message(
                MSG_AUTH_REQ,
                token=TEST_TOKEN,
                client_id="frontend-tester",
                role="agent_frontend",
            ))
            auth_msg = await frontend_ws.receive()
            auth_data = parse_message(auth_msg.data)
            assert auth_data.get("success"), "Frontend auth failed"
            print(f"{GREEN}[OK] Frontend observer connected to Windows Bridge{RESET}")

            # 5. Dispatch Prompt (Create a new Dart file)
            task_id = "test_task_create_file"
            prompt = "Please create and write a new file file:test_app.dart"

            print(f"{CYAN}Dispatching prompt: '{prompt}'...{RESET}")
            await frontend_ws.send_str(create_message(
                MSG_AGENT_START_TASK,
                task_id=task_id,
                prompt=prompt,
                model="gemini-2.5-flash",
            ))

            received_thought = False
            received_file_access = False
            received_approval_req = False
            task_completed = False

            while not task_completed:
                msg = await frontend_ws.receive()
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                data = parse_message(msg.data)
                mtype = data.get("type")

                if mtype == MSG_AGENT_THOUGHT:
                    received_thought = True
                    print(f"  {CYAN}[Live Thinking] {data.get('thought')[:60]}...{RESET}")

                elif mtype == MSG_AGENT_FILE_ACCESS:
                    received_file_access = True
                    print(f"  {YELLOW}[Live File Access] {data.get('action').upper()} -> {data.get('rel_path')}{RESET}")

                elif mtype == MSG_AGENT_APPROVAL_REQ:
                    received_approval_req = True
                    call_id = data.get("call_id")
                    print(f"  {RED}[Approval Prompt] {data.get('action_desc')} -> Approving...{RESET}")
                    # Simulate user approval on Windows!
                    await frontend_ws.send_str(create_message(
                        MSG_AGENT_APPROVAL_RESP,
                        call_id=call_id,
                        approved=True,
                    ))

                elif mtype == MSG_AGENT_TASK_COMPLETED:
                    task_completed = True
                    print(f"  {GREEN}[Task Completed] {data.get('summary')}{RESET}")

            assert received_thought, "Did not receive thinking stream from Linux agent"
            print(f"{GREEN}[OK] Test 2 Passed: Real-time thinking stream received on Windows{RESET}")

            assert received_file_access, "Did not receive file access event from Linux agent"
            print(f"{GREEN}[OK] Test 3 Passed: Real-time file access notification received on Windows{RESET}")

            assert received_approval_req, "Did not receive approval request under review policy"
            print(f"{GREEN}[OK] Test 4 Passed: Antigravity review policy approval requested and approved{RESET}")

            # 6. Verify file synced to Windows workspace
            await asyncio.sleep(0.5)
            created_file = win_ws / "test_app.dart"
            assert created_file.is_file(), f"File {created_file} was not synced to Windows workspace"
            content = created_file.read_text(encoding="utf-8")
            assert "RemoteDev Linux Agent" in content
            print(f"{GREEN}[OK] Test 5 Passed: Linux agent file write automatically synced to Windows workspace{RESET}")

            # 7. Test Policy Rejection (Deny execution)
            task_id_deny = "test_task_deny"
            await frontend_ws.send_str(create_message(
                MSG_AGENT_START_TASK,
                task_id=task_id_deny,
                prompt="Please write sensitive file:secret.dart",
                model="gemini-2.5-flash",
            ))

            denied_task_completed = False
            while not denied_task_completed:
                msg = await frontend_ws.receive()
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                data = parse_message(msg.data)
                if data.get("type") == MSG_AGENT_APPROVAL_REQ:
                    call_id = data.get("call_id")
                    print(f"  {RED}[Approval Prompt] Denying execution for {call_id}...{RESET}")
                    # Send rejection from Windows!
                    await frontend_ws.send_str(create_message(
                        MSG_AGENT_APPROVAL_RESP,
                        call_id=call_id,
                        approved=False,
                        reason="Disallowed by user",
                    ))
                elif data.get("type") == MSG_AGENT_TASK_COMPLETED and data.get("task_id") == task_id_deny:
                    denied_task_completed = True

            secret_file = win_ws / "secret.dart"
            assert not secret_file.exists(), "File should not exist because action was denied"
            print(f"{GREEN}[OK] Test 6 Passed: Denied tool execution was correctly rejected and blocked{RESET}")


    # Cleanup
    await client.stop()
    client_task.cancel()
    await server.stop()
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

    print(f"\n{GREEN}{BOLD}=================================================================={RESET}")
    print(f"{GREEN}{BOLD} ALL TESTS PASSED: Inverted Hybrid Remote Agent Fully Verified! {RESET}")
    print(f"{GREEN}{BOLD}=================================================================={RESET}\n")


if __name__ == "__main__":
    asyncio.run(run_suite())
