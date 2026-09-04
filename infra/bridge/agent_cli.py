#!/usr/bin/env python3
"""
RemoteDev Transparent Windows Agent CLI Frontend.
Allows the developer on Windows to send prompts to the remote Linux agent,
see real-time streaming thinking traces and file access events, approve sensitive actions,
and receive live token responses transparently from Windows.
"""

import os
import sys
import time
import json
import asyncio
from pathlib import Path
from typing import Optional
import aiohttp

# Setup paths
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

# ANSI formatting
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
MAGENTA = "\033[95m"
RED = "\033[91m"
BLUE = "\033[94m"


def load_config() -> dict:
    import yaml
    config_path = infra_dir / "config" / "config.yaml"
    if config_path.is_file():
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {}


class AgentCLI:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765, token: str = ""):
        self.ws_url = f"ws://{host}:{port}/ws"
        self.token = token
        self.task_completed_event = asyncio.Event()
        self.active_task_id: Optional[str] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None

    async def handle_approval(self, call_id: str, action_desc: str, tool_name: str, args: dict):
        """Prompts user on Windows to approve or deny an action requested by the remote Linux agent."""
        sys.stdout.write(f"\n{RED}{BOLD}======================================================{RESET}\n")
        sys.stdout.write(f"{RED}{BOLD} [REVIEW REQUIRED - ANTIGRAVITY POLICY]{RESET}\n")
        sys.stdout.write(f" {BOLD}Tool:{RESET} {tool_name}\n")
        sys.stdout.write(f" {BOLD}Action:{RESET} {action_desc}\n")
        if args:
            sys.stdout.write(f" {DIM}Args: {json.dumps(args, indent=2)}{RESET}\n")
        sys.stdout.write(f"{RED}{BOLD}======================================================{RESET}\n")
        sys.stdout.write(f"{YELLOW}Approve execution? [Y/n]: {RESET}")
        sys.stdout.flush()

        loop = asyncio.get_running_loop()
        # Read user response without blocking the event loop
        user_input = await loop.run_in_executor(None, sys.stdin.readline)
        choice = user_input.strip().lower()
        approved = choice in ("", "y", "yes")

        if approved:
            sys.stdout.write(f"{GREEN} [Approved]{RESET} Proceeding with execution...\n\n")
        else:
            sys.stdout.write(f"{RED} [Denied]{RESET} Execution rejected.\n\n")
        sys.stdout.flush()

        if self.ws and not self.ws.closed:
            await self.ws.send_str(create_message(
                MSG_AGENT_APPROVAL_RESP,
                call_id=call_id,
                approved=approved,
                reason="User rejected from Windows" if not approved else "",
            ))

    async def run_prompt(self, prompt: str, model: str = "gemini-2.5-flash"):
        """Sends a single prompt to the remote Linux agent and streams progress."""
        self.task_completed_event.clear()
        task_id = f"task_{int(time.time()*1000)}"
        self.active_task_id = task_id

        async with aiohttp.ClientSession() as session:
            try:
                async with session.ws_connect(self.ws_url, heartbeat=20.0, max_msg_size=64 * 1024 * 1024) as ws:
                    self.ws = ws

                    # 1. Authenticate
                    await ws.send_str(create_message(
                        MSG_AUTH_REQ,
                        token=self.token,
                        client_id="windows-agent-cli",
                        role="agent_frontend",
                    ))

                    auth_msg = await ws.receive()
                    if auth_msg.type != aiohttp.WSMsgType.TEXT:
                        print(f"{RED}Authentication failed.{RESET}")
                        return

                    auth_data = parse_message(auth_msg.data)
                    if not auth_data.get("success"):
                        print(f"{RED}Authentication error: {auth_data.get('message')}{RESET}")
                        return

                    # 2. Dispatch prompt
                    print(f"\n{CYAN}{BOLD}>>> Sending prompt to Linux Agent (Model: {model})...{RESET}")
                    await ws.send_str(create_message(
                        MSG_AGENT_START_TASK,
                        task_id=task_id,
                        prompt=prompt,
                        model=model,
                    ))

                    in_thought = False
                    in_response = False

                    # 3. Stream incoming events
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = parse_message(msg.data)
                            msg_type = data.get("type")

                            if data.get("task_id") and data.get("task_id") != task_id:
                                # Not for this task
                                continue

                            if msg_type == MSG_AGENT_THOUGHT:
                                thought = data.get("thought", "")
                                sys.stdout.write(f"{CYAN}{DIM} [Thinking] {thought}{RESET}\n")
                                sys.stdout.flush()

                            elif msg_type == MSG_AGENT_FILE_ACCESS:
                                action = data.get("action", "access").upper()
                                rel_path = data.get("rel_path", "")
                                color = MAGENTA if action == "WRITE" else BLUE
                                sys.stdout.write(f"{color} [File {action}] {rel_path}{RESET}\n")
                                sys.stdout.flush()

                            elif msg_type == MSG_AGENT_TOOL_CALL:
                                tool = data.get("tool_name", "")
                                sys.stdout.write(f"{YELLOW} [Tool] {tool}{RESET}\n")
                                sys.stdout.flush()

                            elif msg_type == MSG_AGENT_APPROVAL_REQ:
                                call_id = data.get("call_id", "")
                                desc = data.get("action_desc", "")
                                tool_name = data.get("tool_name", "")
                                args = data.get("args", {})
                                await self.handle_approval(call_id, desc, tool_name, args)

                            elif msg_type == MSG_AGENT_TOKEN:
                                token = data.get("token", "")
                                if not in_response:
                                    sys.stdout.write(f"\n{GREEN}{BOLD}--- Agent Response ---{RESET}\n")
                                    in_response = True
                                sys.stdout.write(token)
                                sys.stdout.flush()

                            elif msg_type == MSG_AGENT_TASK_COMPLETED:
                                status = data.get("status", "unknown")
                                summary = data.get("summary", "")
                                if in_response:
                                    sys.stdout.write("\n")
                                if status == "success":
                                    sys.stdout.write(f"\n{GREEN}{BOLD}[OK] Task Completed:{RESET} {summary}\n")
                                elif status == "cancelled":
                                    sys.stdout.write(f"\n{YELLOW}{BOLD}[WARN] Task Cancelled:{RESET} {summary}\n")
                                else:
                                    sys.stdout.write(f"\n{RED}{BOLD}[ERROR] Task Failed:{RESET} {summary}\n")

                                sys.stdout.flush()
                                break

            except aiohttp.ClientConnectorError:
                print(f"{RED}Error: Cannot connect to Windows bridge at {self.ws_url}. Is 'start_windows_host.ps1' running?{RESET}")
            except Exception as e:
                print(f"{RED}Agent error: {e}{RESET}")


