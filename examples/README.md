# Examples

Client-side examples for calling AgentAnywhere Universal Gateway. They assume a
gateway on `http://localhost:8000`, an API key, and the sample backends from
[`samples/backends/`](../samples/backends/README.md). The
[getting-started tutorial](../docs/tutorials/getting-started.md) shows how to
get all three.

These files were not run for the current documentation. The request shapes
match the sample backends; your own backends define their own paths and
bodies.

## Files

| File | Shows | Needs |
|---|---|---|
| `basic_usage.py` | MCP, A2A, MQTT, HTTP, GraphQL and gRPC requests with `requests` | `pip install requests`; `export API_KEY=...` |
| `python_sdk_example.py` | The shape of a client like the one the SDK generator produces (`/admin/sdk/generate/python`) | `pip install requests` |
| `javascript_example.js` | The same requests from Node.js | `npm install axios ws` |

```bash
export API_KEY=<your key>
python examples/basic_usage.py
node examples/javascript_example.js
```

## What does not work

- **gRPC** (`/grpc/call`) answers HTTP 200 with `"status": "unsupported"`.
- **WebSocket** (`/ws/...`) closes every connection.

Both examples are kept to show the intended interface; see the
[protocol support matrix](../docs/explanation/protocol-support.md).

## The calls in brief

Every data-plane call carries the key in `X-API-Key`:

```python
import requests

GATEWAY = "http://localhost:8000"
HEADERS = {"X-API-Key": "your-key"}

# MCP (sample backend path)
requests.post(f"{GATEWAY}/mcp/query", headers=HEADERS,
              json={"query": "What is the weather?", "context": {"user_id": "123"}})

# A2A (sample backend path)
requests.post(f"{GATEWAY}/a2a/message", headers=HEADERS,
              json={"message": "Hello", "target_agent": "agent_b"})

# MQTT publish; the gateway answers {"status": "published", "topic": ...}
requests.post(f"{GATEWAY}/mqtt/publish", headers=HEADERS,
              json={"topic": "sensors/temperature", "payload": {"value": 25.5}, "qos": 1})

# GraphQL
requests.post(f"{GATEWAY}/graphql", headers=HEADERS, json={"query": "{ hello }"})
```

AG-UI and ACP responses are streamed when you send
`Accept: text/event-stream`; see
[protocol support](../docs/explanation/protocol-support.md#ag-ui-and-acp).

## More

- [Cookbooks](../cookbooks/README.md): LangGraph and CrewAI integration sketches.
- [HTTP API reference](../docs/reference/http-api.md).
