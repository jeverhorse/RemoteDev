"""
Windows Host Executor Service Daemon.
Provides authenticated WebSocket bridge and HTTP status endpoints on the Windows host.
Multiplexes real-time bidirectional file synchronization and remote command execution.
"""

import os
import sys
import time
import yaml
import json
import asyncio
import logging
from pathlib import Path
from typing import Optional, Set, Dict, Any
from aiohttp import web, WSMsgType

# Add parent directory to path to allow direct invocation
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
    MSG_SYNC_MANIFEST_REQ,
    MSG_SYNC_MANIFEST_RESP,
    MSG_SYNC_EVENT,
    MSG_STATUS_REQ,
    MSG_STATUS_RESP,
    MSG_PING,
    MSG_PONG,
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
    MSG_CONFIG_SYNC,
    create_message,
    parse_message,
)
from bridge.security import validate_token
from bridge.diagnostics import DiagnosticsTracker
from bridge.watcher import IgnoreFilter, watch_directory
from bridge.sync_engine import SyncEngine
from bridge.executor import CommandExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("remotedev.service")


class WindowsBridgeServer:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path).resolve()
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        self.host = self.config["server"].get("host", "0.0.0.0")
        self.port = int(self.config["server"].get("port", 8765))
        self.token = self.config["server"].get("token", "")

        raw_ws = self.config["workspace"].get("windows_path", "./workspace")
        self.workspace_path = Path(raw_ws).resolve()
        self.workspace_path.mkdir(parents=True, exist_ok=True)

        self.ignore_filter = IgnoreFilter(self.config["sync"].get("ignore_patterns", []))
        self.diagnostics = DiagnosticsTracker(
            history_limit=self.config.get("diagnostics", {}).get("history_limit", 50)
        )

        self.executor = CommandExecutor(str(self.workspace_path), self.diagnostics)
        self.active_websockets: Set[web.WebSocketResponse] = set()
        self.authenticated_ws: Set[web.WebSocketResponse] = set()

        self.sync_engine = SyncEngine(
            workspace_dir=str(self.workspace_path),
            ignore_filter=self.ignore_filter,
            diagnostics=self.diagnostics,
            send_message_fn=self.broadcast_message,
            debounce_ms=self.config["sync"].get("debounce_ms", 200),
            max_file_size_mb=self.config["sync"].get("max_file_size_mb", 50),
        )

        self.linux_agent_ws: Optional[web.WebSocketResponse] = None
        self.pending_approvals: Dict[str, Dict[str, Any]] = {}
        self.latest_agent_status: Dict[str, Any] = {
            "state": "idle",
            "task_id": None,
            "last_summary": None,
            "connected_linux_agent": False,
        }

        self.app = web.Application()
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_get("/status", self.handle_status)
        self.app.router.add_get("/ws", self.handle_websocket)
        self.app.router.add_post("/agent/prompt", self.handle_agent_prompt)
        self.app.router.add_post("/agent/cancel", self.handle_agent_cancel)
        self.app.router.add_post("/agent/approve", self.handle_agent_approve)
        self.app.router.add_get("/agent/status", self.handle_agent_status)

        self.stop_watcher_event = asyncio.Event()
        self.watcher_task: Optional[asyncio.Task] = None

    def get_antigravity_config(self) -> Dict[str, Any]:
        """Reads workspace rules, MCP configurations, and review policies to synchronize to Linux."""
        agents_dir = root_dir / ".agents"
        rules = []
        rules_dir = agents_dir / "rules"
        if rules_dir.is_dir():
            for rule_file in rules_dir.glob("*.md"):
                try:
                    rules.append(rule_file.read_text(encoding="utf-8", errors="replace"))
                except Exception as e:
                    logger.warning(f"Failed to read rule file {rule_file}: {e}")

        mcp_config = {}
        mcp_file = agents_dir / "mcp_config.json"
        if mcp_file.is_file():
            try:
                mcp_config = json.loads(mcp_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Failed to read mcp_config.json: {e}")

        review_policy = self.config.get("agent", {}).get("review_policy", "request-review")

        return {
            "review_policy": review_policy,
            "rules": rules,
            "mcp_config": mcp_config,
            "model": self.config.get("agent", {}).get("model", "gemini-2.5-flash"),
        }

    async def sync_config_to_linux(self):
        """Sends active Antigravity IDE configuration to Linux agent."""
        if self.linux_agent_ws and not self.linux_agent_ws.closed:
            cfg = self.get_antigravity_config()
            logger.info(f"Syncing Antigravity IDE configuration to Linux agent (review policy: {cfg['review_policy']})...")
            await self.linux_agent_ws.send_str(create_message(
                MSG_CONFIG_SYNC,
                config=cfg,
            ))

    async def handle_agent_prompt(self, request: web.Request) -> web.Response:
        data = await request.json()
        prompt = data.get("prompt", "")
        if not prompt:
            return web.json_response({"error": "Prompt cannot be empty"}, status=400)

        task_id = data.get("task_id", f"task_{int(time.time()*1000)}")
        model = data.get("model", self.config.get("agent", {}).get("model", "gemini-2.5-flash"))
        context = data.get("context", {})

        if not self.linux_agent_ws or self.linux_agent_ws.closed:
            return web.json_response({"error": "No Linux Agent connected to bridge"}, status=503)

        self.latest_agent_status = {
            "state": "running",
            "task_id": task_id,
            "prompt": prompt,
            "start_time": time.time(),
            "connected_linux_agent": True,
        }

        await self.linux_agent_ws.send_str(create_message(
            MSG_AGENT_START_TASK,
            task_id=task_id,
            prompt=prompt,
            model=model,
            context=context,
        ))

        return web.json_response({
            "status": "dispatched",
            "task_id": task_id,
            "prompt": prompt,
        })

    async def handle_agent_cancel(self, request: web.Request) -> web.Response:
        data = await request.json()
        task_id = data.get("task_id", "")
        if self.linux_agent_ws and not self.linux_agent_ws.closed:
            await self.linux_agent_ws.send_str(create_message(
                MSG_AGENT_CANCEL_TASK,
                task_id=task_id,
            ))
        return web.json_response({"status": "cancelled", "task_id": task_id})

    async def handle_agent_approve(self, request: web.Request) -> web.Response:
        data = await request.json()
        call_id = data.get("call_id", "")
        approved = data.get("approved", True)
        reason = data.get("reason", "")
        self.pending_approvals.pop(call_id, None)

        if self.linux_agent_ws and not self.linux_agent_ws.closed:
            await self.linux_agent_ws.send_str(create_message(
                MSG_AGENT_APPROVAL_RESP,
                call_id=call_id,
                approved=approved,
                reason=reason,
            ))
            return web.json_response({"status": "submitted", "call_id": call_id, "approved": approved})
        return web.json_response({"error": "Linux Agent not connected"}, status=503)

    async def handle_agent_status(self, request: web.Request) -> web.Response:
        return web.json_response({
            "agent_status": self.latest_agent_status,
            "pending_approvals": list(self.pending_approvals.values()),
            "connected_linux_agent": bool(self.linux_agent_ws and not self.linux_agent_ws.closed),
        })


    async def broadcast_message(self, raw_msg: str):
        """Sends message to all authenticated connected clients."""
        for ws in list(self.authenticated_ws):
            if not ws.closed:
                try:
                    await ws.send_str(raw_msg)
                except Exception as e:
                    logger.debug(f"Broadcast send failed: {e}")

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "remotedev-bridge-server"})

    async def handle_status(self, request: web.Request) -> web.Response:
        status_data = self.diagnostics.get_status_summary()
        status_data["workspace"] = {
            "windows_path": str(self.workspace_path),
            "files_indexed": len(self.sync_engine.file_index),
        }
        return web.json_response(status_data)

    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=64 * 1024 * 1024)
        await ws.prepare(request)

        peername = request.remote or "unknown"
        logger.info(f"New connection from {peername}")
        self.active_websockets.add(ws)

        async def send_fn(msg: str):
            if not ws.closed:
                await ws.send_str(msg)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = parse_message(msg.data)
                    except ValueError as e:
                        logger.warning(f"Invalid message format from {peername}: {e}")
                        continue

                    msg_type = data.get("type")

                    # Handle Authentication
                    if msg_type == MSG_AUTH_REQ:
                        client_token = data.get("token")
                        if validate_token(self.token, client_token):
                            self.authenticated_ws.add(ws)
                            self.diagnostics.set_connected(True, peername)
                            logger.info(f"Client {peername} authenticated successfully (role: {data.get('role', 'agent')})")
                            await send_fn(create_message(
                                MSG_AUTH_RESP,
                                success=True,
                                message="Authentication successful",
                                server_info={
                                    "platform": "windows",
                                    "workspace": str(self.workspace_path),
                                }
                            ))
                            # Send initial sync manifest and files to client
                            manifest = self.sync_engine.get_manifest()
                            await send_fn(create_message(
                                MSG_SYNC_MANIFEST_RESP,
                                files=manifest,
                            ))
                            if data.get("role") == "linux_agent":
                                self.linux_agent_ws = ws
                                self.latest_agent_status["connected_linux_agent"] = True
                                asyncio.create_task(self.sync_engine.push_all_files(send_fn))
                                asyncio.create_task(self.sync_config_to_linux())
                        else:
                            logger.warning(f"Client {peername} authentication failed: invalid token")
                            await send_fn(create_message(
                                MSG_AUTH_RESP,
                                success=False,
                                message="Authentication failed: invalid token",
                            ))
                            await ws.close()
                            break
                        continue

                    # Require authentication for all other messages
                    if ws not in self.authenticated_ws:
                        logger.warning(f"Unauthenticated message from {peername}. Closing.")
                        await ws.close()
                        break

                    # Dispatch authenticated messages
                    if msg_type == MSG_PING:
                        await send_fn(create_message(MSG_PONG, timestamp=data.get("timestamp")))

                    elif msg_type == MSG_STATUS_REQ:
                        summary = self.diagnostics.get_status_summary()
                        await send_fn(create_message(MSG_STATUS_RESP, data=summary))

                    elif msg_type == MSG_SYNC_MANIFEST_REQ:
                        manifest = self.sync_engine.get_manifest()
                        await send_fn(create_message(MSG_SYNC_MANIFEST_RESP, files=manifest))

                    elif msg_type == MSG_SYNC_EVENT:
                        # Process sync change from remote
                        await self.sync_engine.apply_remote_event(data)

                    elif msg_type == MSG_EXECUTE_COMMAND:
                        cmd_id = data.get("command_id", f"cmd_{int(time.time()*1000)}")
                        cmd_str = data.get("command", "")
                        shell = data.get("shell", "powershell")
                        cwd = data.get("cwd")
                        env_vars = data.get("env")
                        timeout = data.get("timeout", self.config["execution"].get("default_timeout_sec", 120))

                        # Run command concurrently without blocking websocket event loop
                        asyncio.create_task(
                            self.executor.run_command(
                                command_id=cmd_id,
                                command_str=cmd_str,
                                shell=shell,
                                cwd=cwd,
                                env_vars=env_vars,
                                timeout_sec=timeout,
                                send_message_fn=send_fn,
                            )
                        )

                    elif msg_type == MSG_CANCEL_COMMAND:
                        cmd_id = data.get("command_id", "")
                        cancelled = self.executor.cancel_command(cmd_id)
                        logger.info(f"Cancel request for {cmd_id}: {'success' if cancelled else 'not found/done'}")

                    elif msg_type == MSG_AGENT_START_TASK:
                        if self.linux_agent_ws and not self.linux_agent_ws.closed:
                            self.latest_agent_status = {
                                "state": "running",
                                "task_id": data.get("task_id"),
                                "prompt": data.get("prompt"),
                                "start_time": time.time(),
                                "connected_linux_agent": True,
                            }
                            await self.linux_agent_ws.send_str(msg.data)
                        else:
                            await send_fn(create_message(
                                MSG_AGENT_TASK_COMPLETED,
                                task_id=data.get("task_id", ""),
                                status="error",
                                summary="No Linux Agent is currently connected to the Windows bridge",
                            ))

                    elif msg_type == MSG_AGENT_CANCEL_TASK:
                        if self.linux_agent_ws and not self.linux_agent_ws.closed:
                            await self.linux_agent_ws.send_str(msg.data)

                    elif msg_type == MSG_AGENT_APPROVAL_RESP:
                        call_id = data.get("call_id", "")
                        self.pending_approvals.pop(call_id, None)
                        if self.linux_agent_ws and not self.linux_agent_ws.closed:
                            await self.linux_agent_ws.send_str(msg.data)

                    elif msg_type in (
                        MSG_AGENT_THOUGHT,
                        MSG_AGENT_TOOL_CALL,
                        MSG_AGENT_TOOL_RESULT,
                        MSG_AGENT_FILE_ACCESS,
                        MSG_AGENT_APPROVAL_REQ,
                        MSG_AGENT_TOKEN,
                        MSG_AGENT_TASK_COMPLETED,
                    ):
                        if msg_type == MSG_AGENT_APPROVAL_REQ:
                            self.pending_approvals[data.get("call_id")] = data
                        elif msg_type == MSG_AGENT_TASK_COMPLETED:
                            self.latest_agent_status["state"] = data.get("status", "completed")
                            self.latest_agent_status["last_summary"] = data.get("summary")

                        # Broadcast agent stream events to all other connected clients (CLI, UI, MCP)
                        for client_ws in list(self.authenticated_ws):
                            if client_ws != ws and not client_ws.closed:
                                try:
                                    await client_ws.send_str(msg.data)
                                except Exception as e:
                                    logger.debug(f"Failed to forward agent event: {e}")

                elif msg.type == WSMsgType.ERROR:
                    logger.error(f"WebSocket error on connection {peername}: {ws.exception()}")

        finally:
            self.active_websockets.discard(ws)
            self.authenticated_ws.discard(ws)
            if ws == self.linux_agent_ws:
                self.linux_agent_ws = None
                self.latest_agent_status["connected_linux_agent"] = False
            if not self.authenticated_ws:
                self.diagnostics.set_connected(False)
            logger.info(f"Connection closed for {peername}")


        return ws

    async def _start_watcher(self):
        """Watches Windows workspace and feeds changes to sync_engine."""
        try:
            async for action, rel_path in watch_directory(
                str(self.workspace_path),
                self.ignore_filter,
                self.stop_watcher_event,
            ):
                self.sync_engine.queue_local_change(action, rel_path)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Watcher error: {e}")

    async def start(self):
        """Starts HTTP/WebSocket server and sync engine."""
        self.sync_engine.start()
        self.watcher_task = asyncio.create_task(self._start_watcher())

        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        logger.info("=" * 60)
        logger.info(f"RemoteDev Windows Host Executor running on http://{self.host}:{self.port}")
        logger.info(f"Workspace Directory: {self.workspace_path}")
        logger.info("=" * 60)

    async def stop(self):
        logger.info("Stopping Windows Host Executor...")
        self.stop_watcher_event.set()
        if self.watcher_task:
            self.watcher_task.cancel()
        await self.sync_engine.stop()
        for ws in list(self.active_websockets):
            await ws.close()
        logger.info("Windows Host Executor stopped.")


async def main():
    import argparse
    import time
    parser = argparse.ArgumentParser(description="RemoteDev Windows Host Executor Service")
    parser.add_argument("--config", default=str(infra_dir / "config" / "config.yaml"), help="Path to config.yaml")
    args = parser.parse_args()

    server = WindowsBridgeServer(args.config)
    await server.start()

    # Run forever until interrupted
    stop_signal = asyncio.Event()
    try:
        await stop_signal.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await server.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