async def interactive_mode(cli: AgentCLI, model: str):
    print(f"{CYAN}{BOLD}=================================================================={RESET}")
    print(f"{CYAN}{BOLD} RemoteDev Transparent Agent Terminal (Windows -> Linux) {RESET}")
    print(f"{CYAN} Type your prompt and press Enter. Type 'exit' or 'quit' to quit. {RESET}")
    print(f"{CYAN}{BOLD}=================================================================={RESET}\n")

    loop = asyncio.get_running_loop()
    while True:
        try:
            sys.stdout.write(f"{BOLD}Agent> {RESET}")
            sys.stdout.flush()
            user_input = await loop.run_in_executor(None, sys.stdin.readline)
            prompt = user_input.strip()
            if not prompt:
                continue
            if prompt.lower() in ("exit", "quit", "q"):
                print("Goodbye!")
                break
            await cli.run_prompt(prompt, model=model)
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break


def main():
    import argparse
    cfg = load_config()
    server_cfg = cfg.get("server", {})
    agent_cfg = cfg.get("agent", {})

    parser = argparse.ArgumentParser(description="RemoteDev Windows Transparent Agent CLI")
    parser.add_argument("prompt", nargs="?", default=None, help="Prompt to send to the remote Linux agent")
    parser.add_argument("--host", default="127.0.0.1", help="Windows Bridge Host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=server_cfg.get("port", 8765), help="Windows Bridge Port")
    parser.add_argument("--token", default=server_cfg.get("token", "remotedev-secret-hybrid-token-2026"), help="Bridge auth token")
    parser.add_argument("--model", default=agent_cfg.get("model", "gemini-2.5-flash"), help="Model selection")
    args = parser.parse_args()

    cli = AgentCLI(host=args.host, port=args.port, token=args.token)

    if args.prompt:
        asyncio.run(cli.run_prompt(args.prompt, model=args.model))
    else:
        asyncio.run(interactive_mode(cli, model=args.model))


if __name__ == "__main__":
    main()
