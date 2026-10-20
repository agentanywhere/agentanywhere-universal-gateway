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

> The gateway proxies everything under a protocol prefix to your backend.
> `/mcp/query` and `/a2a/message` below are the paths served by the sample
> backends in `samples/backends/`, and the request bodies are the samples'
> shapes. Use the paths and bodies your own backend expects.

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

> **Not working.** The `/ws/*` route currently refuses every connection,
> including ones that present a valid API key, and it has no relay to the
> backend. The example describes the intended interface.

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/endpoint?api_key=your-key');
ws.onmessage = (event) => console.log('Received:', event.data);
ws.send(JSON.stringify({message: 'Hello'}));
```

### gRPC

> **Not working.** The JSON-to-gRPC bridge does not forward calls to a backend.
> With the stock requirements the request below answers HTTP 200 with
> `"status": "unsupported"`. The example describes the intended interface.

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

1. Start the gateway (see the [README](../README.md) or
   [quick start](../docs/QUICKSTART.md)); with Docker: `docker compose up -d --build`
2. Open the admin console at http://localhost:3001 and sign in as `admin` with
   your `ADMIN_TOKEN` (or `SECRET_KEY` if `ADMIN_TOKEN` is empty)
3. Go to the API Keys section and create a key, or use
   `POST /admin/api-keys` as in the quick start
4. Copy the key and use it in the examples

## More Examples

See the [`cookbooks/`](../cookbooks/) directory for integration recipes:
- [LangGraph](../cookbooks/langgraph_integration.py)
- [CrewAI](../cookbooks/crewai_integration.py)


