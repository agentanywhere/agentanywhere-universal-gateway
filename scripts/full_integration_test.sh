#!/bin/bash
# Full integration test with sample backends

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

GATEWAY_URL="http://localhost:8000"
# The admin credential: ADMIN_TOKEN, or SECRET_KEY if ADMIN_TOKEN is not set.
# There is no built-in admin token.
ADMIN_TOKEN="${ADMIN_TOKEN:-${SECRET_KEY:?Set ADMIN_TOKEN (or SECRET_KEY) to the gateway admin credential}}"

echo -e "${BLUE}🚀 Full Integration Test - Gateway + Sample Backends${NC}"
echo "=================================================="
echo ""

# Check services
echo "1. Checking services..."
GATEWAY_OK=false
if curl -s "$GATEWAY_URL/health" > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Gateway is running"
    GATEWAY_OK=true
else
    echo -e "${RED}✗${NC} Gateway is not running"
    exit 1
fi

BACKENDS_OK=true
for backend in "MCP:8001" "A2A:8002" "HTTP:8003" "WebSocket:8080"; do
    name=$(echo $backend | cut -d: -f1)
    port=$(echo $backend | cut -d: -f2)
    if curl -s "http://localhost:$port/health" > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} $name backend is running"
    else
        echo -e "${RED}✗${NC} $name backend is not running"
        BACKENDS_OK=false
    fi
done

if [ "$BACKENDS_OK" = false ]; then
    echo ""
    echo -e "${YELLOW}⚠${NC} Some backends are not running. Start them with:"
    echo "  docker compose --profile backends up -d"
    echo ""
fi

# Create API key
echo ""
echo "2. Creating API key..."
API_KEY_RESPONSE=$(curl -s -X POST "$GATEWAY_URL/admin/api-keys?name=integration-test&rate_limit_per_minute=1000" \
    -H "Authorization: Bearer $ADMIN_TOKEN")

API_KEY=$(echo "$API_KEY_RESPONSE" | jq -r '.key' 2>/dev/null)

if [ -z "$API_KEY" ] || [ "$API_KEY" = "null" ]; then
    echo -e "${RED}✗${NC} Failed to create API key"
    exit 1
fi

echo -e "${GREEN}✓${NC} API key created: ${API_KEY:0:30}..."
export API_KEY

# Test MCP
echo ""
echo "3. Testing MCP Protocol..."
MCP_RESPONSE=$(curl -s -X POST "$GATEWAY_URL/mcp/query" \
    -H "X-API-Key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"query": "What is the weather?", "context": {"user_id": "test123"}}')

if echo "$MCP_RESPONSE" | jq -e '.response' > /dev/null 2>&1; then
    RESPONSE_TEXT=$(echo "$MCP_RESPONSE" | jq -r '.response' | head -c 80)
    echo -e "${GREEN}✓${NC} MCP test passed"
    echo "   Response: $RESPONSE_TEXT..."
else
    ERROR=$(echo "$MCP_RESPONSE" | jq -r '.error // .message // "Unknown error"' 2>/dev/null)
    echo -e "${RED}✗${NC} MCP test failed: $ERROR"
fi

# Test A2A
echo ""
echo "4. Testing A2A Protocol..."
A2A_RESPONSE=$(curl -s -X POST "$GATEWAY_URL/a2a/message" \
    -H "X-API-Key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"message": "Hello from agent A", "target_agent": "agent_b", "source_agent": "agent_a"}')

if echo "$A2A_RESPONSE" | jq -e '.response' > /dev/null 2>&1; then
    RESPONSE_TEXT=$(echo "$A2A_RESPONSE" | jq -r '.response' | head -c 80)
    echo -e "${GREEN}✓${NC} A2A test passed"
    echo "   Response: $RESPONSE_TEXT..."
else
    ERROR=$(echo "$A2A_RESPONSE" | jq -r '.error // .message // "Unknown error"' 2>/dev/null)
    echo -e "${RED}✗${NC} A2A test failed: $ERROR"
fi

# Test HTTP
echo ""
echo "5. Testing HTTP/REST Protocol..."
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
else
    ERROR=$(echo "$HTTP_RESPONSE" | jq -r '.error // .message // "Unknown error"' 2>/dev/null)
    echo -e "${RED}✗${NC} HTTP test failed: $ERROR"
fi

# Summary
echo ""
echo "=================================================="
echo -e "${BLUE}📊 Test Summary${NC}"
echo ""

SUCCESS_COUNT=0
TOTAL_COUNT=3

if echo "$MCP_RESPONSE" | jq -e '.response' > /dev/null 2>&1; then
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
fi
if echo "$A2A_RESPONSE" | jq -e '.response' > /dev/null 2>&1; then
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
fi
if echo "$HTTP_RESPONSE" | jq -e '.id' > /dev/null 2>&1; then
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
fi

if [ $SUCCESS_COUNT -eq $TOTAL_COUNT ]; then
    echo -e "${GREEN}✅ All tests passed! ($SUCCESS_COUNT/$TOTAL_COUNT)${NC}"
    exit 0
else
    echo -e "${YELLOW}⚠️  Some tests failed ($SUCCESS_COUNT/$TOTAL_COUNT passed)${NC}"
    echo ""
    echo "Check backend URLs in .env file:"
    echo "  MCP_BACKEND_URL should be: http://mcp-backend:8001"
    echo "  A2A_BACKEND_URL should be: http://a2a-backend:8002"
    echo "  HTTP_BACKEND_URL should be: http://http-backend:8003"
    exit 1
fi


