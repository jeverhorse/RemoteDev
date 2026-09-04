"""
Windows Host Process Executor for RemoteDev Hybrid Bridge.
Manages asynchronous subprocess execution, real-time stdout/stderr streaming,
timeout enforcement, and full process-tree termination via psutil.
"""

import os
import sys
import time
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional, Callable, Awaitable
import logging

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from .security import sanitize_and_resolve_path, SecurityError
from .diagnostics import DiagnosticsTracker
from .protocol import (
    MSG_COMMAND_STARTED,
    MSG_COMMAND_OUTPUT,
    MSG_COMMAND_COMPLETED,
    create_message,
)

logger = logging.getLogger("remotedev.executor")


class CommandExecutor:
    def __init__(self, default_workspace_dir: str, diagnostics: DiagnosticsTracker):
        self.default_workspace = Path(default_workspace_dir).resolve()
        self.diagnostics = diagnostics
        self.running_processes: Dict[str, asyncio.subprocess.Process] = {}
        self.cancelled_commands: set = set()

    def _kill_process_tree(self, pid: int):
        """Recursively terminates process and all its children using psutil."""
        if not HAS_PSUTIL:
            try:
                import subprocess
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            except Exception as e:
                logger.error(f"Failed to taskkill pid {pid}: {e}")
            return

        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            parent.kill()
            logger.info(f"Terminated process tree for PID {pid} ({len(children)} child processes)")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        except Exception as e:
            logger.error(f"Error terminating process tree for PID {pid}: {e}")

    def cancel_command(self, command_id: str) -> bool:
        """Cancels a currently executing command."""
        self.cancelled_commands.add(command_id)
        proc = self.running_processes.get(command_id)
        if proc and proc.returncode is None:
            logger.warning(f"Cancelling command {command_id} (PID {proc.pid})")
            self._kill_process_tree(proc.pid)
            return True
        return False

    async def run_command(
        self,
        command_id: str,
        command_str: str,
        shell: str,
        cwd: Optional[str],
        env_vars: Optional[Dict[str, str]],
        timeout_sec: Optional[int],
        send_message_fn: Callable[[str], Awaitable[None]],
    ) -> Dict[str, Any]:
        """
        Executes command on Windows host, streams output in real time,
        and returns completion details.
        """
        # 1. Path Sandboxing
        try:
            resolved_cwd = sanitize_and_resolve_path(str(self.default_workspace), cwd)
        except SecurityError as se:
            err_msg = str(se)
            logger.error(err_msg)
            await send_message_fn(create_message(
                MSG_COMMAND_COMPLETED,
                command_id=command_id,
                exit_code=-1,
                start_time=time.time(),
                end_time=time.time(),
                duration_ms=0,
                status="failed",
                error=err_msg,
            ))
            return {"exit_code": -1, "status": "failed", "error": err_msg}

        # 2. Build execution command arguments
        shell_lower = (shell or "powershell").lower()
        if shell_lower in ("powershell", "pwsh"):
            cmd_args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command_str]
        elif shell_lower == "cmd":
            cmd_args = ["cmd.exe", "/c", command_str]
        elif shell_lower == "bash":
            cmd_args = ["bash.exe", "-c", command_str]
        else:
            cmd_args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command_str]

        # 3. Environment preparation
        merged_env = os.environ.copy()
        # Force unbuffered Python output if python is invoked
        merged_env["PYTHONUNBUFFERED"] = "1"
        if env_vars:
            merged_env.update(env_vars)

        start_time = time.time()
        logger.info(f"Spawning command [{command_id}]: {command_str} (in {resolved_cwd})")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=resolved_cwd,
                env=merged_env,
            )
        except Exception as e:
            err_msg = f"Failed to spawn process: {e}"
            logger.error(err_msg)
            await send_message_fn(create_message(
                MSG_COMMAND_COMPLETED,
                command_id=command_id,
                exit_code=-1,
                start_time=start_time,
                end_time=time.time(),
                duration_ms=0,
                status="failed",
                error=err_msg,
            ))
            return {"exit_code": -1, "status": "failed", "error": err_msg}

        self.running_processes[command_id] = proc
        self.diagnostics.command_started(command_id, command_str, shell_lower, resolved_cwd, proc.pid)

        # Notify command started
        await send_message_fn(create_message(
            MSG_COMMAND_STARTED,
            command_id=command_id,
            pid=proc.pid,
            start_time=start_time,
        ))

        # 4. Stream stdout and stderr chunks
        async def stream_reader(stream: asyncio.StreamReader, stream_name: str):
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                self.diagnostics.command_output(command_id, stream_name, text)
                await send_message_fn(create_message(
                    MSG_COMMAND_OUTPUT,
                    command_id=command_id,
                    stream=stream_name,
                    chunk=text,
                    timestamp=time.time(),
                ))

        reader_tasks = [
            asyncio.create_task(stream_reader(proc.stdout, "stdout")),
            asyncio.create_task(stream_reader(proc.stderr, "stderr")),
        ]

        # 5. Wait for completion or timeout
        status = "success"
        timed_out = False
        try:
            if timeout_sec and timeout_sec > 0:
                await asyncio.wait_for(proc.wait(), timeout=timeout_sec)
            else:
                await proc.wait()
        except asyncio.TimeoutError:
            timed_out = True
            status = "timeout"
            logger.warning(f"Command [{command_id}] timed out after {timeout_sec}s. Terminating.")
            self._kill_process_tree(proc.pid)
            await proc.wait()

        # Wait for readers to finish draining
        await asyncio.gather(*reader_tasks, return_exceptions=True)

        end_time = time.time()
        duration_ms = round((end_time - start_time) * 1000.0, 2)
        exit_code = proc.returncode if proc.returncode is not None else -1

        if command_id in self.cancelled_commands:
            status = "cancelled"
            self.cancelled_commands.remove(command_id)
        elif timed_out:
            status = "timeout"
        elif exit_code != 0:
            status = "failed"

        self.running_processes.pop(command_id, None)
        self.diagnostics.command_completed(command_id, exit_code, status, duration_ms)

        # Notify command completed
        await send_message_fn(create_message(
            MSG_COMMAND_COMPLETED,
            command_id=command_id,
            exit_code=exit_code,
            start_time=start_time,
            end_time=end_time,
            duration_ms=duration_ms,
            status=status,
        ))

        logger.info(f"Command [{command_id}] finished: status={status}, exit_code={exit_code}, duration={duration_ms}ms")
        return {
            "command_id": command_id,
            "exit_code": exit_code,
            "status": status,
            "duration_ms": duration_ms,
        }
