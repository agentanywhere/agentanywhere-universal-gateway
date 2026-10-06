# Testing Sample Backends

This guide shows how to test the sample backends with the gateway.

## Prerequisites

- Gateway running on port 8000
- Sample backends running (see README.md)

## Quick Test

```bash
# Start everything
docker-compose --profile backends up -d

# Wait for services to be ready
sleep 10

# Get API key
export API_KEY=$(curl -s -X POST "http://localhost:8000/admin/api-keys?name=test&rate_limit_per_minute=100" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq -r '.key')

echo "API Key: $API_KEY"
```

## Test Each Protocol

### MCP Protocol

```bash
# Test MCP query
curl -X POST http://localhost:8000/mcp/query \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "What is the weather?",
    "context": {"user_id": "user123"}
  }' | jq .

# Expected: Response with MCP-formatted data
```

### A2A Protocol

```bash
# Test A2A message
curl -X POST http://localhost:8000/a2a/message \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Hello from agent A",
    "target_agent": "agent_b",
    "source_agent": "agent_a",
    "metadata": {"priority": "high"}
  }' | jq .

# Expected: Response from target agent
```

### HTTP/REST Protocol

```bash
# Create a user
curl -X POST http://localhost:8000/api/users \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "John Doe",
    "email": "john@example.com"
  }' | jq .

# List users
curl http://localhost:8000/api/users \
  -H "X-API-Key: $API_KEY" | jq .

# Get specific user
curl http://localhost:8000/api/users/user_1 \
  -H "X-API-Key: $API_KEY" | jq .
```

### WebSocket Protocol

```javascript
// In browser console or Node.js
const API_KEY = 'your-api-key-here';
const ws = new WebSocket(`ws://localhost:8000/ws/endpoint?api_key=${API_KEY}`);

ws.onopen = () => {
  console.log('Connected!');
  ws.send(JSON.stringify({message: 'Hello WebSocket'}));
};

ws.onmessage = (event) => {
  console.log('Received:', JSON.parse(event.data));
};

ws.onerror = (error) => {
  console.error('Error:', error);
};
```

### MQTT Protocol

```bash
# Publish message
curl -X POST http://localhost:8000/mqtt/publish \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "sensors/temperature",
    "payload": {
      "value": 25.5,
      "unit": "celsius",
      "timestamp": "2025-12-03T10:00:00Z"
    },
    "qos": 1
  }' | jq .

# Subscribe (requires MQTT client)
mosquitto_sub -h localhost -t sensors/temperature
```

## Verify Backend Health

```bash
# Check all backend health endpoints
echo "MCP Backend:"
curl -s http://localhost:8001/health | jq .

echo "A2A Backend:"
curl -s http://localhost:8002/health | jq .

echo "HTTP Backend:"
curl -s http://localhost:8003/health | jq .

echo "WebSocket Backend:"
curl -s http://localhost:8080/health | jq .
```

## Test Through Gateway

All requests should go through the gateway (port 8000), not directly to backends. The gateway will:
1. Authenticate the request
2. Apply rate limiting
3. Route to the appropriate backend
4. Log the request
5. Return the response

## Troubleshooting

### Backends not responding

```bash
# Check if backends are running
docker-compose ps

# Check backend logs
docker-compose logs mcp-backend
docker-compose logs a2a-backend
docker-compose logs http-backend
```

### Gateway returning 502

- Check backend URLs in `.env` file
- Verify backends are healthy
- Check gateway logs: `docker-compose logs gateway`

### Authentication errors

- Verify API key is correct
- Check API key is active in admin console
- Ensure `X-API-Key` header is set

## Next Steps

- Modify backends to match your use case
- Add more endpoints
- Integrate with your existing services
- See `examples/` for more integration examples


