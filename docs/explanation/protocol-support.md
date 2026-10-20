# Protocol support

AgentAnywhere Universal Gateway puts one authentication, rate-limiting and
logging layer in front of several protocols. This page says, for each one,
what the gateway actually does with it and how well that is established.

Status means:

- **Working**: requests are authenticated, limited and forwarded to the
  configured backend, and responses come back.
- **Partial**: some of the protocol's surface works through the gateway, and
  some does not.
- **Not working**: the route exists, but requests do not reach a backend.

Evidence names the tests in `tests/` and, where it applies, what was run for
this documentation. "Earlier manual check" refers to the hand checks recorded
in the previous edition of the README, against the sample backends on
Python 3.11; they were not repeated for this page.

## Contents

- [Summary](#summary)
- [HTTP / REST](#http--rest)
- [MCP](#mcp)
- [A2A](#a2a)
- [AG-UI and ACP](#ag-ui-and-acp)
- [GraphQL](#graphql)
- [MQTT](#mqtt)
- [WebSocket](#websocket)
- [gRPC](#grpc)
- [Agent edge](#agent-edge)
- [Plugin protocols](#plugin-protocols)

## Summary

| Protocol | Gateway route | Backend setting | Status | Evidence |
|---|---|---|---|---|
| HTTP / REST | `/api/*`, `/rest/*`, `/http/*`, and any path without a known prefix | `HTTP_BACKEND_URL` | Working | `tests/test_gateway.py`; run in process for the tutorials; earlier manual check |
| MCP | `/mcp/*`, `/api/mcp/*` | `MCP_BACKEND_URL` | Working (HTTP transport) | `tests/test_gateway.py::test_mcp_routing`, `tests/test_tier1_protocols.py`; run in process for the tutorials, including the prompt-injection block; earlier manual check |
| A2A | `/a2a/*`, `/api/a2a/*` | `A2A_BACKEND_URL` | Working (HTTP and SSE) | `tests/test_gateway.py::test_a2a_routing`, `tests/test_tier1_protocols.py`; earlier manual check |
| AG-UI | `/agui/*`, `/api/agui/*` | `AG_UI_BACKEND_URL` | Working (HTTP and SSE) | `tests/test_tier2_protocols.py`; earlier manual check |
| ACP | `/acp/*`, `/api/acp/*` | `ACP_BACKEND_URL` | Working (HTTP and SSE) | `tests/test_tier2_protocols.py`; earlier manual check |
| GraphQL | `POST /graphql` | `GRAPHQL_BACKEND_URL` | Working (queries over HTTP; no subscriptions) | `tests/test_gateway.py::test_graphql_routing`, `tests/test_protocols.py`; earlier manual check |
| MQTT | `POST /mqtt/publish`, `POST /mqtt/subscribe` | `MQTT_BROKER_URL` | Partial: publish works; subscribe registers a subscription but messages are not delivered to the HTTP caller | `tests/test_protocols.py`; earlier manual check of publish |
| WebSocket | `/ws/*` | `WEBSOCKET_BACKEND_URL` | Not working | Reproduced for this page: every connection is closed with 1008 |
| gRPC | `/grpc*`, `/api/grpc*` | `GRPC_BACKEND_URL` | Not working | `tests/test_protocols.py::test_call_unary_does_not_fabricate_success`; earlier manual check |
| Agent edge | `/agents/<slug>/*` (HTTP and WebSocket) | `AGENT_EDGE_ENABLED`, `AGENT_ROUTES_JSON`, or `/admin/agents` | Working, without gateway credentials (off by default) | `tests/test_agent_edge_proxy.py`, `tests/test_agent_routes.py` |
| Plugin protocols | Any prefix a `ProtocolPlugin` claims | Plugin | Working (request and response, no streaming) | `tests/test_plugins.py` |

## HTTP / REST

The catch-all route forwards the method, path, query string, headers and body
to `HTTP_BACKEND_URL` and returns the response. Paths that match no protocol
prefix also go there, or to `MCP_BACKEND_URL` when `HTTP_BACKEND_URL` is unset.
Responses are buffered, apart from the streaming cases below.

## MCP

The gateway does not implement MCP itself; it proxies the HTTP transport of an
MCP server. For a `POST` under `/mcp` with a body, the **MCP boundary**
(`app/tier1_mcp.py`) runs first, unless `MCP_BOUNDARY_ENABLED=false`:

- the body must not exceed `MCP_BOUNDARY_MAX_BODY_BYTES` (2 MiB by default)
  and must be valid UTF-8 JSON;
- JSON-RPC methods can be restricted with `MCP_BOUNDARY_ALLOWED_METHODS` and
  `MCP_BOUNDARY_BLOCKED_METHODS`;
- string values are scanned for prompt-injection patterns (a built-in list, or
  `MCP_BOUNDARY_INJECTION_PATTERNS`), and a match is blocked with 400 when
  `MCP_BOUNDARY_PROMPT_INJECTION_ACTION=block` (the default);
- `tools/call` arguments are validated against the input schema of tools
  synthesised from registered OpenAPI documents.

The pattern list is a heuristic. It blocks phrases such as "ignore previous
instructions" and is easy to evade; it is not a defence against prompt
injection.

## A2A

Proxied like HTTP. For `POST` bodies on send-message paths, the A2A boundary
(`app/tier1_a2a.py`) checks the message shape when
`A2A_ENFORCE_SEND_MESSAGE_SHAPE=true`. A path containing `message:stream` is
streamed. `/.well-known/agent-card.json` is a public A2A Agent Card built from
registered OpenAPI documents.

## AG-UI and ACP

The gateway strips the `/agui` or `/acp` prefix before forwarding, so
`POST /agui` reaches the backend as `POST /` and `POST /acp/runs` as
`POST /runs`. Requests are streamed (chunk by chunk, with
`TIER2_STREAM_CONNECT_TIMEOUT` and `TIER2_STREAM_READ_TIMEOUT`) when:

- AG-UI: the client sends `Accept: text/event-stream`, or `?sse=1`;
- ACP: a `POST` to a path containing `/runs` with `Accept: text/event-stream`,
  or any request to a `/runs` path with `?stream=1`.

Otherwise the response is buffered. Payload checks for both are in
`app/tier2_acp.py` and `app/tier2_ag_ui.py`.

## GraphQL

`POST /graphql` takes `{"query", "variables", "operationName"}` and posts it
to `GRAPHQL_BACKEND_URL`. Without a backend it answers 503. `/api/graphql` is
not handled by the GraphQL handler; it goes to the HTTP backend like any other
`/api/*` path. The sample GraphQL backend listens on port 8006, but
`env.example` points at port 4000.

## MQTT

`POST /mqtt/publish` with `{"topic", "payload", "qos"}` publishes to the broker
and answers `{"status": "published", "topic": ...}`. The client connects once,
when the gateway starts; if the broker is not up then, every call answers 503
until the gateway restarts. `POST /mqtt/subscribe` subscribes the gateway's
client to a topic and answers `{"status": "subscribed", ...}`, but received
messages are not returned to HTTP callers; the gateway only writes them to its
log as `mqtt_message_received`.

## WebSocket

`/ws/{path}` is meant to authenticate an API key from `?api_key=` or
`X-API-Key` and relay frames to `WEBSOCKET_BACKEND_URL`. It closes every
connection: with no key, code 1008 `API key required`; with a valid key, code
1008 `Authentication failed`, because the route passes a stand-in request
object without `state` to the authentication code. If `WEBSOCKET_BACKEND_URL`
is unset, it closes with 1003. Use the agent edge, or connect to the backend
directly.

## gRPC

Requests whose path starts with `grpc` or `api/grpc` go to a JSON-to-gRPC
bridge (`app/protocols/grpc_handler.py`) that takes
`{"service", "method", "data"}`. It needs gRPC server reflection
(`grpc_reflection`, which is not in `requirements.txt`) or the sample
`greeter_pb2` modules. With the stock requirements it answers HTTP 200 with
`"status": "unsupported"`. It is not a gRPC proxy: gRPC clients cannot connect
to the gateway.

## Agent edge

`/agents/<slug>/...` forwards HTTP (streamed) and WebSocket traffic to the
upstream registered for the slug, with the `/agents/<slug>` prefix removed. It
applies **no** gateway API key, rate limit or quota, and forwards
`Authorization` and cookies; the upstream must authenticate its own callers.
An unknown slug answers 404 `AGENT_NOT_FOUND`. Agents are registered with
`AGENT_ROUTES_JSON` or `/admin/agents`.

## Plugin protocols

A `ProtocolPlugin` claims a path prefix. Its `handle()` method receives the
request context and returns a dict, which the gateway sends as JSON with
status 200. See [Write a plugin](../how-to/write-a-plugin.md).
