#!/bin/bash
# Comprehensive test script for gateway with sample backends

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

GATEWAY_URL="http://localhost:8000"
# The admin credential: ADMIN_TOKEN, or SECRET_KEY if ADMIN_TOKEN is not set.
# There is no built-in admin token.
ADMIN_TOKEN="${ADMIN_TOKEN:-${SECRET_KEY:?Set ADMIN_TOKEN (or SECRET_KEY) to the gateway admin credential}}"

echo "🧪 Testing Gateway with Sample Backends"
echo "========================================"
echo ""

# Check gateway
echo "1. Checking gateway..."
if curl -s "$GATEWAY_URL/health" > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Gateway is running"
else
    echo -e "${RED}✗${NC} Gateway is not running. Start it first: docker-compose up -d"
    exit 1
fi

# Check backends
echo ""
echo "2. Checking sample backends..."
BACKENDS=(
    "MCP:8001"
    "A2A:8002"
    "HTTP:8003"
    "WebSocket:8080"
)

ALL_BACKENDS_OK=true
for backend in "${BACKENDS[@]}"; do
    name=$(echo $backend | cut -d: -f1)
    port=$(echo $backend | cut -d: -f2)
    if curl -s "http://localhost:$port/health" > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} $name backend (port $port) is running"
    else
        echo -e "${RED}✗${NC} $name backend (port $port) is not running"
        ALL_BACKENDS_OK=false
    fi
done

if [ "$ALL_BACKENDS_OK" = false ]; then
    echo ""
    echo -e "${YELLOW}⚠${NC} Some backends are not running. Start them with:"
    echo "  cd samples/backends && bash start_all.sh"
    echo "  Or: docker-compose --profile backends up -d"
    echo ""
    read -p "Continue testing anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Create API key
echo ""
echo "3. Creating API key..."
API_KEY_RESPONSE=$(curl -s -X POST "$GATEWAY_URL/admin/api-keys?name=integration-test&rate_limit_per_minute=1000" \
    -H "Authorization: Bearer $ADMIN_TOKEN")

API_KEY=$(echo "$API_KEY_RESPONSE" | jq -r '.key' 2>/dev/null)

if [ -z "$API_KEY" ] || [ "$API_KEY" = "null" ]; then
    echo -e "${RED}✗${NC} Failed to create API key"
    echo "Response: $API_KEY_RESPONSE"
    exit 1
fi

echo -e "${GREEN}✓${NC} API key created: ${API_KEY:0:30}..."
export API_KEY

# Test MCP
echo ""
echo "4. Testing MCP Protocol..."
MCP_RESPONSE=$(curl -s -X POST "$GATEWAY_URL/mcp/query" \
    -H "X-API-Key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"query": "Hello MCP", "context": {"user_id": "test123"}}')

if echo "$MCP_RESPONSE" | jq -e '.response' > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} MCP test passed"
    echo "   Response: $(echo "$MCP_RESPONSE" | jq -r '.response' | head -c 60)..."
elif echo "$MCP_RESPONSE" | jq -e '.error' > /dev/null 2>&1; then
    echo -e "${YELLOW}⚠${NC} MCP test returned error: $(echo "$MCP_RESPONSE" | jq -r '.error')"
else
    echo -e "${RED}✗${NC} MCP test failed"
    echo "   Response: $MCP_RESPONSE"
fi

# Test A2A
echo ""
echo "5. Testing A2A Protocol..."
A2A_RESPONSE=$(curl -s -X POST "$GATEWAY_URL/a2a/message" \
    -H "X-API-Key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"message": "Hello from agent A", "target_agent": "agent_b", "source_agent": "agent_a"}')

if echo "$A2A_RESPONSE" | jq -e '.response' > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} A2A test passed"
    echo "   Response: $(echo "$A2A_RESPONSE" | jq -r '.response' | head -c 60)..."
elif echo "$A2A_RESPONSE" | jq -e '.error' > /dev/null 2>&1; then
    echo -e "${YELLOW}⚠${NC} A2A test returned error: $(echo "$A2A_RESPONSE" | jq -r '.error')"
else
    echo -e "${RED}✗${NC} A2A test failed"
    echo "   Response: $A2A_RESPONSE"
fi

# Test HTTP
echo ""
echo "6. Testing HTTP/REST Protocol..."
HTTP_RESPONSE=$(curl -s -X POST "$GATEWAY_URL/api/users" \
    -H "X-API-Key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"name": "Test User", "email": "test@example.com"}')

if echo "$HTTP_RESPONSE" | jq -e '.id' > /dev/null 2>&1; then
    USER_ID=$(echo "$HTTP_RESPONSE" | jq -r '.id')
    echo -e "${GREEN}✓${NC} HTTP test passed - User created: $USER_ID"
    
    # Test GET
    GET_RESPONSE=$(curl -s "$GATEWAY_URL/api/users/$USER_ID" -H "X-API-Key: $API_KEY")
    if echo "$GET_RESPONSE" | jq -e '.id' > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} HTTP GET test passed"
    fi
elif echo "$HTTP_RESPONSE" | jq -e '.error' > /dev/null 2>&1; then
    echo -e "${YELLOW}⚠${NC} HTTP test returned error: $(echo "$HTTP_RESPONSE" | jq -r '.error')"
else
    echo -e "${RED}✗${NC} HTTP test failed"
    echo "   Response: $HTTP_RESPONSE"
fi

# Test WebSocket (basic check)
echo ""
echo "7. Testing WebSocket Protocol..."
WS_HEALTH=$(curl -s "http://localhost:8080/health" | jq -r '.status' 2>/dev/null)
if [ "$WS_HEALTH" = "healthy" ]; then
    echo -e "${GREEN}✓${NC} WebSocket backend is healthy"
    echo "   Note: Full WebSocket test requires browser/client connection"
else
    echo -e "${YELLOW}⚠${NC} WebSocket backend health check failed"
fi

# Summary
echo ""
echo "========================================"
echo -e "${GREEN}✅ Testing Complete!${NC}"
echo ""
echo "API Key: $API_KEY"
echo ""
echo "Try more tests:"
echo "  export API_KEY=$API_KEY"
echo "  curl -X POST $GATEWAY_URL/mcp/query \\"
echo "    -H \"X-API-Key: \$API_KEY\" \\"
echo "    -H \"Content-Type: application/json\" \\"
echo "    -d '{\"query\": \"Your query here\"}'"
echo ""
echo "View logs:"
echo "  docker-compose logs gateway"
echo "  docker-compose --profile backends logs"


