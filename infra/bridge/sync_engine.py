"""
Bidirectional Synchronization Engine for RemoteDev Hybrid Bridge.
Handles echo suppression, debouncing, atomic writes, mtime preservation, and conflict resolution.
"""

import os
import base64
import time
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional, Callable, Awaitable, List
import logging

from .watcher import IgnoreFilter, compute_file_hash, scan_workspace
from .security import sanitize_relative_sync_path
from .diagnostics import DiagnosticsTracker
from .protocol import MSG_SYNC_EVENT, create_message

logger = logging.getLogger("remotedev.sync")


class SyncEngine:
    def __init__(
        self,
        workspace_dir: str,
        ignore_filter: IgnoreFilter,
        diagnostics: DiagnosticsTracker,
        send_message_fn: Callable[[str], Awaitable[None]],
        debounce_ms: int = 200,
        max_file_size_mb: int = 50,
    ):
        self.workspace_dir = Path(workspace_dir).resolve()
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.ignore_filter = ignore_filter
        self.diagnostics = diagnostics
        self.send_message = send_message_fn
        self.debounce_sec = debounce_ms / 1000.0
        self.max_file_size = max_file_size_mb * 1024 * 1024

        # Track local index: rel_path -> {hash, mtime, size}
        self.file_index: Dict[str, Dict[str, Any]] = {}

        # Echo suppression cache: rel_path -> expected_hash (or "DELETED")
        self.in_flight_remote_writes: Dict[str, str] = {}

        # Debouncing queue: rel_path -> (action, queue_timestamp)
        self.pending_outbound: Dict[str, Dict[str, Any]] = {}
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False

    def start(self):
        self._running = True
        self.file_index = scan_workspace(str(self.workspace_dir), self.ignore_filter)
        self._flush_task = asyncio.create_task(self._debounce_flush_loop())
        logger.info(f"SyncEngine started for {self.workspace_dir} ({len(self.file_index)} files indexed)")

    async def stop(self):
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

    def get_manifest(self) -> Dict[str, Dict[str, Any]]:
        """Returns current local manifest map."""
        self.file_index = scan_workspace(str(self.workspace_dir), self.ignore_filter)
        return self.file_index

    def queue_local_change(self, action: str, rel_path: str):
        """Called by watcher when local filesystem changes."""
        clean_path = sanitize_relative_sync_path(rel_path)
        if self.ignore_filter.is_ignored(clean_path):
            return

        full_path = self.workspace_dir / clean_path

        # Check echo suppression
        if clean_path in self.in_flight_remote_writes:
            expected = self.in_flight_remote_writes[clean_path]
            if action == "delete" and expected == "DELETED":
                logger.debug(f"[Echo Suppression] Suppressed local delete for {clean_path}")
                del self.in_flight_remote_writes[clean_path]
                self.file_index.pop(clean_path, None)
                return

            if action == "upsert" and full_path.is_file():
                current_hash = compute_file_hash(full_path)
                if current_hash == expected:
                    logger.debug(f"[Echo Suppression] Suppressed local upsert for {clean_path} (hash match)")
                    del self.in_flight_remote_writes[clean_path]
                    try:
                        st = full_path.stat()
                        self.file_index[clean_path] = {"hash": current_hash, "mtime": st.st_mtime, "size": st.st_size}
                    except Exception:
                        pass
                    return

        # Queue for debounced outbound sync
        self.pending_outbound[clean_path] = {
            "action": action,
            "time": time.time(),
        }
        self.diagnostics.pending_sync_operations = len(self.pending_outbound)

    async def _debounce_flush_loop(self):
        """Background loop flushing settled changes to remote peer."""
        while self._running:
            await asyncio.sleep(0.05)
            if not self.pending_outbound:
                continue

            now = time.time()
            to_flush = []
            for path, item in list(self.pending_outbound.items()):
                if now - item["time"] >= self.debounce_sec:
                    to_flush.append((path, item["action"]))
                    del self.pending_outbound[path]

            self.diagnostics.pending_sync_operations = len(self.pending_outbound)

            for rel_path, action in to_flush:
                await self._send_sync_event(action, rel_path)

    async def _send_sync_event(self, action: str, rel_path: str):
        """Prepares and transmits a sync event to the peer."""
        full_path = self.workspace_dir / rel_path

        if action == "delete":
            self.file_index.pop(rel_path, None)
            msg = create_message(
                MSG_SYNC_EVENT,
                action="delete",
                rel_path=rel_path,
                is_dir=False,
            )
            try:
                await self.send_message(msg)
                self.diagnostics.record_sync_event("sent", "delete", rel_path)
                logger.info(f"[Sync -> Remote] Deleted {rel_path}")
            except Exception as e:
                logger.error(f"Failed to send delete event for {rel_path}: {e}")
            return

        # Upsert
        if not full_path.exists():
            return

        if full_path.is_dir():
            msg = create_message(
                MSG_SYNC_EVENT,
                action="upsert",
                rel_path=rel_path,
                is_dir=True,
            )
            await self.send_message(msg)
            return

        try:
            stat = full_path.stat()
            if stat.st_size > self.max_file_size:
                logger.warning(f"File {rel_path} exceeds size limit ({stat.st_size} bytes). Skipping.")
                return

            with open(full_path, "rb") as f:
                content = f.read()

            content_b64 = base64.b64encode(content).decode("ascii")
            file_hash = compute_file_hash(full_path)

            # Update local index
            self.file_index[rel_path] = {
                "hash": file_hash,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            }

            msg = create_message(
                MSG_SYNC_EVENT,
                action="upsert",
                rel_path=rel_path,
                content_b64=content_b64,
                hash=file_hash,
                mtime=stat.st_mtime,
                is_dir=False,
            )
            await self.send_message(msg)
            self.diagnostics.record_sync_event("sent", "upsert", rel_path, stat.st_size)
        except Exception as e:
            logger.error(f"Failed to send upsert event for {rel_path}: {e}")

    async def push_file(self, rel_path: str, send_fn: Optional[Callable[[str], Awaitable[None]]] = None):
        """Pushes a single file to peer."""
        clean_path = sanitize_relative_sync_path(rel_path)
        if self.ignore_filter.is_ignored(clean_path):
            return
        full_path = self.workspace_dir / clean_path
        if not full_path.is_file():
            return
        try:
            stat = full_path.stat()
            if stat.st_size > self.max_file_size:
                return
            with open(full_path, "rb") as f:
                content = f.read()
            content_b64 = base64.b64encode(content).decode("ascii")
            file_hash = compute_file_hash(full_path)
            self.file_index[clean_path] = {
                "hash": file_hash,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            }
            msg = create_message(
                MSG_SYNC_EVENT,
                action="upsert",
                rel_path=clean_path,
                content_b64=content_b64,
                hash=file_hash,
                mtime=stat.st_mtime,
                is_dir=False,
            )
            sender = send_fn or self.send_message
            await sender(msg)
            self.diagnostics.record_sync_event("sent", "upsert", clean_path, stat.st_size)
            logger.info(f"[Initial Sync -> Remote] Pushed {clean_path} ({stat.st_size} bytes)")
        except Exception as e:
            logger.error(f"Error pushing file {clean_path}: {e}")

    async def push_all_files(self, send_fn: Optional[Callable[[str], Awaitable[None]]] = None):
        """Pushes all existing indexed workspace files to peer on connect."""
        self.file_index = scan_workspace(str(self.workspace_dir), self.ignore_filter)
        logger.info(f"Pushing full workspace snapshot ({len(self.file_index)} files)...")
        for rel_path in list(self.file_index.keys()):
            await self.push_file(rel_path, send_fn)

    async def apply_remote_event(self, event_data: Dict[str, Any]):
        """Processes an incoming sync event from the remote peer."""
        action = event_data.get("action")
        rel_path = sanitize_relative_sync_path(event_data.get("rel_path", ""))
        is_dir = event_data.get("is_dir", False)
        target_path = self.workspace_dir / rel_path

        if self.ignore_filter.is_ignored(rel_path):
            logger.debug(f"Ignoring remote event for ignored path: {rel_path}")
            return

        if action == "delete":
            self.in_flight_remote_writes[rel_path] = "DELETED"
            self.file_index.pop(rel_path, None)
            if target_path.exists():
                try:
                    if target_path.is_dir():
                        import shutil
                        shutil.rmtree(target_path, ignore_errors=True)
                    else:
                        target_path.unlink(missing_ok=True)
                    logger.info(f"[Sync <- Remote] Deleted {rel_path}")
                    self.diagnostics.record_sync_event("received", "delete", rel_path)
                except Exception as e:
                    logger.error(f"Failed to delete {target_path}: {e}")
            return

        if is_dir:
            target_path.mkdir(parents=True, exist_ok=True)
            return

        # File Upsert
        remote_hash = event_data.get("hash")
        remote_mtime = event_data.get("mtime")
        content_b64 = event_data.get("content_b64", "")

        try:
            content = base64.b64decode(content_b64)
        except Exception as e:
            logger.error(f"Failed to decode base64 for {rel_path}: {e}")
            return

        # Check for conflict: local file modified with different hash than both previous index and remote
        if target_path.exists() and target_path.is_file():
            local_hash = compute_file_hash(target_path)
            if local_hash != remote_hash:
                # Local has changes that weren't synced yet
                prev_meta = self.file_index.get(rel_path)
                if prev_meta and prev_meta.get("hash") != local_hash and rel_path in self.pending_outbound:
                    # True concurrent modification conflict!
                    conflict_name = f"{target_path.name}.conflict_{int(time.time())}"
                    backup_path = target_path.parent / conflict_name
                    try:
                        import shutil
                        shutil.copy2(target_path, backup_path)
                        logger.warning(f"[CONFLICT DETECTED] Conflict on {rel_path}. Saved backup to {backup_path.name}")
                        self.diagnostics.record_conflict(rel_path, local_hash or "", remote_hash or "", str(backup_path))
                    except Exception as ce:
                        logger.error(f"Failed to create conflict backup: {ce}")

        # Register for echo suppression
        if remote_hash:
            self.in_flight_remote_writes[rel_path] = remote_hash

        # Write file atomically
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.with_suffix(f"{target_path.suffix}.remotedev_tmp")
        try:
            with open(tmp_path, "wb") as f:
                f.write(content)

            # Preserve timestamp if available
            if remote_mtime:
                try:
                    os.utime(tmp_path, (remote_mtime, remote_mtime))
                except Exception:
                    pass

            # Atomic replace
            tmp_path.replace(target_path)

            self.file_index[rel_path] = {
                "hash": remote_hash or compute_file_hash(target_path),
                "mtime": remote_mtime or target_path.stat().st_mtime,
                "size": len(content),
            }
            logger.info(f"[Sync <- Remote] Applied {rel_path} ({len(content)} bytes)")
            self.diagnostics.record_sync_event("received", "upsert", rel_path, len(content))
        except Exception as e:
            logger.error(f"Failed to write file {target_path}: {e}")
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
