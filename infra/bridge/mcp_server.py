#!/usr/bin/env python3
"""
Model Context Protocol (MCP) Server for RemoteDev Hybrid Environment.
Provides native Antigravity IDE tool integration over stdio (JSON-RPC 2.0).

Tools exposed:
- `run_in_linux`: Executes arbitrary commands in the high-speed Linux environment.
- `run_in_windows`: Executes native commands on the Windows host.
- `get_hybrid_status`: Reports sync health, connection status, and active tasks.
"""

import os
import sys
import json
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional

# Set up paths
infra_dir = Path(__file__).resolve().parent.parent
root_dir = infra_dir.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
if str(infra_dir) not in sys.path:
    sys.path.insert(0, str(infra_dir))

CONFIG_PATH = infra_dir / "config" / "config.yaml"


def load_config() -> dict:
    import yaml
    if CONFIG_PATH.is_file():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    return {}


TOOLS = [
    {
        "name": "run_in_linux",
        "description": (
            "Executes a bash command in the high-speed remote Linux environment (Docker/Colab). "
            "Use for fast dependency downloads, git cloning, model fetching, and Linux-specific toolchains. "
            "File changes in the Linux workspace automatically synchronize to the Windows workspace."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command line string to execute in the Linux environment."
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional working directory inside /workspace/project."
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "run_in_windows",
        "description": (
            "Executes a command on the Windows host machine (via PowerShell or CMD). "
            "Use for native Flutter builds, Dart commands, Android emulators, and local tests. "
            "Executes in the local Windows workspace."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command line string to execute on Windows."
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional working directory relative to the Windows workspace."
                },
                "shell": {
                    "type": "string",
                    "enum": ["powershell", "cmd"],
                    "default": "powershell",
                    "description": "Shell to use for execution."
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "get_hybrid_status",
        "description": (
            "Checks the status of the RemoteDev Hybrid Development Environment. "
            "Returns synchronization health, connected peers, and recent command history."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "delegate_to_remote_agent",
        "description": (
            "Delegates an agentic coding or refactoring prompt to the remote Linux agent running in Google Colab / Linux container. "
            "The Linux agent executes with direct unthrottled Google AI access (bypassing Windows ISP restrictions). "
            "Streams reasoning and file modifications directly into the workspace."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The coding task or prompt to delegate to the remote Linux agent."
                },
                "model": {
                    "type": "string",
                    "description": "Optional Gemini model name (e.g. gemini-2.5-flash, gemini-2.5-pro)."
                }
            },
            "required": ["prompt"]
        }
    }
]



def execute_in_linux_container(command: str, cwd: Optional[str] = None) -> Dict[str, Any]:
    """Executes a command inside the remotedev-linux-agent container."""
    workdir = f"/workspace/project/{cwd.strip('/')}" if cwd else "/workspace/project"
    cmd = [
        "docker", "exec",
        "-w", workdir,
        "remotedev-linux-agent",
        "bash", "-c", command
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": 124, "stdout": "", "stderr": "Command timed out in Linux container"}
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": f"Docker execution error: {e}"}


def execute_on_windows_host(command: str, cwd: Optional[str] = None, shell: str = "powershell") -> Dict[str, Any]:
    """Executes a command on the Windows host workspace."""
    cfg = load_config()
    win_ws = Path(cfg.get("workspace", {}).get("windows_path", str(root_dir / "workspace"))).resolve()
    target_cwd = (win_ws / cwd).resolve() if cwd else win_ws

    if shell == "cmd":
        args = ["cmd.exe", "/c", command]
    else:
        args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command]

    try:
        proc = subprocess.run(
            args,
            cwd=str(target_cwd),
            capture_output=True,
            text=True,
            timeout=180,
        )
        return {
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"exit_code": 124, "stdout": "", "stderr": "Command timed out on Windows host"}
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": f"Host execution error: {e}"}


def get_bridge_status() -> Dict[str, Any]:
    """Queries the Windows bridge daemon /status endpoint."""
    import urllib.request
    try:
        req = urllib.request.urlopen("http://127.0.0.1:8765/status", timeout=2.0)
        if req.status == 200:
            return json.loads(req.read().decode("utf-8"))
    except Exception as e:
        return {"status": "offline", "error": str(e)}
    return {"status": "unknown"}


def delegate_to_remote_agent(prompt: str, model: Optional[str] = None) -> Dict[str, Any]:
    """Dispatches prompt to the Windows bridge server's /agent/prompt endpoint."""
    import urllib.request
    cfg = load_config()
    model_name = model or cfg.get("agent", {}).get("model", "gemini-2.5-flash")
    payload = json.dumps({"prompt": prompt, "model": model_name}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8765/agent/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            return {
                "success": True,
                "task_id": res_data.get("task_id"),
                "message": f"Prompt successfully delegated to Linux agent (Task ID: {res_data.get('task_id')}). Model: {model_name}. Files will synchronize automatically.",
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to dispatch to Windows bridge server: {e}. Ensure start_windows_host.ps1 is running.",
        }



def handle_rpc(request: dict) -> Optional[dict]:
    req_id = request.get("id")
    method = request.get("method")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "remotedev-hybrid-bridge",
                    "version": "1.0.0"
                }
            }
        }

    elif method == "notifications/initialized":
        return None

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": TOOLS
            }
        }

    elif method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if tool_name == "run_in_linux":
            res = execute_in_linux_container(arguments.get("command", ""), arguments.get("cwd"))
            text = f"Exit Code: {res['exit_code']}\n"
            if res["stdout"]:
                text += f"STDOUT:\n{res['stdout']}\n"
            if res["stderr"]:
                text += f"STDERR:\n{res['stderr']}\n"
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text.strip()}],
                    "isError": res["exit_code"] != 0,
                }
            }

        elif tool_name == "run_in_windows":
            res = execute_on_windows_host(
                arguments.get("command", ""),
                arguments.get("cwd"),
                arguments.get("shell", "powershell"),
            )
            text = f"Exit Code: {res['exit_code']}\n"
            if res["stdout"]:
                text += f"STDOUT:\n{res['stdout']}\n"
            if res["stderr"]:
                text += f"STDERR:\n{res['stderr']}\n"
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text.strip()}],
                    "isError": res["exit_code"] != 0,
                }
            }

        elif tool_name == "get_hybrid_status":
            status_data = get_bridge_status()
            text = json.dumps(status_data, indent=2)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                }
            }

        elif tool_name == "delegate_to_remote_agent":
            res = delegate_to_remote_agent(
                arguments.get("prompt", ""),
                arguments.get("model"),
            )
            text = res.get("message") if res.get("success") else f"Error: {res.get('error')}"
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": not res.get("success"),
                }
            }

        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {

                    "code": -32601,
                    "message": f"Method/Tool '{tool_name}' not found",
                }
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": -32601,
            "message": f"Method '{method}' not recognized",
        }
    }


def main():
    """Stdio loop for MCP communication with Antigravity IDE."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_rpc(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
        except Exception as e:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {e}"}
            }
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
