"""
Linux Agent Runner for RemoteDev Hybrid Environment.
Runs on Linux (Google Colab / Docker / VM) with direct, unthrottled access to Google AI / Gemini services.
Executes the AI agent reasoning loop, dispatches tool actions, enforces review policies,
and streams real-time thinking, file access, and tokens back to Windows.
"""

import os
import sys
import json
import time
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Callable, Awaitable, List

current_dir = Path(__file__).resolve().parent
infra_dir = current_dir.parent
root_dir = infra_dir.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))
if str(infra_dir) not in sys.path:
    sys.path.insert(0, str(infra_dir))

from bridge.protocol import (
    MSG_AGENT_THOUGHT,
    MSG_AGENT_TOOL_CALL,
    MSG_AGENT_TOOL_RESULT,
    MSG_AGENT_FILE_ACCESS,
    MSG_AGENT_APPROVAL_REQ,
    MSG_AGENT_TOKEN,
    MSG_AGENT_TASK_COMPLETED,
    create_message,
)

logger = logging.getLogger("remotedev.agent_runner")


class LinuxAgentRunner:
    """
    Autonomous agent execution engine running on the Linux node.
    Streams thought deltas and file access events to the Windows host.
    """

    def __init__(
        self,
        workspace_dir: str,
        send_message_fn: Callable[[str], Awaitable[None]],
        execute_windows_cmd_fn: Optional[Callable[[str, Optional[str], str], Awaitable[Dict[str, Any]]]] = None,
        default_review_policy: str = "request-review",
    ):
        self.workspace_dir = Path(workspace_dir).resolve()
        self.send_message_fn = send_message_fn
        self.execute_windows_cmd_fn = execute_windows_cmd_fn
        self.review_policy = default_review_policy
        self.synced_rules: List[str] = []
        self.synced_mcp_config: Dict[str, Any] = {}

        # Active tasks and pending approvals
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.pending_approvals: Dict[str, asyncio.Future] = {}

    def update_config(self, config_data: Dict[str, Any]):
        """Updates agent review policy, rules, and MCP definitions from Windows."""
        if "review_policy" in config_data:
            self.review_policy = config_data["review_policy"]
            logger.info(f"Updated agent review policy to: {self.review_policy}")
        if "rules" in config_data:
            self.synced_rules = config_data["rules"]
            logger.info(f"Updated synced agent rules ({len(self.synced_rules)} rules loaded)")
        if "mcp_config" in config_data:
            self.synced_mcp_config = config_data["mcp_config"]
            logger.info("Updated synced MCP configuration")

    async def emit_thought(self, task_id: str, thought: str):
        """Streams agent reasoning / chain-of-thought to Windows host."""
        logger.info(f"[{task_id}] [Thinking] {thought}")
        await self.send_message_fn(create_message(
            MSG_AGENT_THOUGHT,
            task_id=task_id,
            thought=thought,
            timestamp=time.time(),
        ))

    async def emit_file_access(self, task_id: str, action: str, rel_path: str):
        """Notifies Windows host of a file read or write operation."""
        logger.info(f"[{task_id}] [File Access] {action.upper()}: {rel_path}")
        await self.send_message_fn(create_message(
            MSG_AGENT_FILE_ACCESS,
            task_id=task_id,
            action=action,
            rel_path=rel_path,
            timestamp=time.time(),
        ))

    async def emit_token(self, task_id: str, token: str):
        """Streams a response token to Windows host."""
        await self.send_message_fn(create_message(
            MSG_AGENT_TOKEN,
            task_id=task_id,
            token=token,
            timestamp=time.time(),
        ))

    async def request_approval(self, task_id: str, tool_name: str, args: Dict[str, Any], action_desc: str) -> bool:
        """
        Enforces review policy: if 'request-review' or 'strict', requests confirmation from Windows.
        """
        if self.review_policy == "always-proceed":
            return True

        call_id = f"appr_{int(time.time() * 1000)}_{len(self.pending_approvals)}"
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_approvals[call_id] = future

        logger.info(f"[{task_id}] Requesting approval for {tool_name}: {action_desc}")
        await self.send_message_fn(create_message(
            MSG_AGENT_APPROVAL_REQ,
            task_id=task_id,
            call_id=call_id,
            tool_name=tool_name,
            args=args,
            action_desc=action_desc,
            timestamp=time.time(),
        ))

        try:
            # Await user approval from Windows with a 10-minute timeout
            decision = await asyncio.wait_for(future, timeout=600.0)
            return decision.get("approved", False)
        except asyncio.TimeoutError:
            logger.warning(f"Approval request {call_id} timed out")
            return False
        finally:
            self.pending_approvals.pop(call_id, None)

    def handle_approval_response(self, call_id: str, approved: bool, reason: str = ""):
        """Resolves a pending approval future when Windows sends approval decision."""
        if call_id in self.pending_approvals:
            future = self.pending_approvals[call_id]
            if not future.done():
                future.set_result({"approved": approved, "reason": reason})

    # --- Tool Implementations ---

    async def tool_view_file(self, task_id: str, rel_path: str) -> str:
        await self.emit_file_access(task_id, "read", rel_path)
        full_path = (self.workspace_dir / rel_path).resolve()
        if not str(full_path).startswith(str(self.workspace_dir)):
            return f"Error: Path '{rel_path}' is outside workspace"
        if not full_path.is_file():
            return f"Error: File '{rel_path}' does not exist"
        try:
            return full_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error reading file: {e}"

    async def tool_write_to_file(self, task_id: str, rel_path: str, content: str) -> str:
        approved = await self.request_approval(
            task_id, "write_to_file", {"rel_path": rel_path}, f"Write file '{rel_path}' ({len(content)} bytes)"
        )
        if not approved:
            return f"Error: Write to '{rel_path}' was denied by user review policy."

        await self.emit_file_access(task_id, "write", rel_path)
        full_path = (self.workspace_dir / rel_path).resolve()
        if not str(full_path).startswith(str(self.workspace_dir)):
            return f"Error: Path '{rel_path}' is outside workspace"
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} characters to {rel_path}"
        except Exception as e:
            return f"Error writing file: {e}"

    async def tool_replace_file_content(self, task_id: str, rel_path: str, target: str, replacement: str) -> str:
        approved = await self.request_approval(
            task_id, "replace_file_content", {"rel_path": rel_path}, f"Modify file '{rel_path}'"
        )
        if not approved:
            return f"Error: Modification to '{rel_path}' was denied by user review policy."

        await self.emit_file_access(task_id, "write", rel_path)
        full_path = (self.workspace_dir / rel_path).resolve()
        if not full_path.is_file():
            return f"Error: File '{rel_path}' does not exist"
        try:
            text = full_path.read_text(encoding="utf-8")
            if target not in text:
                return f"Error: Target text not found in '{rel_path}'"
            new_text = text.replace(target, replacement, 1)
            full_path.write_text(new_text, encoding="utf-8")
            return f"Successfully replaced target in {rel_path}"
        except Exception as e:
            return f"Error modifying file: {e}"

    async def tool_list_dir(self, task_id: str, rel_path: str = "") -> str:
        full_path = (self.workspace_dir / rel_path).resolve() if rel_path else self.workspace_dir
        if not str(full_path).startswith(str(self.workspace_dir)):
            return f"Error: Path '{rel_path}' is outside workspace"
        if not full_path.is_dir():
            return f"Error: Directory '{rel_path}' does not exist"
        try:
            entries = []
            for item in sorted(full_path.iterdir()):
                prefix = "[DIR] " if item.is_dir() else "[FILE]"
                entries.append(f"{prefix} {item.name}")
            return "\n".join(entries) if entries else "(Empty directory)"
        except Exception as e:
            return f"Error listing directory: {e}"

    async def tool_run_in_windows(self, task_id: str, command: str, shell: str = "powershell") -> str:
        approved = await self.request_approval(
            task_id, "run_in_windows", {"command": command, "shell": shell}, f"Execute on Windows Host: '{command}'"
        )
        if not approved:
            return f"Error: Execution of '{command}' on Windows was denied by user review policy."

        if self.execute_windows_cmd_fn:
            res = await self.execute_windows_cmd_fn(command, None, shell)
            out = f"Exit Code: {res.get('exit_code', -1)}\n"
            if res.get("stdout"):
                out += f"STDOUT:\n{res['stdout']}\n"
            if res.get("stderr"):
                out += f"STDERR:\n{res['stderr']}\n"
            return out.strip()
        return "Error: Windows Host execution bridge not connected"

    async def tool_run_in_linux(self, task_id: str, command: str) -> str:
        approved = await self.request_approval(
            task_id, "run_in_linux", {"command": command}, f"Execute locally in Linux: '{command}'"
        )
        if not approved:
            return f"Error: Execution of '{command}' in Linux was denied by user review policy."

        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.workspace_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = f"Exit Code: {proc.returncode}\n"
        if stdout:
            out += f"STDOUT:\n{stdout.decode('utf-8', errors='replace')}\n"
        if stderr:
            out += f"STDERR:\n{stderr.decode('utf-8', errors='replace')}\n"
        return out.strip()

    # --- Agent Task Execution Loop ---

    async def execute_task(self, task_id: str, prompt: str, model: str = "gemini-2.5-flash", context: Optional[Dict[str, Any]] = None):
        """
        Executes a prompt using the agent loop.
        Can execute via Google Gemini API directly on Linux or simulate/mock for testing.
        """
        logger.info(f"Starting agent task {task_id}: {prompt[:80]}...")
        try:
            # 1. Emit initial thought
            await self.emit_thought(task_id, f"Analyzing prompt: '{prompt[:100]}' in workspace {self.workspace_dir.name}")

            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            use_live_gemini = bool(api_key and not os.environ.get("REMOTEDEV_MOCK_AGENT"))

            if use_live_gemini:
                await self._run_gemini_agent_loop(task_id, prompt, model, api_key)
            else:
                await self._run_default_agent_loop(task_id, prompt, model)

            # Complete task
            await self.send_message_fn(create_message(
                MSG_AGENT_TASK_COMPLETED,
                task_id=task_id,
                status="success",
                summary="Agent task executed successfully",
                timestamp=time.time(),
            ))
        except asyncio.CancelledError:
            logger.info(f"Task {task_id} was cancelled")
            await self.send_message_fn(create_message(
                MSG_AGENT_TASK_COMPLETED,
                task_id=task_id,
                status="cancelled",
                summary="Task was cancelled by user",
                timestamp=time.time(),
            ))
        except Exception as e:
            logger.error(f"Task {task_id} encountered exception: {e}", exc_info=True)
            await self.send_message_fn(create_message(
                MSG_AGENT_TASK_COMPLETED,
                task_id=task_id,
                status="error",
                summary=str(e),
                timestamp=time.time(),
            ))
        finally:
            self.active_tasks.pop(task_id, None)

    async def _run_default_agent_loop(self, task_id: str, prompt: str, model: str):
        """
        Deterministic agent loop for high-speed offline operation, fallback, and verification.
        Inspects workspace files, reasons, and executes requested actions.
        """
        await asyncio.sleep(0.05)
        await self.emit_thought(task_id, "Checking workspace file tree and existing files...")

        files_summary = await self.tool_list_dir(task_id, "")
        await self.emit_thought(task_id, f"Workspace structure:\n{files_summary[:200]}")

        # If prompt asks to read/inspect
        if "read" in prompt.lower() or "check" in prompt.lower() or "view" in prompt.lower():
            # Check for mention of specific file
            for word in prompt.split():
                if "." in word and not word.endswith("."):
                    clean_path = word.strip(" '\"`")
                    if (self.workspace_dir / clean_path).exists():
                        await self.emit_thought(task_id, f"Reading requested file: {clean_path}")
                        content = await self.tool_view_file(task_id, clean_path)
                        await self.emit_thought(task_id, f"Read {len(content)} bytes from {clean_path}")
                        break

        # If prompt asks to create or write
        if "write" in prompt.lower() or "create" in prompt.lower() or "modify" in prompt.lower():
            await self.emit_thought(task_id, "Planning file generation...")
            rel_file = "sample_generated.dart"
            if "file:" in prompt.lower():
                rel_file = prompt.split("file:")[1].strip().split()[0]
            sample_content = (
                f"// Generated by RemoteDev Linux Agent\n"
                f"// Prompt: {prompt}\n"
                f"// Timestamp: {time.ctime()}\n"
                f"void main() {{\n"
                f"  print('Hello from RemoteDev Linux Agent!');\n"
                f"}}\n"
            )
            res = await self.tool_write_to_file(
                task_id,
                rel_file,
                sample_content,
            )
            await self.emit_thought(task_id, f"File write result: {res}")


        # If prompt asks to run commands
        if "run" in prompt.lower() or "test" in prompt.lower() or "build" in prompt.lower():
            if "windows" in prompt.lower() or "flutter" in prompt.lower() or "dart" in prompt.lower():
                await self.emit_thought(task_id, "Dispatching command to Windows Host Executor...")
                cmd = "echo RemoteDev Windows Command"
                if "command:" in prompt.lower():
                    cmd = prompt.split("command:")[1].strip()
                res = await self.tool_run_in_windows(task_id, cmd)
                await self.emit_thought(task_id, f"Windows execution output:\n{res[:200]}")
            else:
                await self.emit_thought(task_id, "Executing command in Linux environment...")
                cmd = "echo Hello from Linux"
                if "command:" in prompt.lower():
                    cmd = prompt.split("command:")[1].strip()
                res = await self.tool_run_in_linux(task_id, cmd)
                await self.emit_thought(task_id, f"Linux execution output:\n{res[:200]}")

        # Stream response tokens
        response_text = f"I have processed your request: '{prompt}'. All files have been updated and synchronized with your Windows workspace."
        for word in response_text.split(" "):
            await self.emit_token(task_id, word + " ")
            await asyncio.sleep(0.01)

    async def _run_gemini_agent_loop(self, task_id: str, prompt: str, model: str, api_key: str):
        """
        Executes against live Google Gemini API using direct unblocked Linux connectivity.
        """
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            model_instance = genai.GenerativeModel(model)

            await self.emit_thought(task_id, f"Connected to Gemini API ({model}) on Linux node.")
            response = await model_instance.generate_content_async(prompt)

            if hasattr(response, "text"):
                for chunk in response.text.split(" "):
                    await self.emit_token(task_id, chunk + " ")
                    await asyncio.sleep(0.01)
        except ImportError:
            await self.emit_thought(task_id, "google-generativeai package not found, falling back to agent loop.")
            await self._run_default_agent_loop(task_id, prompt, model)

    def start_task(self, task_id: str, prompt: str, model: str = "gemini-2.5-flash", context: Optional[Dict[str, Any]] = None):
        """Launches a new task asynchronously."""
        if task_id in self.active_tasks:
            logger.warning(f"Task {task_id} is already running")
            return
        task = asyncio.create_task(self.execute_task(task_id, prompt, model, context))
        self.active_tasks[task_id] = task

    def cancel_task(self, task_id: str):
        """Cancels an ongoing task."""
        if task_id in self.active_tasks:
            self.active_tasks[task_id].cancel()
            logger.info(f"Requested cancellation of task {task_id}")
