# RemoteDev: Google Colab Transition & Migration Guide

## Overview
This document outlines the operational roadmap for migrating the RemoteDev hybrid development environment from the local Docker/WSL2 prototype to a remote **Google Colab** execution environment.

---

## Part A: What is Fully Working Locally

The local prototype provides a complete, working implementation of:
1. **Bidirectional Near-Real-Time Synchronization**:
   * File creation, modification, and deletion propagation across Windows and Linux workspaces.
   * Rust-backed `watchfiles` filesystem monitoring on Windows (`ReadDirectoryChangesW`) and Linux (`inotify`).
   * Deterministic echo-suppression preventing feedback loops via in-flight hash verification.
   * Debouncing (200ms) for atomic write patterns.
   * Conflict detection and preservation of divergent files (`*.conflict_<timestamp>`).
   * Ignore rules filtering `.git/`, `.dart_tool/`, `build/`, `node_modules/`, and temporary swap files.
2. **Windows Host Command Executor**:
   * Secure WebSocket bridge listening on port 8765.
   * Real-time unbuffered stdout and stderr chunk streaming.
   * Full process tree termination via `psutil` upon cancellation (Ctrl+C) or timeout.
   * Constant-time token authentication.
   * Strict path sandboxing preventing directory traversal attacks outside the configured workspace root.
3. **Agent Integration**:
   * Linux CLI tool `win-run` for interactive and scripted command execution on Windows.
   * Real-time terminal output streaming with preserved exit codes.
   * `win-run status` diagnostic dashboard.
4. **Resilience**:
   * Automatic client reconnection with exponential backoff on network interruption.
   * Startup manifest reconciliation.

---

## Part B: What is Only Simulated in the Local Prototype

1. **Local Network Topology**:
   * In the local prototype, communication occurs over Docker's internal virtual bridge network (`host.docker.internal` / `172.17.0.1`), where latency is sub-millisecond (<1ms) and bandwidth is essentially memory bus speed (gigabytes/sec).
2. **Direct Inbound Connectivity**:
   * The local Docker container can directly reach the Windows host IP because both share the same host networking bridge.
3. **Session Lifetime**:
   * The local Docker container persists indefinitely until explicitly stopped.

---

## Part C: What Will Need to Change for Google Colab

When moving the Linux Agent Environment to Google Colab, the following adaptations are required:

### 1. Inbound NAT & Reverse Tunneling Architecture
* **The Problem**: Google Colab instances run behind Google Cloud NAT. The Windows host machine usually sits behind a home/office router (NAT/firewall) with a dynamic private IP (`192.168.x.x`). Neither machine can directly reach the other via raw IP without port forwarding.
* **The Solution**: **Multiplexed Secure Tunneling**.
  Because RemoteDev multiplexes all command execution, output streaming, and file synchronization over a single WebSocket connection on port 8765, you only need ONE outbound tunnel!
  Recommended options:
  1. **Cloudflare Tunnel (`cloudflared`)**:
     * Zero-cost, zero-port-forwarding, authenticated tunnel.
     * Run `cloudflared tunnel --url http://localhost:8765` on Windows.
     * Cloudflare assigns a public secure URL (e.g. `https://hybrid-bridge-xyz.trycloudflare.com`).
     * In Colab, point `WINDOWS_HOST` to this tunnel address (`wss://hybrid-bridge-xyz.trycloudflare.com/ws`).
  2. **Tailscale (Mesh VPN)**:
     * Install Tailscale on Windows and in Colab (via `tailscale up --authkey=...`).
     * Both machines join your private encrypted overlay network.
     * Colab connects directly to Windows's Tailscale IP (`100.x.y.z:8765`).
  3. **Reverse SSH Tunnel (`ssh -R`)**:
     * Connect both Windows and Colab to a lightweight intermediate VPS ($3/mo) with a reverse tunnel.

### 2. Colab Bootstrap Script
Create a simple 1-cell notebook script for Google Colab:
```python
# Cell 1: Setup RemoteDev in Google Colab
!pip install -q aiohttp watchfiles pyyaml psutil
!git clone <YOUR_INFRA_REPO> /infra
%set_env REMOTEDEV_CONFIG=/infra/config/config.yaml
%set_env WINDOWS_HOST=your-tunnel.trycloudflare.com
%set_env LINUX_WORKSPACE=/content/workspace
!chmod +x /infra/bridge/win-run
!ln -sf /infra/bridge/win-run /usr/local/bin/win-run

# Start background sync daemon
import subprocess
subprocess.Popen(["python3", "-m", "bridge.client"])
print("RemoteDev connected to Windows host!")
```

### 3. Delta-Transfer Compression for Large Files
* On WAN connections (Colab <-> Home Windows), transferring large binaries (e.g. 20MB assets) as base64 strings consumes more bandwidth. For WAN operation, add gzip/zstandard compression to WebSocket sync frames.

---

## Part D: Architectural Limitations & Mitigations

| Potential Issue | Impact on Colab | Mitigation Strategy |
| :--- | :--- | :--- |
| **Colab Session Idle Disconnection** | Google Colab terminates idle notebook runtimes after 90 minutes of inactivity or 12 hours total. | RemoteDev sync is stateless and uses hash-based reconciliation on connect. When Colab reboots, running the 1-cell bootstrap immediately syncs the latest worktree from Windows within seconds. |
| **WAN Network Jitter & Latency** | Command streaming over internet adds 30-100ms round-trip latency. | Subprocess output streaming in `bridge/executor.py` already buffers lines asynchronously and flushes over WebSocket. Latency is negligible for terminal output and builds. |
| **Simultaneous Bidirectional Edits on Same File** | If both the agent in Colab and the developer on Windows edit line 5 of the exact same file within 200ms, a race occurs. | Handled automatically by `SyncEngine`: The remote change is applied, but the divergent local file is preserved as `filename.conflict_<timestamp>` so work is never lost. |
| **Bandwidth Consumption with Deep Build Trees** | If `build/` or `.dart_tool/` or `.git/` were synchronized, hundreds of megabytes would saturate the WAN link. | Strictly guarded by `ignore_patterns` in `infra/config/config.yaml`. Only source files and project assets are synced. |
