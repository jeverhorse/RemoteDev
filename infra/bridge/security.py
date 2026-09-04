"""
Security and Sandboxing Module for RemoteDev Hybrid Bridge.
Provides token verification, path sandboxing, and parameter sanitization.
"""

import os
import hmac
from pathlib import Path
from typing import Optional, Dict, Any


class SecurityError(Exception):
    """Raised when an operation violates security or sandboxing rules."""
    pass


def validate_token(expected_token: str, received_token: Optional[str]) -> bool:
    """Constant-time token validation to prevent timing attacks."""
    if not expected_token or not received_token:
        return False
    return hmac.compare_digest(expected_token.strip(), received_token.strip())


def sanitize_and_resolve_path(workspace_root: str, relative_or_absolute: Optional[str]) -> str:
    """
    Validates that a path resides strictly within workspace_root.
    Prevents directory traversal attacks (e.g., ../../Windows/System32).
    Returns normalized absolute path.
    """
    root = Path(workspace_root).resolve()
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)

    if not relative_or_absolute:
        return str(root)

    # Convert Windows / Linux separators consistently
    clean_path = str(relative_or_absolute).replace("\\", "/")
    
    # If absolute path was sent, check if it starts with root
    target = Path(clean_path)
    if not target.is_absolute():
        target = (root / target).resolve()
    else:
        target = target.resolve()

    try:
        target.relative_to(root)
    except ValueError:
        raise SecurityError(
            f"Access denied: Target path '{clean_path}' is outside configured workspace root '{root}'"
        )

    return str(target)


def sanitize_relative_sync_path(rel_path: str) -> str:
    """
    Validates relative path for synchronization.
    Blocks leading slashes, backslashes, drive letters, and '..' components.
    """
    clean = rel_path.replace("\\", "/").strip().lstrip("/")
    parts = clean.split("/")
    if any(p in ("..", ".", "") for p in parts if p != "."):
        # Check for traversal
        resolved_parts = []
        for p in parts:
            if p == "..":
                if resolved_parts:
                    resolved_parts.pop()
                else:
                    raise SecurityError(f"Directory traversal detected in sync path: {rel_path}")
            elif p != "." and p != "":
                resolved_parts.append(p)
        clean = "/".join(resolved_parts)

    if ":" in clean:
        raise SecurityError(f"Drive letter or invalid colon detected in sync path: {rel_path}")

    return clean
