#!/bin/bash
# Quick Start Test Script
# Tests that the quick start guide works end-to-end

set -e

echo "🚀 Testing Quick Start Guide"
echo "=============================="
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# --------------------------------------------------------------------------
# Choose a Python interpreter that has the project deps installed. Falls
# back to `python` (often Anaconda) when the system `python3` is missing
# packages like `requests`. Honors an explicit ``$PYTHON`` override.
# --------------------------------------------------------------------------
choose_python() {
    if [ -n "$PYTHON" ] && command -v "$PYTHON" > /dev/null 2>&1; then
        if "$PYTHON" -c "import requests" > /dev/null 2>&1; then
            command -v "$PYTHON"
            return
        fi
    fi
    for candidate in python3 python python3.12 python3.11; do
        local resolved
        resolved="$(command -v "$candidate" 2>/dev/null)"
        if [ -n "$resolved" ] && "$resolved" -c "import requests" > /dev/null 2>&1; then
            echo "$resolved"
            return
        fi
    done
    echo ""  # no suitable interpreter
}

PY="$(choose_python)"
if [ -n "$PY" ]; then
    echo -e "${GREEN}✓${NC} Using Python interpreter: $PY"
else
    echo -e "${YELLOW}⚠${NC} No Python interpreter with 'requests' found. Skipping Python example."
fi

# Check if gateway is running
echo "1. Checking if gateway is running..."
if curl -s http://localhost:8000/health > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Gateway is running"
else
    echo -e "${RED}✗${NC} Gateway is not running. Start it with: docker-compose up -d"
    exit 1
fi

# Check health endpoint
echo ""
echo "2. Testing health endpoint..."
HEALTH=$(curl -s http://localhost:8000/health | jq -r '.status' 2>/dev/null)
if [ "$HEALTH" = "healthy" ]; then
    echo -e "${GREEN}✓${NC} Health check passed: $HEALTH"
else
    echo -e "${RED}✗${NC} Health check failed: $HEALTH"
    exit 1
fi

# Check ready endpoint
echo ""
echo "3. Testing ready endpoint..."
READY=$(curl -s http://localhost:8000/ready | jq -r '.status' 2>/dev/null)
if [ "$READY" = "ready" ]; then
    echo -e "${GREEN}✓${NC} Ready check passed: $READY"
else
    echo -e "${RED}✗${NC} Ready check failed: $READY"
    exit 1
fi

# Create API key
echo ""
echo "4. Creating test API key..."
API_KEY_RESPONSE=$(curl -s -X POST "http://localhost:8000/admin/api-keys?name=quickstart-test-key&rate_limit_per_minute=100&rate_limit_per_hour=1000" \
    -H "Authorization: Bearer ${ADMIN_TOKEN:-${SECRET_KEY:?Set ADMIN_TOKEN (or SECRET_KEY) to the gateway admin credential}}")

API_KEY=$(echo "$API_KEY_RESPONSE" | jq -r '.key' 2>/dev/null)

if [ -z "$API_KEY" ] || [ "$API_KEY" = "null" ]; then
    echo -e "${RED}✗${NC} Failed to create API key"
    echo "Response: $API_KEY_RESPONSE"
    exit 1
else
    echo -e "${GREEN}✓${NC} API key created: ${API_KEY:0:20}..."
    export API_KEY
fi

# Test MCP endpoint
echo ""
echo "5. Testing MCP endpoint..."
MCP_RESPONSE=$(curl -s -X POST http://localhost:8000/mcp/query \
    -H "X-API-Key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"query": "test"}' \
    -w "\n%{http_code}")

HTTP_CODE=$(echo "$MCP_RESPONSE" | tail -1)
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "502" ] || [ "$HTTP_CODE" = "503" ]; then
    echo -e "${GREEN}✓${NC} MCP endpoint responded (HTTP $HTTP_CODE)"
    echo "   Note: 502/503 is expected if backend is not configured"
else
    echo -e "${YELLOW}⚠${NC} MCP endpoint returned HTTP $HTTP_CODE"
fi

# Test A2A endpoint
echo ""
echo "6. Testing A2A endpoint..."
A2A_RESPONSE=$(curl -s -X POST http://localhost:8000/a2a/message \
    -H "X-API-Key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"message": "test", "target_agent": "agent_b"}' \
    -w "\n%{http_code}")

HTTP_CODE=$(echo "$A2A_RESPONSE" | tail -1)
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "502" ] || [ "$HTTP_CODE" = "503" ]; then
    echo -e "${GREEN}✓${NC} A2A endpoint responded (HTTP $HTTP_CODE)"
else
    echo -e "${YELLOW}⚠${NC} A2A endpoint returned HTTP $HTTP_CODE"
fi

# Test test endpoints (no auth required)
echo ""
echo "7. Testing test endpoints (no auth)..."
TEST_HTTP=$(curl -s -w "\n%{http_code}" http://localhost:8000/test/http | tail -1)
if [ "$TEST_HTTP" = "200" ]; then
    echo -e "${GREEN}✓${NC} Test HTTP endpoint works"
else
    echo -e "${YELLOW}⚠${NC} Test HTTP endpoint returned HTTP $TEST_HTTP"
fi

# Test Python example
echo ""
echo "8. Testing Python example..."
if [ -f "examples/basic_usage.py" ]; then
    if [ -z "$PY" ]; then
        echo -e "${YELLOW}⚠${NC} Skipping Python example: no interpreter with 'requests' available."
        echo "   Hint: pip install requests, or set PYTHON=/path/to/python"
    else
        export API_KEY
        # Capture the full output — the success marker is on the last line.
        PYTHON_OUTPUT=$("$PY" examples/basic_usage.py 2>&1)
        if echo "$PYTHON_OUTPUT" | grep -qE "✓|completed"; then
            echo -e "${GREEN}✓${NC} Python example runs successfully"
        else
            echo -e "${YELLOW}⚠${NC} Python example output:"
            echo "$PYTHON_OUTPUT" | head -5
        fi
    fi
else
    echo -e "${YELLOW}⚠${NC} Python example file not found"
fi

# Summary
echo ""
echo "=============================="
echo -e "${GREEN}✓ Quick Start Test Complete!${NC}"
echo ""
echo "Your API Key: $API_KEY"
echo ""
echo "Try it yourself:"
echo "  export API_KEY=$API_KEY"
echo "  curl -X POST http://localhost:8000/mcp/query \\"
echo "    -H \"X-API-Key: \$API_KEY\" \\"
echo "    -H \"Content-Type: application/json\" \\"
echo "    -d '{\"query\": \"Hello!\"}'"
echo ""
echo "Access Admin Console: http://localhost:3001"
echo "API Documentation: http://localhost:8000/docs"

