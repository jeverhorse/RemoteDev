"""
Communication Protocol for RemoteDev Hybrid Bridge.
Defines strongly-typed messages and serialization helpers.
"""

from typing import Dict, Any, Optional, Literal
from dataclasses import dataclass, asdict
import json
import time

# Message types
MSG_AUTH_REQ = "auth_req"
MSG_AUTH_RESP = "auth_resp"

MSG_EXECUTE_COMMAND = "execute_command"
MSG_COMMAND_STARTED = "command_started"
MSG_COMMAND_OUTPUT = "command_output"
MSG_COMMAND_COMPLETED = "command_completed"
MSG_CANCEL_COMMAND = "cancel_command"

MSG_SYNC_MANIFEST_REQ = "sync_manifest_req"
MSG_SYNC_MANIFEST_RESP = "sync_manifest_resp"
MSG_SYNC_EVENT = "sync_event"
MSG_SYNC_ACK = "sync_ack"

# Status and Heartbeat
MSG_STATUS_REQ = "status_req"
MSG_STATUS_RESP = "status_resp"
MSG_PING = "ping"
MSG_PONG = "pong"
MSG_ERROR = "error"

# Remote Agent Execution & Observability (Linux Agent <-> Windows Host)
MSG_AGENT_START_TASK = "agent_start_task"       # Windows -> Linux: prompt, task_id, model, review_policy, context
MSG_AGENT_CANCEL_TASK = "agent_cancel_task"     # Windows -> Linux: task_id
MSG_AGENT_THOUGHT = "agent_thought"             # Linux -> Windows: task_id, thought
MSG_AGENT_TOOL_CALL = "agent_tool_call"         # Linux -> Windows: task_id, call_id, tool_name, args
MSG_AGENT_TOOL_RESULT = "agent_tool_result"     # Linux -> Windows: task_id, call_id, tool_name, result
MSG_AGENT_FILE_ACCESS = "agent_file_access"     # Linux -> Windows: task_id, action (read/write), rel_path
MSG_AGENT_APPROVAL_REQ = "agent_approval_req"   # Linux -> Windows: task_id, call_id, action_desc, tool_name, args
MSG_AGENT_APPROVAL_RESP = "agent_approval_resp" # Windows -> Linux: task_id, call_id, approved, reason
MSG_AGENT_TOKEN = "agent_token"                 # Linux -> Windows: task_id, token
MSG_AGENT_TASK_COMPLETED = "agent_task_completed" # Linux -> Windows: task_id, status, summary

# Configuration & Policy Synchronization (Windows -> Linux)
MSG_CONFIG_SYNC = "config_sync"                 # Windows -> Linux: review_policy, rules, mcp_config, settings



def create_message(msg_type: str, **kwargs) -> str:
    """Helper to serialize a protocol message to JSON string."""
    payload = {"type": msg_type, **kwargs}
    return json.dumps(payload)


def parse_message(raw_data: str) -> Dict[str, Any]:
    """Helper to parse a protocol message from JSON string."""
    try:
        data = json.loads(raw_data)
        if not isinstance(data, dict) or "type" not in data:
            raise ValueError("Message must be a JSON object containing a 'type' field")
        return data
    except Exception as e:
        raise ValueError(f"Failed to parse message: {e}")
