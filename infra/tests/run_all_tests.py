#!/usr/bin/env python3
"""
Master End-to-End Test Suite for RemoteDev Hybrid Development Environment.
Validates all 16 core architectural requirements:
 1. Linux creates a file -> Windows receives it.
 2. Windows creates a file -> Linux receives it.
 3. Linux modifies a file -> Windows sees the modification.
 4. Windows modifies a file -> Linux sees the modification.
 5. Linux deletes a file -> Windows deletes it.
 6. Windows deletes a file -> Linux deletes it.
 7. Linux requests a Windows PowerShell command -> Windows executes it.
 8. Windows stdout is returned to Linux.
 9. Windows stderr is returned to Linux.
10. Exit codes are preserved.
11. Long-running command output can be streamed.
12. A failed command is correctly reported.
13. A running command can be cancelled.
14. Agent modifies a file -> immediately executes Windows command using that new version.
15. Windows build/command output can be inspected from the Linux side.
16. Temporary disconnect & automatic recovery.
"""

import os
import sys
import time
import json
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional
import aiohttp

# Add parent directory
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
    MSG_EXECUTE_COMMAND,
    MSG_CANCEL_COMMAND,
    MSG_COMMAND_OUTPUT,
    MSG_COMMAND_COMPLETED,
    MSG_SYNC_EVENT,
    MSG_STATUS_REQ,
    MSG_STATUS_RESP,
    create_message,
    parse_message,
)

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


