---
name: delegate
description: Delegates a coding or refactoring task to the remote Linux agent in the RemoteDev hybrid environment (Google Colab / Linux container), bypassing Windows ISP restrictions while streaming thinking and file access live.
---

# RemoteDev Agent Delegation

This skill allows Antigravity IDE to delegate complex coding, refactoring, or high-speed tasks to the remote Linux agent.

## How to Use

1. **Via MCP Tool in Chat Panel**:
   Mention `@remotedev-hybrid` or ask:
   > "Delegate to remote agent: [your instruction here]"

2. **Via Windows Terminal**:
   Run from PowerShell:
   ```powershell
   .\infra\scripts\agent.ps1 "[your prompt here]"
   ```

3. **What Happens Behind the Scenes**:
   - The prompt is transmitted over the RemoteDev bridge to Linux.
   - The Linux agent runs with unrestricted Google AI access.
   - Live **thinking traces** and **file access notices** stream back in real time.
   - Modified files are automatically synchronized back to your Windows workspace and editor tabs.
   - If Antigravity review policy is active (`request-review`), modifying actions pause and ask for your approval on Windows.
