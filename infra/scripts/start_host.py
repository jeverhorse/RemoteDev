#!/usr/bin/env python3
"""
Launcher script to start RemoteDev Windows Host Executor detached from shell job objects.
"""

import os
import sys
import subprocess
import time
from pathlib import Path
import urllib.request

infra_dir = Path(__file__).resolve().parent.parent
pid_file = infra_dir / ".remotedev_host.pid"
log_file = infra_dir / "host_executor.log"
err_file = infra_dir / "host_executor_err.log"
config_file = infra_dir / "config" / "config.yaml"

# Check if already running
if pid_file.exists():
    try:
        old_pid = int(pid_file.read_text().strip())
        import psutil
        if psutil.pid_exists(old_pid):
            print(f"Host Executor is already running with PID {old_pid}.")
            sys.exit(0)
    except Exception:
        pass

python_bin = sys.executable
cmd = [python_bin, "-u", "-m", "bridge.service", "--config", str(config_file)]

stdout_f = open(log_file, "a", encoding="utf-8")
stderr_f = open(err_file, "a", encoding="utf-8")

flags = 0
if sys.platform == "win32":
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

proc = subprocess.Popen(
    cmd,
    cwd=str(infra_dir),
    stdout=stdout_f,
    stderr=stderr_f,
    creationflags=flags,
)

pid_file.write_text(str(proc.pid))
print(f"Started Windows Host Executor with PID {proc.pid}")

# Wait for service to become healthy
healthy = False
for _ in range(25):
    time.sleep(0.2)
    try:
        req = urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=1)
        if req.status == 200:
            healthy = True
            break
    except Exception:
        pass

if healthy:
    print(f"Windows Host Executor is RUNNING and HEALTHY on port 8765! (PID: {proc.pid})")
else:
    print(f"Warning: Process spawned (PID {proc.pid}), but health check did not respond yet.")
