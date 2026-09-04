# RemoteDev Hybrid Development Environment: Troubleshooting Guide

## 1. Quick Diagnostics Check

Always start by querying the diagnostics status:
```bash
# Inside Linux container:
win-run status

# On Windows host (PowerShell):
Invoke-RestMethod http://localhost:8765/status | ConvertTo-Json -Depth 5
```

---

## 2. Common Issues & Solutions

### Issue A: `[win-run] Error: Could not connect to Windows bridge at ws://host.docker.internal:8765/ws`

**Cause 1: Windows Host Executor daemon is not running.**
* **Solution**: Start the daemon on Windows:
  ```powershell
  powershell -File infra\scripts\start_windows_host.ps1
  ```
  Verify by checking `http://localhost:8765/health` in a browser or curl.

**Cause 2: Docker cannot resolve `host.docker.internal`.**
* **Solution**: Ensure the container was started with `--add-host=host.docker.internal:host-gateway`. Check resolution inside the container:
  ```bash
  python3 -c "import socket; print(socket.gethostbyname('host.docker.internal'))"
  ```
  If it fails, pass the host machine's LAN IP or Docker gateway IP via `WINDOWS_HOST` environment variable:
  ```bash
  docker run -e WINDOWS_HOST=172.17.0.1 ...
  ```

**Cause 3: Windows Defender Firewall blocking port 8765.**
* **Solution**: Allow inbound traffic for port 8765 on private networks:
  ```powershell
  New-NetFirewallRule -DisplayName "RemoteDev Bridge" -Direction Inbound -LocalPort 8765 -Protocol TCP -Action Allow
  ```

---

### Issue B: `Authentication failed: invalid token`

**Cause: Mismatch between Linux agent token and Windows host token.**
* **Solution**: Check `infra/config/config.yaml`. Verify both environments use identical `server.token` values, or override with `REMOTEDEV_TOKEN` environment variable.

---

### Issue C: Files are not synchronizing

**Check 1: Ignore filter match.**
* Verify the file does not match an ignore rule in `infra/config/config.yaml` (such as `.git/`, `build/`, `.dart_tool/`, `*.tmp`, `node_modules/`).

**Check 2: File exceeds maximum allowed size.**
* Default limit is 50MB. Adjust `sync.max_file_size_mb` in `config.yaml` if working with large binary assets.

**Check 3: Pending debounce queue.**
* Run `win-run status` and look at `pending_operations`. If an editor is holding an active lock or writing continuously, the event will settle after 200ms.

---

### Issue D: `Access denied: Target path is outside configured workspace root`

**Cause: Path traversal security violation.**
* The executor strictly enforces sandboxing. Any command attempting to set `--cwd` outside the configured `workspace.windows_path` is rejected.
* Ensure you provide relative paths within the workspace or configure the root path in `config.yaml`.

---

### Issue E: Long-running command stuck or hanging

**Solution:**
* In `win-run`, press `Ctrl+C`. This triggers `cancel_command`, instructing the host executor to terminate the process and all child processes recursively via `psutil`.
* Alternatively, specify a timeout:
  ```bash
  win-run --timeout 30 "flutter build apk"
  ```
