"""
Filesystem Watcher and Ignore Filter for RemoteDev Hybrid Bridge.
Uses watchfiles (Rust notify backend) with pattern matching for low-latency detection.
"""

import os
import hashlib
import fnmatch
from pathlib import Path
from typing import List, Set, Optional, Tuple, Dict, AsyncGenerator, Any
import asyncio
import logging

try:
    from watchfiles import awatch, Change
    HAS_WATCHFILES = True
except ImportError:
    HAS_WATCHFILES = False

logger = logging.getLogger("remotedev.watcher")


class IgnoreFilter:
    """Evaluates ignore patterns against relative file paths."""
    def __init__(self, patterns: List[str]):
        self.patterns = [p.replace("\\", "/").rstrip("/") for p in patterns]

    def is_ignored(self, rel_path: str) -> bool:
        clean = rel_path.replace("\\", "/").strip("/")
        parts = clean.split("/")

        for pattern in self.patterns:
            # Direct match
            if fnmatch.fnmatch(clean, pattern):
                return True
            # Strip trailing wildcards for directory matching
            clean_pat = pattern.rstrip("/*")
            # Check pattern with /** or /*
            if clean == clean_pat or clean.startswith(clean_pat + "/") or f"/{clean_pat}/" in f"/{clean}/":
                return True
            # Any path component match (e.g. .git, build, .dart_tool, node_modules)
            for part in parts:
                if fnmatch.fnmatch(part, clean_pat):
                    return True
        return False


def compute_file_hash(filepath: Path) -> Optional[str]:
    """Computes SHA-256 hash of a file. Returns None if file does not exist or unreadable."""
    if not filepath.is_file():
        return None
    try:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        logger.debug(f"Could not hash {filepath}: {e}")
        return None


def scan_workspace(root_dir: str, ignore_filter: IgnoreFilter) -> Dict[str, Dict[str, Any]]:
    """
    Recursively scans root_dir, returning a map of relative_path -> {hash, mtime, size, is_dir}.
    Ignores patterns defined in ignore_filter.
    """
    result = {}
    root = Path(root_dir).resolve()
    if not root.exists():
        return result

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""

        # Prune ignored directories from walk
        dirnames[:] = [
            d for d in dirnames
            if not ignore_filter.is_ignored(f"{rel_dir}/{d}".strip("/"))
        ]

        for fname in filenames:
            rel_path = f"{rel_dir}/{fname}".strip("/")
            if ignore_filter.is_ignored(rel_path):
                continue

            full_path = Path(dirpath) / fname
            try:
                stat = full_path.stat()
                file_hash = compute_file_hash(full_path)
                if file_hash is not None:
                    result[rel_path] = {
                        "hash": file_hash,
                        "mtime": stat.st_mtime,
                        "size": stat.st_size,
                        "is_dir": False,
                    }
            except Exception as e:
                logger.debug(f"Error scanning {full_path}: {e}")

    return result


async def watch_directory(
    root_dir: str,
    ignore_filter: IgnoreFilter,
    stop_event: asyncio.Event,
    poll_interval: float = 0.5
) -> AsyncGenerator[Tuple[str, str], None]:
    """
    Watches root_dir and yields (action, rel_path) where action is 'upsert' or 'delete'.
    """
    root = Path(root_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)

    if HAS_WATCHFILES:
        logger.info(f"Using watchfiles (Rust notify) watcher for {root}")
        try:
            async for changes in awatch(str(root), stop_event=stop_event):
                for change_type, path_str in changes:
                    try:
                        p = Path(path_str)
                        rel_path = os.path.relpath(p, root).replace("\\", "/")
                    except ValueError:
                        continue

                    if ignore_filter.is_ignored(rel_path):
                        continue

                    if change_type == Change.deleted:
                        yield ("delete", rel_path)
                    else:
                        yield ("upsert", rel_path)
        except asyncio.CancelledError:
            return
    else:
        logger.info(f"Using polling watcher fallback for {root}")
        prev_scan = scan_workspace(str(root), ignore_filter)
        while not stop_event.is_set():
            await asyncio.sleep(poll_interval)
            curr_scan = scan_workspace(str(root), ignore_filter)

            # Check deleted
            for rel_path in prev_scan:
                if rel_path not in curr_scan:
                    yield ("delete", rel_path)

            # Check added or modified
            for rel_path, meta in curr_scan.items():
                if rel_path not in prev_scan or prev_scan[rel_path]["hash"] != meta["hash"]:
                    yield ("upsert", rel_path)

            prev_scan = curr_scan
