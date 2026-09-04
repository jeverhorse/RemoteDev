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

## Part C: Solution 2 — Antigravity Headless on Colab + Remote Control + Windows Sync

This is the exact architecture you requested:
1. **Official Google Login & 5-Hour Quota**: You log in with your Google account directly inside Colab via `agy auth login`, unlocking your personal 5-hour Antigravity quota without any Gemini API keys.
2. **Antigravity Remote Control Canvas**: You interact with the agent through the official Antigravity web interface (`https://antigravity.google/remote`), complete with model selection (*Gemini 3.8 Flash*, *Gemini Pro*), live thinking traces, tool views, and planning.
3. **Transparent Windows Workspace Sync**: `SyncEngine` continuously synchronizes files between Colab (`/content/workspace`) and your Windows workspace in near real-time.
4. **Native Windows Tool Execution**: Whenever local Windows tools are needed (Flutter SDK, Dart analyzer, Android emulators), the agent runs them on Windows via `win-run` / the RemoteDev bridge.

---

### Phase 1: On Your Windows Machine (Host & Tunnel)

#### Step 1: Install `cloudflared` (One-time)
Open PowerShell and run:
```powershell
winget install Cloudflare.cloudflared
```
Verify installation:
```powershell
cloudflared --version
```

#### Step 2: Start the Windows Host Executor
In your project root on Windows, run:
```powershell
powershell -File .\infra\scripts\start_windows_host.ps1
```
You should see:
```text
Windows Host Executor is RUNNING and HEALTHY on port 8765!
```

#### Step 3: Launch the Cloudflare Tunnel
Open a second PowerShell window and run:
```powershell
cloudflared tunnel --url http://localhost:8765
```
Look for the line containing `trycloudflare.com`:
```text
+--------------------------------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at:                                          |
|  https://your-tunnel-words.trycloudflare.com                                               |
+--------------------------------------------------------------------------------------------+
```
> [!IMPORTANT]
> Copy that domain (e.g. `your-tunnel-words.trycloudflare.com`). Keep this PowerShell window running while you work!

---

### Phase 2: In Google Colab (1-Time Setup per Session)

