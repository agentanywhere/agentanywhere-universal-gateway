# A Rust data plane for the gateway: design proposal

**Status:** proposal, for review. No product code yet.
**Goal:** make the gateway the fastest in its class without giving up what makes it useful: one place for
authentication, limits, policy and logging in front of several protocols.

## Why

The Python gateway's per-request cost is dominated by the language runtime and the web stack, not by our own logic.
Profiling the tuned gateway showed our own code is under a tenth of the CPU, with the rest in the ASGI stack and the HTTP
client. After the fixes already merged, the remaining wins in Python are small and risky. Throughput that matters for a
gateway (many thousands of requests per second per node, tight tail latency) needs a compiled data path.

## Shape: a Rust data plane in front, Python control plane behind

```
 client ──► Rust data plane (public port) ──► backend
                 │  ▲
        snapshot │  │ logs, usage
         + events▼  │
            Python control plane (internal port)
            admin API, console, OAuth2, policies, analytics, migrations
```

* The **data plane** owns the public port. It authenticates, rate-limits, enforces IP rules and quotas, forwards, and
  emits one log record per request.
* The **control plane** is today's Python gateway, moved to an internal port. It keeps everything that is not on the hot
  path, and it is where configuration lives.
* **Anything the data plane cannot handle yet is forwarded to the control plane unchanged.** This is the strangler
  pattern: behaviour stays identical, and the share handled natively grows phase by phase. No big-bang rewrite, and the
  Python gateway keeps working on its own at every step.

## What the data plane must reproduce exactly

These are the semantics of the Python gateway today. Each becomes a conformance test (see below).

| Concern | Today's behaviour |
|---|---|
| API key lookup | Key sent in `X-API-Key` or `Authorization: Bearer`. Stored and cached by SHA-256 hex digest; the plaintext key is never kept. Constant-time comparison where plaintext is compared. Inactive and expired keys are rejected. |
| Revocation | Deleting, disabling or changing a key takes effect on the next request (cache invalidation), not after a TTL. |
| Rate limits | Per key, fixed windows of 1 minute and 1 hour, limits from the key (or defaults). Exceeding gives `429` with `Retry-After`. |
| Tenant quota | Per tenant, minute and hour windows, counters incremented atomically. |
| IP rules | Rules scoped global, tenant or key. A rule applies only to requests in its scope. |
| Routing | Path prefix selects backend and protocol (`/mcp`, `/a2a`, `/agui`, `/acp`, `/graphql`, plain HTTP, and the dynamic route registry and `/agents/<slug>` edge). |
| Errors | JSON body `{error, message, request_id, timestamp, details}`; an HTML page for browsers. Stable error codes. |
| Headers | `X-Request-ID` and `traceparent` on every response; incoming `traceparent` honoured. |
| Resilience | Circuit breaker per backend; `503` when open. |
| Request log | One row per finished request: request id, method, path, client IP, api key id, tenant id, status, response time, user agent, error, protocol. |

## How the two planes talk

* **Configuration snapshot.** The control plane serves the data plane its working set: keys (hashes and limits only),
  tenants and quotas, IP rules, routes and backends, rate-limit defaults. The data plane loads it at start and keeps it in
  memory.
* **Change events.** Key, tenant, rule or route changes are pushed over a long-lived stream so a revocation takes effect
  immediately. If the stream drops, the data plane re-fetches the snapshot and, until it has, **fails closed** for
  anything it cannot confirm.
* **Logs and usage.** The data plane batches request-log records and usage counters and sends them to the control
  plane, which writes them through the existing request-log sink. Bounded queue, drop-newest and count when full, as today.
* **Trust.** The internal channel is a private port or Unix socket with a shared secret. It is never exposed publicly.

## Phases

1. **Hot path.** Listener; health, ready, live and metrics; key authentication from the snapshot; rate limits; quota; IP
   rules; plain HTTP proxying with streaming, per-backend circuit breakers and trace headers; request-log emission.
   Everything else falls through to the control plane. Ships behind a switch, off by default.
2. **Move policy.** Transformations, MCP injection checks and governance policies, one at a time, each with conformance
   tests against the Python implementation.
3. **Protocols Python cannot carry.** WebSocket and gRPC pass-through (the Python gateway has routes for both but does not
   forward them today), then MQTT and GraphQL specifics.
4. **Decide on the rest.** The admin API, OAuth2 and console backend can stay in Python indefinitely; they are not on the
   hot path. Revisit only if there is a reason.

## Proving it is the same

A **conformance suite** runs one list of HTTP scenarios (missing key, wrong key, revoked key, expired key, rate-limited,
over quota, blocked IP, each protocol prefix, backend down, circuit open, streaming) against the Python gateway and the
data plane, and compares status codes, error codes and the headers listed above. A scenario passing on both is the
definition of "same behaviour". New behaviour requires a scenario first.

## Risks and how they are handled

* **Two languages to maintain.** Mitigated by keeping the Rust surface small in phase 1 and by the strangler design:
  the control plane stays Python.
* **State shared across data-plane instances** (rate-limit counters, quota). Single node: in memory. Several nodes: the
  same Redis the Python gateway already supports.
* **Revocation latency.** Event stream plus fail-closed re-sync, covered by a conformance scenario.
* **Parity drift.** The conformance suite runs in CI against both planes.
* **Memory safety and supply chain.** Rust removes a class of bugs in a proxy that parses untrusted input; dependencies
  are pinned and audited (`cargo audit` and `cargo deny` in CI).

## Open questions (need a decision before phase 1)

1. **Where does the code live?** Proposal: a `dataplane/` crate in this repository, Apache-2.0, built in CI next to the
   Python code. The alternative is a separate repository.
2. **Other control planes.** Proposal: the data plane depends only on the snapshot and event interface, so any
   control plane that provides that interface can drive it.
3. **Deployment.** Proposal: the data plane takes the public port and the Python gateway moves to an internal port, in
   Docker Compose and the quick start, with a single `docker compose up` unchanged for users.
4. **Target.** What request rate and p99 should one node reach? That sets how much of phase 2 is on the critical path.
5. **Ownership.** Who reviews and maintains the Rust code.
