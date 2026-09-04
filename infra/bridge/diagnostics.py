"""
Diagnostics Module for RemoteDev Hybrid Bridge.
Tracks connection status, sync health, command execution history, and errors.
"""

from typing import Dict, Any, List, Optional
import time
from collections import deque


class DiagnosticsTracker:
    def __init__(self, history_limit: int = 50):
        self.history_limit = history_limit
        self.is_connected = False
        self.peer_info: Optional[str] = None
        self.connection_time: Optional[float] = None
        
        # Sync metrics
        self.last_sync_event: Optional[Dict[str, Any]] = None
        self.pending_sync_operations: int = 0
        self.total_sync_sent: int = 0
        self.total_sync_received: int = 0
        self.conflicts: List[Dict[str, Any]] = []
        
        # Command execution metrics
        self.running_commands: Dict[str, Dict[str, Any]] = {}
        self.command_history: deque = deque(maxlen=history_limit)
        
        # Errors
        self.errors: deque = deque(maxlen= history_limit)

    def set_connected(self, connected: bool, peer_info: Optional[str] = None):
        self.is_connected = connected
        self.peer_info = peer_info
        if connected:
            self.connection_time = time.time()
        else:
            self.connection_time = None

    def record_sync_event(self, direction: str, action: str, rel_path: str, size: int = 0):
        event = {
            "timestamp": time.time(),
            "time_str": time.strftime("%H:%M:%S"),
            "direction": direction, # "sent" or "received"
            "action": action,       # "upsert" or "delete"
            "path": rel_path,
            "size": size,
        }
        self.last_sync_event = event
        if direction == "sent":
            self.total_sync_sent += 1
        else:
            self.total_sync_received += 1

    def record_conflict(self, rel_path: str, local_hash: str, remote_hash: str, backup_path: str):
        conflict = {
            "timestamp": time.time(),
            "time_str": time.strftime("%H:%M:%S"),
            "path": rel_path,
            "local_hash": local_hash,
            "remote_hash": remote_hash,
            "backup_path": backup_path,
        }
        self.conflicts.append(conflict)

    def command_started(self, command_id: str, cmd: str, shell: str, cwd: str, pid: int):
        self.running_commands[command_id] = {
            "command_id": command_id,
            "cmd": cmd,
            "shell": shell,
            "cwd": cwd,
            "pid": pid,
            "start_time": time.time(),
            "start_str": time.strftime("%H:%M:%S"),
            "recent_output": deque(maxlen=20),
        }

    def command_output(self, command_id: str, stream: str, chunk: str):
        if command_id in self.running_commands:
            self.running_commands[command_id]["recent_output"].append(f"[{stream}] {chunk}")

    def command_completed(self, command_id: str, exit_code: int, status: str, duration_ms: float):
        cmd_info = self.running_commands.pop(command_id, None)
        record = {
            "command_id": command_id,
            "cmd": cmd_info["cmd"] if cmd_info else "unknown",
            "shell": cmd_info["shell"] if cmd_info else "unknown",
            "cwd": cmd_info["cwd"] if cmd_info else "unknown",
            "exit_code": exit_code,
            "status": status,
            "duration_ms": duration_ms,
            "completed_at": time.time(),
            "completed_str": time.strftime("%H:%M:%S"),
            "output_tail": list(cmd_info["recent_output"]) if cmd_info else [],
        }
        self.command_history.append(record)

    def record_error(self, message: str):
        self.errors.append({
            "timestamp": time.time(),
            "time_str": time.strftime("%H:%M:%S"),
            "message": message,
        })

    def get_status_summary(self) -> Dict[str, Any]:
        return {
            "is_connected": self.is_connected,
            "peer_info": self.peer_info,
            "uptime_seconds": round(time.time() - self.connection_time, 1) if self.connection_time else 0,
            "sync": {
                "last_event": self.last_sync_event,
                "pending_operations": self.pending_sync_operations,
                "total_sent": self.total_sync_sent,
                "total_received": self.total_sync_received,
                "conflicts_count": len(self.conflicts),
                "recent_conflicts": self.conflicts[-5:],
            },
            "commands": {
                "currently_running_count": len(self.running_commands),
                "running": [
                    {k: v for k, v in c.items() if k != "recent_output"}
                    for c in self.running_commands.values()
                ],
                "history_count": len(self.command_history),
                "recent_history": list(self.command_history)[-5:],
            },
            "recent_errors": list(self.errors)[-5:],
        }
