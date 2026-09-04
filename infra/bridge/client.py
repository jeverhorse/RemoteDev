"""
Linux Agent Bridge Client Daemon.
Runs inside the Linux container (or future Google Colab), maintains persistent
authenticated WebSocket connection to the Windows Host Executor, and handles
continuous bidirectional workspace file synchronization.
"""

import os
import sys
import yaml
import time
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any
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
    MSG_SYNC_MANIFEST_REQ,
    MSG_SYNC_MANIFEST_RESP,
    MSG_SYNC_EVENT,
    MSG_PING,
    MSG_PONG,
    MSG_EXECUTE_COMMAND,
    MSG_COMMAND_OUTPUT,
    MSG_COMMAND_COMPLETED,
    MSG_AGENT_START_TASK,
    MSG_AGENT_CANCEL_TASK,
    MSG_AGENT_APPROVAL_RESP,
    MSG_CONFIG_SYNC,
    create_message,
    parse_message,
)
from bridge.diagnostics import DiagnosticsTracker
from bridge.watcher import IgnoreFilter, watch_directory
from bridge.sync_engine import SyncEngine
from bridge.agent_runner import LinuxAgentRunner


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("remotedev.client")


class LinuxBridgeClient:
    def __init__(self, config_path: str, server_url: Optional[str] = None):
        self.config_path = Path(config_path).resolve()
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        host = self.config["server"].get("host", "host.docker.internal")
        if host == "0.0.0.0":
            host = os.environ.get("WINDOWS_HOST", "host.docker.internal")
        port = int(self.config["server"].get("port", 8765))
        self.server_url = server_url or f"ws://{host}:{port}/ws"
        self.token = self.config["server"].get("token", "")

        # Workspace directory in Linux
        raw_ws = os.environ.get(
            "LINUX_WORKSPACE",
            self.config["workspace"].get("linux_path", "/workspace/project"),
        )
        self.workspace_path = Path(raw_ws).resolve()
        self.workspace_path.mkdir(parents=True, exist_ok=True)

        self.ignore_filter = IgnoreFilter(self.config["sync"].get("ignore_patterns", []))
        self.diagnostics = DiagnosticsTracker(
            history_limit=self.config.get("diagnostics", {}).get("history_limit", 50)
        )

        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.sync_engine: Optional[SyncEngine] = None
        self.agent_runner: Optional[LinuxAgentRunner] = None
        self.pending_windows_commands: Dict[str, Dict[str, Any]] = {}
        self.stop_event = asyncio.Event()
        self.watcher_task: Optional[asyncio.Task] = None
        self.reconnect_delay = 2.0

    async def execute_windows_command(self, command: str, cwd: Optional[str] = None, shell: str = "powershell") -> Dict[str, Any]:
        """Sends command to Windows host executor and waits for completion."""
        cmd_id = f"cmd_agent_{int(time.time()*1000)}"
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_windows_commands[cmd_id] = {
            "future": future,
            "stdout": "",
            "stderr": "",
        }

        await self.send_message(create_message(
            MSG_EXECUTE_COMMAND,
            command_id=cmd_id,
            command=command,
            cwd=cwd,
            shell=shell,
        ))

        try:
            res = await asyncio.wait_for(future, timeout=180.0)
            return res
        except asyncio.TimeoutError:
            return {"exit_code": 124, "stdout": "", "stderr": "Command timed out on Windows host"}
        finally:
            self.pending_windows_commands.pop(cmd_id, None)

    async def send_message(self, raw_msg: str):
        """Callback for sync_engine and agent_runner to send message over WebSocket."""
        if self.ws and not self.ws.closed:
            await self.ws.send_str(raw_msg)
        else:
            logger.warning("Attempted to send message while WebSocket is disconnected")


    async def _start_watcher(self):
        """Watches Linux workspace and feeds changes to sync_engine."""
        try:
            async for action, rel_path in watch_directory(
                str(self.workspace_path),
                self.ignore_filter,
                self.stop_event,
            ):
                if self.sync_engine:
                    self.sync_engine.queue_local_change(action, rel_path)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Watcher error: {e}")

    async def _reconcile_initial_manifest(self, server_manifest: Dict[str, Dict[str, Any]]):
        """Compares local manifest with Windows server manifest and applies initial differences."""
        logger.info(f"Reconciling initial manifest ({len(server_manifest)} files on Windows host)...")
        if not self.sync_engine:
            return

        local_manifest = self.sync_engine.get_manifest()

        # Files on server that are missing or different locally
        for rel_path, remote_meta in server_manifest.items():
            if self.ignore_filter.is_ignored(rel_path):
                continue

            local_meta = local_manifest.get(rel_path)
            if not local_meta or local_meta["hash"] != remote_meta["hash"]:
                # Request or pull file from server
                pass

        # Files locally that are missing on server: queue upsert to server
        for rel_path, local_meta in local_manifest.items():
            if self.ignore_filter.is_ignored(rel_path):
                continue
            if rel_path not in server_manifest:
                self.sync_engine.queue_local_change("upsert", rel_path)

    async def run(self):
        """Main client loop with automatic reconnection and backoff."""
        self.sync_engine = SyncEngine(
            workspace_dir=str(self.workspace_path),
            ignore_filter=self.ignore_filter,
            diagnostics=self.diagnostics,
            send_message_fn=self.send_message,
            debounce_ms=self.config["sync"].get("debounce_ms", 200),
            max_file_size_mb=self.config["sync"].get("max_file_size_mb", 50),
        )
        default_policy = self.config.get("agent", {}).get("review_policy", "request-review")
        self.agent_runner = LinuxAgentRunner(
            workspace_dir=str(self.workspace_path),
            send_message_fn=self.send_message,
            execute_windows_cmd_fn=self.execute_windows_command,
            default_review_policy=default_policy,
        )
        self.sync_engine.start()
        self.watcher_task = asyncio.create_task(self._start_watcher())

        logger.info("=" * 60)
        logger.info(f"RemoteDev Linux Bridge Client started")
        logger.info(f"Connecting to: {self.server_url}")
        logger.info(f"Local Workspace: {self.workspace_path}")
        logger.info("=" * 60)

        async with aiohttp.ClientSession() as session:
            while not self.stop_event.is_set():
                try:
                    logger.info(f"Connecting to Windows bridge at {self.server_url}...")
                    async with session.ws_connect(
                        self.server_url,
                        heartbeat=20.0,
                        max_msg_size=64 * 1024 * 1024,
                    ) as ws:
                        self.ws = ws
                        logger.info("Connected to WebSocket. Authenticating...")

                        # 1. Authenticate
                        await ws.send_str(create_message(
                            MSG_AUTH_REQ,
                            token=self.token,
                            client_id="linux-agent-sync-daemon",
                            role="linux_agent",
                        ))

                        auth_msg = await ws.receive()
                        if auth_msg.type != aiohttp.WSMsgType.TEXT:
                            logger.error("Failed to receive authentication response")
                            await ws.close()
                            await asyncio.sleep(self.reconnect_delay)
                            continue

                        auth_data = parse_message(auth_msg.data)
                        if not auth_data.get("success"):
                            logger.error(f"Authentication failed: {auth_data.get('message')}")
                            await ws.close()
                            await asyncio.sleep(5.0)
                            continue

                        self.diagnostics.set_connected(True, self.server_url)
                        logger.info("Authentication successful! Requesting sync manifest...")

                        # 2. Request initial sync manifest
                        await ws.send_str(create_message(MSG_SYNC_MANIFEST_REQ))

                        # 3. Message processing loop
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = parse_message(msg.data)
                                except ValueError as ve:
                                    logger.warning(f"Error parsing message: {ve}")
                                    continue

                                msg_type = data.get("type")
                                if msg_type == MSG_PONG:
                                    continue

                                elif msg_type == MSG_SYNC_MANIFEST_RESP:
                                    files = data.get("files", {})
                                    await self._reconcile_initial_manifest(files)

                                elif msg_type == MSG_SYNC_EVENT:
                                    await self.sync_engine.apply_remote_event(data)

                                elif msg_type == MSG_AGENT_START_TASK:
                                    task_id = data.get("task_id", f"task_{int(time.time()*1000)}")
                                    prompt = data.get("prompt", "")
                                    model = data.get("model", "gemini-2.5-flash")
                                    context = data.get("context")
                                    if self.agent_runner:
                                        self.agent_runner.start_task(task_id, prompt, model, context)

                                elif msg_type == MSG_AGENT_CANCEL_TASK:
                                    task_id = data.get("task_id", "")
                                    if self.agent_runner:
                                        self.agent_runner.cancel_task(task_id)

                                elif msg_type == MSG_AGENT_APPROVAL_RESP:
                                    call_id = data.get("call_id", "")
                                    approved = data.get("approved", False)
                                    reason = data.get("reason", "")
                                    if self.agent_runner:
                                        self.agent_runner.handle_approval_response(call_id, approved, reason)

                                elif msg_type == MSG_CONFIG_SYNC:
                                    config_data = data.get("config", {})
                                    if self.agent_runner:
                                        self.agent_runner.update_config(config_data)

                                elif msg_type == MSG_COMMAND_OUTPUT:
                                    cmd_id = data.get("command_id")
                                    if cmd_id in self.pending_windows_commands:
                                        stream = data.get("stream", "stdout")
                                        chunk = data.get("chunk", "")
                                        self.pending_windows_commands[cmd_id][stream] += chunk

                                elif msg_type == MSG_COMMAND_COMPLETED:
                                    cmd_id = data.get("command_id")
                                    if cmd_id in self.pending_windows_commands:
                                        entry = self.pending_windows_commands[cmd_id]
                                        res = {
                                            "exit_code": data.get("exit_code", 0),
                                            "stdout": entry["stdout"],
                                            "stderr": entry["stderr"],
                                        }
                                        if not entry["future"].done():
                                            entry["future"].set_result(res)

                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                logger.warning("WebSocket closed or encountered error")
                                break


                except (aiohttp.ClientConnectorError, aiohttp.WSServerHandshakeError) as ce:
                    logger.warning(f"Connection to {self.server_url} failed: {ce}. Retrying in {self.reconnect_delay}s...")
                except Exception as e:
                    logger.error(f"Unexpected client exception: {e}")

                self.diagnostics.set_connected(False)
                self.ws = None
                await asyncio.sleep(self.reconnect_delay)

    async def stop(self):
        self.stop_event.set()
        if self.watcher_task:
            self.watcher_task.cancel()
        if self.agent_runner:
            for task_id in list(self.agent_runner.active_tasks.keys()):
                self.agent_runner.cancel_task(task_id)
        if self.sync_engine:
            await self.sync_engine.stop()
        if self.ws and not self.ws.closed:
            await self.ws.close()



async def main():
    import argparse
    parser = argparse.ArgumentParser(description="RemoteDev Linux Bridge Client")
    parser.add_argument("--config", default=str(infra_dir / "config" / "config.yaml"), help="Path to config.yaml")
    parser.add_argument("--server", default=None, help="Explicit server WebSocket URL")
    args = parser.parse_args()

    client = LinuxBridgeClient(args.config, args.server)
    try:
        await client.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await client.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