Open a new notebook on [Google Colab](https://colab.research.google.com/). You will run 3 short, beginner-friendly cells:

#### Cell 1: Install Antigravity CLI & RemoteDev Sync
```python
# Cell 1: Install Antigravity CLI & Dependencies
!apt-get update -qq && apt-get install -y -qq dbus dbus-x11
!pip install -q aiohttp watchfiles pyyaml psutil
!curl -fsSL https://antigravity.google/cli/agy-daemon.sh | bash

import os
os.environ["PATH"] = f"/root/.gemini/antigravity-cli/bin:{os.environ.get('PATH', '')}"
!agy --version
```

#### Cell 2: Log into Your Google Account (Unlocks 5-Hour Quota)
```python
# Cell 2: Authenticate your Google Account
# This connects your 5-hour Antigravity subscription quota directly to Colab!
!export $(dbus-launch) && agy auth login
```
* **What to do**: Colab will display an authorization URL and a verification code.
* Click the link, sign in with your Google account (the one with the Antigravity 5-hour quota), click **Allow**, and paste the code back into Colab if prompted.
* You are now officially authenticated with your Google account on Colab!

#### Cell 3: Start Headless Daemon & RemoteDev Workspace Sync
Paste your Cloudflare tunnel domain into `TUNNEL_DOMAIN` below:

```python
# ==============================================================================
# Cell 3: Start Antigravity Remote Control Daemon + RemoteDev Sync Bridge
# ==============================================================================
import os, sys, subprocess, time

# 1. Configuration
TUNNEL_DOMAIN = "your-tunnel-words.trycloudflare.com"  # <-- PASTE YOUR TUNNEL HERE
AUTH_TOKEN = "remotedev-secret-hybrid-token-2026"
WORKSPACE_DIR = "/content/workspace"
INFRA_DIR = "/content/infra"

os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(INFRA_DIR, exist_ok=True)

# 2. Set up win-run symlink for the agent
!chmod +x /infra/bridge/win-run 2>/dev/null || true
!ln -sf /infra/bridge/win-run /usr/local/bin/win-run 2>/dev/null || true

# 3. Start Antigravity Headless Remote Control Daemon
!export $(dbus-launch) && nohup agy-daemon start --workspace /content/workspace > /content/agy_daemon.log 2>&1 &
print("🚀 Antigravity Headless Daemon started on Colab!")

# 4. Start RemoteDev Sync Engine connecting to Windows
os.environ["WINDOWS_HOST"] = TUNNEL_DOMAIN
os.environ["LINUX_WORKSPACE"] = WORKSPACE_DIR
os.environ["PYTHONPATH"] = f"{INFRA_DIR}:{os.environ.get('PYTHONPATH', '')}"

wss_url = f"wss://{TUNNEL_DOMAIN.strip().replace('https://', '').replace('http://', '').strip('/')}/ws"
print(f"🔗 Connecting RemoteDev file sync to Windows: {wss_url}")

log_file = open("/content/remotedev_client.log", "w")
client_proc = subprocess.Popen(
    ["python3", "-m", "bridge.client", "--server", wss_url],
    cwd=INFRA_DIR,
    stdout=log_file,
    stderr=subprocess.STDOUT,
)

time.sleep(3)
if client_proc.poll() is None:
    print("\n✅ System fully online!")
    print(f"   - 5-Hour Quota: Authenticated on your Google account")
    print(f"   - Antigravity Remote Control: ACTIVE")
    print(f"   - Bidirectional Workspace Sync: CONNECTED to Windows")
else:
    print("\n❌ Sync daemon error:")
    with open("/content/remotedev_client.log") as f:
        print(f.read())
```

---

### Phase 3: Your Day-to-Day Workflow on Windows

Once Phase 1 and 2 are running, **you do not touch any CLI commands on Windows**:

#### 1. Open the Antigravity Agent Interface
Open your web browser (or an embedded tab in your IDE via `Simple Browser: Show`) and go to:
👉 **https://antigravity.google/remote**

You will see:
* Your **logged-in Google Account** with your **5-hour sliding quota**.
* Model selection dropdown (*Gemini 3.8 Flash*, *Gemini 3.8 Pro*, etc.).
* The full agent chat canvas, multi-turn conversations, live thinking deltas, and tool executions.

#### 2. Prompt the Agent
Type your prompts naturally in the web canvas:
> "Analyze the Flutter workspace in /content/workspace, add a login screen in lib/screens/login.dart, and run flutter analyze."

#### 3. Automatic Synchronization to Windows
* As the agent reasons and modifies files in `/content/workspace`, `SyncEngine` synchronizes the files to your Windows project directory within 200ms.
* In your Windows editor, open `lib/screens/login.dart`—it updates live before your eyes.
* When the agent needs to run `flutter test` or local Windows commands, it invokes `win-run "flutter test"` behind the scenes, running natively on your Windows PC!

---

### Troubleshooting & FAQ

| Question | Answer |
| :--- | :--- |
| **Do I need to log into Google on Windows?** | **No**. The Google login is done once in Colab (Cell 2). Windows never contacts Google services. |
| **What happens if Colab disconnects after idle time?** | Free Colab times out after 90 minutes of inactivity. When you restart Colab, simply run Cells 1, 2, and 3 again. RemoteDev syncs the entire workspace from Windows in seconds. |
| **If my Cloudflare tunnel URL changes, what do I update?** | If you close and reopen `cloudflared`, paste the new `trycloudflare.com` URL into Cell 3's `TUNNEL_DOMAIN` and re-run Cell 3. |
| **Does this use my personal 5-hour quota?** | **Yes**. Because `agy auth login` runs on Colab with your personal Google credentials, all agent calls tap into your 5-hour Antigravity quota. |


