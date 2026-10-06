#!/bin/bash
# Start all sample backends

set -e

echo "🚀 Starting all sample backends..."
echo ""

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is required but not installed"
    exit 1
fi

# Install dependencies if needed
echo "📦 Checking dependencies..."
# Each backend lists what it needs (the GraphQL one needs graphql-core, the
# WebSocket one needs websockets); installing only a fixed handful left some
# backends unable to start.
for req in */requirements.txt; do
    pip install -q -r "$req" || echo "⚠️  Could not install $req"
done
mkdir -p logs

# Start backends in background
echo ""
echo "Starting MCP backend (port 8001)..."
python3 mcp/mcp_server.py > logs/mcp.log 2>&1 &
MCP_PID=$!
echo "  ✓ MCP backend started (PID: $MCP_PID)"

echo ""
echo "Starting A2A backend (port 8002)..."
python3 a2a/a2a_server.py > logs/a2a.log 2>&1 &
A2A_PID=$!
echo "  ✓ A2A backend started (PID: $A2A_PID)"

echo ""
echo "Starting HTTP backend (port 8003)..."
python3 http/http_server.py > logs/http.log 2>&1 &
HTTP_PID=$!
echo "  ✓ HTTP backend started (PID: $HTTP_PID)"

echo ""
echo "Starting WebSocket backend (port 8080)..."
python3 websocket/websocket_server.py > logs/websocket.log 2>&1 &
WS_PID=$!
echo "  ✓ WebSocket backend started (PID: $WS_PID)"

echo ""
echo "Starting AG-UI backend (port 8004)..."
python3 agui/agui_server.py > logs/agui.log 2>&1 &
AGUI_PID=$!
echo "  ✓ AG-UI backend started (PID: $AGUI_PID)"

echo ""
echo "Starting ACP backend (port 8005)..."
python3 acp/acp_server.py > logs/acp.log 2>&1 &
ACP_PID=$!
echo "  ✓ ACP backend started (PID: $ACP_PID)"

echo ""
echo "Starting GraphQL backend (port 8006)..."
python3 graphql/graphql_server.py > logs/graphql.log 2>&1 &
GQL_PID=$!
echo "  ✓ GraphQL backend started (PID: $GQL_PID)"

# Wait for backends to be ready
echo ""
echo "⏳ Waiting for backends to be ready..."
sleep 3

# Check health
echo ""
echo "🏥 Checking backend health..."

check_health() {
    local name=$1
    local url=$2
    if curl -s "$url" > /dev/null 2>&1; then
        echo "  ✓ $name is healthy"
        return 0
    else
        echo "  ✗ $name is not responding"
        return 1
    fi
}

check_health "MCP" "http://localhost:8001/health"
check_health "A2A" "http://localhost:8002/health"
check_health "HTTP" "http://localhost:8003/health"
check_health "WebSocket" "http://localhost:8080/health"
check_health "AG-UI" "http://localhost:8004/health"
check_health "ACP" "http://localhost:8005/health"
check_health "GraphQL" "http://localhost:8006/health"

echo ""
echo "✅ All backends started!"
echo ""
echo "Backend URLs:"
echo "  MCP:       http://localhost:8001"
echo "  A2A:       http://localhost:8002"
echo "  HTTP:      http://localhost:8003"
echo "  WebSocket: ws://localhost:8080"
echo "  AG-UI:     http://localhost:8004"
echo "  ACP:       http://localhost:8005"
echo "  GraphQL:   http://localhost:8006/graphql"
echo ""
echo "Logs are in: logs/"
echo ""
echo "To stop all backends:"
echo "  pkill -f 'mcp_server.py|a2a_server.py|http_server.py|websocket_server.py|agui_server.py|acp_server.py|graphql_server.py'"
echo ""
echo "Or use: bash stop_all.sh"

# Save PIDs for stop script
echo "$MCP_PID" > .pids
echo "$A2A_PID" >> .pids
echo "$HTTP_PID" >> .pids
echo "$WS_PID" >> .pids
echo "$AGUI_PID" >> .pids
echo "$ACP_PID" >> .pids
echo "$GQL_PID" >> .pids


