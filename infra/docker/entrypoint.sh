#!/usr/bin/env bash
set -e

echo "=== Starting RemoteDev Linux Agent Environment ==="
echo "Workspace: $LINUX_WORKSPACE"
echo "Windows Host: $WINDOWS_HOST"

# Start background sync client if not explicitly disabled
if [ "$DISABLE_SYNC_DAEMON" != "1" ]; then
    echo "Starting background sync daemon..."
    python3 -m bridge.client &
    SYNC_PID=$!
    echo "Sync daemon started with PID $SYNC_PID"
fi

echo "Environment ready. 'win-run' is available."
exec "$@"
