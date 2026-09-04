# Transparent Inverted Hybrid Development Environment Rule

You are operating inside a **Hybrid Development Environment** designed to bypass ISP restrictions on Google AI / Gemini services while maintaining a 100% native Windows developer experience.

## Core Architectural Principles

1. **Remote Agent Execution on Linux**:
   - The AI Agent reasoning loop executes on the remote Linux environment (Google Colab or Linux VM) where Google AI services are directly accessible at gigabit speeds without ISP restrictions, VPNs, or proxy latency.
   - The Linux agent directly performs thinking, planning, and file modifications in `/workspace/project`.

2. **Transparent Windows Observability & Controls**:
   - The developer initiates prompts naturally on Windows via the `agent` command or the Antigravity IDE `delegate_to_remote_agent` MCP tool.
   - All **thinking deltas** (`[Thinking]`) are streamed in real time to Windows.
   - All **file access events** (`[File Access]`) are reported to Windows as files are read or written.
   - File edits made by the Linux agent are immediately synchronized to the Windows workspace via `SyncEngine` and refresh in Antigravity IDE editor tabs.

3. **Antigravity IDE Policy & Config Synchronization**:
   - Antigravity IDE Review Policies (`request-review`, `always-proceed`, `strict`) are synchronized from Windows to Linux.
   - When a review policy is active, the Linux agent pauses before executing modifying tools or commands and requests approval on Windows. The user approves or denies directly from Windows.
   - Workspace rules (`.agents/rules/`), MCP definitions (`.agents/mcp_config.json`), and skills automatically synchronize from Windows to Linux.

4. **Transparent Command Execution Routing**:
   - **Linux Remote Environment**:
     - Use for: High-speed internet downloads, package installation (`pip`, `apt`), downloading AI models/weights, Linux shell scripts, or git clones.
     - Execution: Runs locally in the Linux agent environment.
   - **Windows Host Environment**:
     - Use for: Local compilation, Flutter SDK (`flutter run`, `flutter build`, `flutter test`), Dart SDK (`dart analyze`, `dart test`), Android SDK/emulators, and PowerShell/CMD tasks.
     - Execution: The Linux agent dispatches native execution to Windows via `run_in_windows` / the bridge executor.

