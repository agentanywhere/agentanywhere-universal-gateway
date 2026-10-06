#!/bin/bash
# Stop all sample backends

echo "🛑 Stopping all sample backends..."

if [ -f .pids ]; then
    while read pid; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null && echo "  ✓ Stopped process $pid"
        fi
    done < .pids
    rm .pids
fi

# Also kill by process name
pkill -f 'mcp_server.py|a2a_server.py|http_server.py|websocket_server.py|agui_server.py|acp_server.py|graphql_server.py' 2>/dev/null && echo "  ✓ Stopped remaining processes"

echo "✅ All backends stopped"


