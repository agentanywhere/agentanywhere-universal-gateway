# Examples

This directory contains example code demonstrating how to use the AgentAnywhere Universal Gateway.

## Quick Start Examples

### Python

```bash
# Install dependencies
pip install requests

# Run basic examples
python examples/basic_usage.py

# Run SDK example
python examples/python_sdk_example.py
```

### JavaScript/Node.js

```bash
# Install dependencies
npm install axios ws

# Run examples
node examples/javascript_example.js
```

## Example Files

- **`basic_usage.py`** - Basic Python examples for all protocols
- **`python_sdk_example.py`** - Using the auto-generated Python SDK
- **`javascript_example.js`** - Node.js examples for all protocols

## Protocol Examples

### MCP (Model Context Protocol)

```python
import requests

response = requests.post(
    "http://localhost:8000/mcp/query",
    headers={"X-API-Key": "your-key"},
    json={"query": "What is the weather?", "context": {"user_id": "123"}}
)
```

### A2A (Agent-to-Agent)

```python
response = requests.post(
    "http://localhost:8000/a2a/message",
    headers={"X-API-Key": "your-key"},
    json={"message": "Hello", "target_agent": "agent_b"}
)
```

### MQTT

```python
response = requests.post(
    "http://localhost:8000/mqtt/publish",
    headers={"X-API-Key": "your-key"},
    json={
        "topic": "sensors/temperature",
        "payload": {"value": 25.5},
        "qos": 1
    }
)
```

### WebSocket

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/endpoint?api_key=your-key');
ws.onmessage = (event) => console.log('Received:', event.data);
ws.send(JSON.stringify({message: 'Hello'}));
```

### gRPC

```python
response = requests.post(
    "http://localhost:8000/grpc/call",
    headers={"X-API-Key": "your-key"},
    json={"service": "Greeter", "method": "SayHello", "data": {"name": "world"}}
)
```

### GraphQL

```python
response = requests.post(
    "http://localhost:8000/graphql",
    headers={"X-API-Key": "your-key"},
    json={"query": "{ hello }"}
)
```

> AG-UI and ACP surfaces are streamed — send `Accept: text/event-stream` to
> `/agui/...` or POST to an ACP `/acp/.../runs` path. See
> [docs/API_REFERENCE.md](../docs/API_REFERENCE.md).

## Getting Your API Key

1. Start the gateway: `docker-compose up -d`
2. Access admin console: http://localhost:3001
3. Navigate to API Keys section
4. Create a new API key
5. Copy the key and use it in examples

## More Examples

See the [`cookbooks/`](../cookbooks/) directory for integration recipes:
- [LangGraph](../cookbooks/langgraph_integration.py)
- [CrewAI](../cookbooks/crewai_integration.py)