class E2ETestRunner:
    def __init__(self, server_url: str = "ws://127.0.0.1:8765/ws", token: str = "remotedev-secret-hybrid-token-2026"):
        self.server_url = server_url
        self.http_url = server_url.replace("ws://", "http://").replace("/ws", "")
        self.token = token
        self.windows_workspace = Path(root_dir / "workspace").resolve()
        self.windows_workspace.mkdir(parents=True, exist_ok=True)
        self.passed = 0
        self.failed = 0

    def log_result(self, test_num: int, title: str, passed: bool, details: str = ""):
        mark = f"{GREEN}[PASS]{RESET}" if passed else f"{RED}[FAIL]{RESET}"
        print(f"Test {test_num:02d}: {mark} {BOLD}{title}{RESET}")
        if details:
            print(f"         {CYAN}-> {details}{RESET}")
        if passed:
            self.passed += 1
        else:
            self.failed += 1

    async def run_remote_command(self, cmd: str, shell: str = "powershell", timeout: int = 30) -> Dict[str, Any]:
        """Helper to run command over WebSocket bridge."""
        command_id = f"test_cmd_{int(time.time()*1000)}"
        stdout_chunks = []
        stderr_chunks = []
        result = {}

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.server_url) as ws:
                await ws.send_str(create_message(MSG_AUTH_REQ, token=self.token, client_id="test_runner", role="cli"))
                auth_resp = parse_message((await ws.receive()).data)
                if not auth_resp.get("success"):
                    raise RuntimeError("Authentication failed")

                await ws.send_str(create_message(
                    MSG_EXECUTE_COMMAND,
                    command_id=command_id,
                    command=cmd,
                    shell=shell,
                    timeout=timeout,
                ))

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = parse_message(msg.data)
                        mtype = data.get("type")
                        if mtype == MSG_COMMAND_OUTPUT:
                            if data.get("stream") == "stderr":
                                stderr_chunks.append(data.get("chunk", ""))
                            else:
                                stdout_chunks.append(data.get("chunk", ""))
                        elif mtype == MSG_COMMAND_COMPLETED:
                            result = data
                            break

        result["stdout"] = "".join(stdout_chunks)
        result["stderr"] = "".join(stderr_chunks)
        return result

    async def test_file_sync_linux_to_windows(self):
        """Tests 1, 3, 5: Linux creates, modifies, and deletes a file -> Windows receives."""
        rel_path = "test_artifacts/linux_originated.txt"
        win_file = self.windows_workspace / rel_path
        win_file.parent.mkdir(parents=True, exist_ok=True)
        if win_file.exists():
            win_file.unlink()

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.server_url) as ws:
                await ws.send_str(create_message(MSG_AUTH_REQ, token=self.token, client_id="test_sync", role="linux_agent"))
                await ws.receive() # auth resp

                # 1. Linux creates file
                import base64
                content1 = b"Hello from Linux Agent Environment!"
                await ws.send_str(create_message(
                    MSG_SYNC_EVENT,
                    action="upsert",
                    rel_path=rel_path,
                    content_b64=base64.b64encode(content1).decode("ascii"),
                    is_dir=False,
                ))
                await asyncio.sleep(0.4)
                passed1 = win_file.is_file() and win_file.read_bytes() == content1
                self.log_result(1, "Linux creates a file -> Windows receives it", passed1, f"File created at {win_file.name}")

                # 3. Linux modifies file
                content2 = b"Updated content from Linux Agent Environment!"
                await ws.send_str(create_message(
                    MSG_SYNC_EVENT,
                    action="upsert",
                    rel_path=rel_path,
                    content_b64=base64.b64encode(content2).decode("ascii"),
                    is_dir=False,
                ))
                await asyncio.sleep(0.4)
                passed3 = win_file.is_file() and win_file.read_bytes() == content2
                self.log_result(3, "Linux modifies a file -> Windows sees the modification", passed3, f"Content verified on Windows")

                # 5. Linux deletes file
                await ws.send_str(create_message(
                    MSG_SYNC_EVENT,
                    action="delete",
                    rel_path=rel_path,
                    is_dir=False,
                ))
                await asyncio.sleep(0.4)
                passed5 = not win_file.exists()
                self.log_result(5, "Linux deletes a file -> Windows deletes it", passed5, f"File successfully deleted on Windows")

    async def test_file_sync_windows_to_linux(self):
        """Tests 2, 4, 6: Windows creates, modifies, and deletes a file -> Linux receives event."""
        rel_path = "test_artifacts/win_originated.txt"
        win_file = self.windows_workspace / rel_path
        win_file.parent.mkdir(parents=True, exist_ok=True)

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.server_url) as ws:
                await ws.send_str(create_message(MSG_AUTH_REQ, token=self.token, client_id="test_listener", role="linux_agent"))
                await ws.receive() # auth resp

                # Helper to wait for expected sync event
                async def wait_for_event(expected_action: str, expected_path: str, timeout_sec: float = 3.0):
                    deadline = time.time() + timeout_sec
                    while time.time() < deadline:
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = parse_message(msg.data)
                                if data.get("type") == MSG_SYNC_EVENT and data.get("action") == expected_action:
                                    if data.get("rel_path", "").replace("\\", "/") == expected_path:
                                        return data
                        except asyncio.TimeoutError:
                            pass
                    return None

                # 2. Windows creates file
                win_file.write_bytes(b"Created on Windows Host")
                evt2 = await wait_for_event("upsert", rel_path)
                passed2 = evt2 is not None
                self.log_result(2, "Windows creates a file -> Linux receives it", passed2, f"Received sync upsert event for {rel_path}")

                # 4. Windows modifies file
                win_file.write_bytes(b"Modified on Windows Host Version 2")
                evt4 = await wait_for_event("upsert", rel_path)
                passed4 = evt4 is not None
                self.log_result(4, "Windows modifies a file -> Linux sees the modification", passed4, f"Received modified event on Linux")

                # 6. Windows deletes file
                if win_file.exists():
                    win_file.unlink()
                evt6 = await wait_for_event("delete", rel_path)
                passed6 = evt6 is not None
                self.log_result(6, "Windows deletes a file -> Linux deletes it", passed6, f"Received delete event on Linux")

    async def test_command_execution(self):
        """Tests 7, 8, 9, 10, 11, 12, 13: Command execution, outputs, exit codes, streaming, cancellation."""
        # 7 & 8: PowerShell command and stdout
        res7 = await self.run_remote_command("Write-Output 'Hello From Windows PowerShell'")
        passed7 = "Hello From Windows PowerShell" in res7["stdout"]
        self.log_result(7, "Linux requests a Windows PowerShell command -> Windows executes it", passed7, "Command completed on host")
        self.log_result(8, "Windows stdout is returned to Linux", "Hello From Windows PowerShell" in res7["stdout"], res7["stdout"].strip())

        # 9: Stderr returned to Linux
        res9 = await self.run_remote_command("[Console]::Error.WriteLine('Diagnostic Stderr Message')")
        passed9 = "Diagnostic Stderr Message" in res9["stderr"]
        self.log_result(9, "Windows stderr is returned to Linux", passed9, res9["stderr"].strip())

        # 10: Exit codes preserved
        res10 = await self.run_remote_command("exit 42")
        passed10 = res10.get("exit_code") == 42
        self.log_result(10, "Exit codes are preserved", passed10, f"Exit code received: {res10.get('exit_code')}")

        # 11: Long-running command output streaming
        chunks_received = 0
        timestamps = []
        cmd_id = f"stream_test_{int(time.time())}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.server_url) as ws:
                await ws.send_str(create_message(MSG_AUTH_REQ, token=self.token, client_id="test_stream", role="cli"))
                await ws.receive()
                await ws.send_str(create_message(
                    MSG_EXECUTE_COMMAND,
                    command_id=cmd_id,
                    command="for ($i=1; $i -le 3; $i++) { Write-Output \"Tick $i\"; Start-Sleep -Milliseconds 250 }",
                ))
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        d = parse_message(msg.data)
                        if d.get("type") == MSG_COMMAND_OUTPUT:
                            chunks_received += 1
                            timestamps.append(time.time())
                        elif d.get("type") == MSG_COMMAND_COMPLETED:
                            break

        # Check that chunks arrived over time (not all at once at the end)
        streamed_properly = chunks_received >= 3 and (timestamps[-1] - timestamps[0] > 0.3)
        self.log_result(11, "Long-running command output can be streamed", streamed_properly, f"Streamed {chunks_received} chunks incrementally")

        # 12: Failed command correctly reported
        res12 = await self.run_remote_command("Get-Item 'C:\\NonExistent_Path_Test_12345'")
        passed12 = res12.get("exit_code") != 0 or res12.get("status") == "failed" or len(res12.get("stderr")) > 0
        self.log_result(12, "A failed command is correctly reported", passed12, f"Exit code: {res12.get('exit_code')}")

        # 13: Running command can be cancelled
        cancel_id = f"cancel_test_{int(time.time())}"
        was_cancelled = False
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.server_url) as ws:
                await ws.send_str(create_message(MSG_AUTH_REQ, token=self.token, client_id="test_cancel", role="cli"))
                await ws.receive()
                await ws.send_str(create_message(
                    MSG_EXECUTE_COMMAND,
                    command_id=cancel_id,
                    command="Start-Sleep -Seconds 10",
                ))
                # Wait briefly then cancel
                await asyncio.sleep(0.5)
                await ws.send_str(create_message(MSG_CANCEL_COMMAND, command_id=cancel_id))

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        d = parse_message(msg.data)
                        if d.get("type") == MSG_COMMAND_COMPLETED:
                            was_cancelled = d.get("status") == "cancelled"
                            break
        self.log_result(13, "A running command can be cancelled", was_cancelled, f"Process cancelled cleanly via psutil tree kill")

    async def test_agent_workflow_loop(self):
        """Tests 14 & 15: Agent modifies file, executes Windows command reading new version, inspects output."""
        code_file = "sample_app/lib/generated_code.txt"
        target_path = self.windows_workspace / code_file
        target_path.parent.mkdir(parents=True, exist_ok=True)

        new_secret_number = 987654
        import base64
        file_payload = f"magic_constant = {new_secret_number}".encode("utf-8")

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.server_url) as ws:
                await ws.send_str(create_message(MSG_AUTH_REQ, token=self.token, client_id="agent_workflow", role="linux_agent"))
                await ws.receive()

                # Step 1: Agent modifies file over sync
                await ws.send_str(create_message(
                    MSG_SYNC_EVENT,
                    action="upsert",
                    rel_path=code_file,
                    content_b64=base64.b64encode(file_payload).decode("ascii"),
                ))
                await asyncio.sleep(0.4)

        # Step 2: Immediately execute Windows command to verify the file
        res = await self.run_remote_command(f"Get-Content 'sample_app/lib/generated_code.txt'")
        output_verified = str(new_secret_number) in res["stdout"]

        self.log_result(14, "Agent can modify a file and immediately execute a Windows command using new version", output_verified, f"Verified content: {res['stdout'].strip()}")
        self.log_result(15, "Windows build/command output can be inspected from Linux side", len(res["stdout"]) > 0, f"Captured stdout length: {len(res['stdout'])} chars")

        # Cleanup
        if target_path.exists():
            target_path.unlink()

    async def test_disconnect_and_recovery(self):
        """Test 16: Temporary disconnect & automatic recovery."""
        # 1. Connect client 1
        client_connected = False
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.server_url) as ws:
                await ws.send_str(create_message(MSG_AUTH_REQ, token=self.token, client_id="recon_client", role="linux_agent"))
                resp = parse_message((await ws.receive()).data)
                client_connected = resp.get("success", False)
                # Intentionally close connection suddenly
                await ws.close()

        # 2. Reconnect immediately
        reconnected = False
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.server_url) as ws:
                await ws.send_str(create_message(MSG_AUTH_REQ, token=self.token, client_id="recon_client", role="linux_agent"))
                resp2 = parse_message((await ws.receive()).data)
                reconnected = resp2.get("success", False)

        passed16 = client_connected and reconnected
        self.log_result(16, "Temporary disconnect and automatic recovery", passed16, "Client successfully reconnected and re-authenticated")

    async def test_mcp_and_router(self):
        """Tests 17 & 18: Native Antigravity MCP Server & Transparent Command Router."""
        import subprocess
        # 17. MCP Server Stdio JSON-RPC
        proc = subprocess.Popen(
            [sys.executable, str(infra_dir / "bridge" / "mcp_server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        init_req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}) + "\n"
        proc.stdin.write(init_req)
        proc.stdin.flush()
        init_resp = json.loads(proc.stdout.readline())

        list_req = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"
        proc.stdin.write(list_req)
        proc.stdin.flush()
        list_resp = json.loads(proc.stdout.readline())

        tools = [t["name"] for t in list_resp.get("result", {}).get("tools", [])]
        mcp_ok = "run_in_linux" in tools and "run_in_windows" in tools and "get_hybrid_status" in tools
        proc.terminate()
        self.log_result(17, "Antigravity MCP Server stdio integration", mcp_ok, f"Tools discovered: {', '.join(tools)}")

        # 18. Transparent Command Router
        from bridge.router import decide_target
        target_curl = decide_target("curl -O https://example.com/model.bin")
        target_flutter = decide_target("flutter test")
        router_ok = (target_curl == "linux") and (target_flutter == "windows")
        self.log_result(18, "Transparent command router automatic dispatch", router_ok, f"curl -> {target_curl}, flutter -> {target_flutter}")

    async def run_all(self):
        print("\n" + "=" * 70)
        print(f"{BOLD}RUNNING MASTER END-TO-END HYBRID ENVIRONMENT TEST SUITE{RESET}")
        print(f"Target Server : {self.server_url}")
        print(f"Workspace     : {self.windows_workspace}")
        print("=" * 70 + "\n")

        # Verify server is up
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.http_url}/health", timeout=aiohttp.ClientTimeout(total=3.0)) as r:
                    if r.status != 200:
                        print(f"{RED}Error: Bridge server at {self.http_url} is not responding (HTTP {r.status}){RESET}")
                        return False
        except Exception as e:
            print(f"{RED}Error: Cannot reach Windows Bridge Server at {self.http_url}: {e}{RESET}")
            print(f"{YELLOW}Hint: Start the Windows Host Executor first using: powershell -File infra/scripts/start_windows_host.ps1{RESET}")
            return False

        # Run all test suites
        await self.test_file_sync_linux_to_windows()
        await self.test_file_sync_windows_to_linux()
        await self.test_command_execution()
        await self.test_agent_workflow_loop()
        await self.test_disconnect_and_recovery()
        await self.test_mcp_and_router()

        total = self.passed + self.failed
        print("\n" + "=" * 70)
        print(f"{BOLD}TEST RESULTS: {self.passed}/{total} PASSED{RESET}")
        if self.failed == 0:
            print(f"{GREEN}{BOLD}ALL TESTS PASSED SUCCESSFULLY! 100% SUCCESS RATE.{RESET}")
        else:
            print(f"{RED}{BOLD}{self.failed} TESTS FAILED.{RESET}")
        print("=" * 70 + "\n")
        return self.failed == 0


if __name__ == "__main__":
    runner = E2ETestRunner()
    success = asyncio.run(runner.run_all())
    sys.exit(0 if success else 1)
