# AgentAnywhere data plane (experimental)

A Rust proxy that sits in front of the Python gateway and handles the hot path itself: it authenticates API keys
(sent in the key header or as `Authorization: Bearer`), refuses requests without a working one, applies per-key rate limits, path scopes, IP rules, governance policies and tenant quotas, and forwards plain HTTP,
MCP, A2A, AG-UI and ACP requests, each to its own backend, streaming bodies and event streams in both directions.
Everything else is handed to the Python gateway unchanged. Design, goals and phases: [DESIGN.md](DESIGN.md).

**Status: phases 1 and 2 in part, not for production.** The limits below are real; read them before using it.

## Build and run

```bash
cd dataplane
cargo build --release
```

Start the Python gateway on an internal port with a shared secret and the data plane's address as a trusted proxy, then
the data plane on the public port:

```bash
DATAPLANE_TOKEN=change-me TRUSTED_PROXIES=127.0.0.1 uvicorn app.main:app --port 8001

./target/release/dataplane --listen 0.0.0.0:8000 \
    --backend http://127.0.0.1:9100 --control http://127.0.0.1:8001 --control-token change-me
```

`--backend` is the HTTP backend, `--control` the Python gateway. The MCP, A2A, AG-UI and ACP backends are the ones
the Python gateway is configured with (`MCP_BACKEND_URL`, `A2A_BACKEND_URL`, `AG_UI_BACKEND_URL`, `ACP_BACKEND_URL`);
they arrive with the snapshot, so they are set in one place. The token can also come from `DATAPLANE_TOKEN`.
`/internal/dataplane` is served only when the token is set, and the data plane answers `404` to any client that asks
for `/internal/...`. Tunables: `--sync-interval-ms` (default 500), `--max-staleness-ms` (default 5000), `--workers N` (default: one per
core) and `--access-log` (off by default).

## Threads

Each worker is one OS thread with its own single-threaded runtime, its own connections to the backend and its own
report counters, so a request never crosses cores and workers share no locks on the hot path. On Linux every worker has
its own listening socket on the shared address (`SO_REUSEPORT`) and the kernel spreads new connections; elsewhere one
acceptor thread deals them out in turn. The conversation with the control plane has a thread of its own, so traffic
never delays hearing about a revoked key. Workers do not steal work from each other: give the process as many cores
as it has workers, or set `--workers` below the core count on a shared machine, or tail latency suffers.

## How the data plane stays correct

* **Keys come from the control plane.** Every interval it asks for anything newer than the revision it holds. The
  revision is a hash of the content, so a change made by any Python worker, however it was made, is noticed, and a
  quiet poll costs a few bytes. Keys arrive as SHA-256 digests only.
* **Changes take effect within about one interval.** In the conformance suite a revoked key is refused 0.4 s after
  the delete, a new key is served natively after 0.3 s, and a new IP rule or policy is enforced within the same time.
* **It fails closed.** Until the first answer, and again whenever no answer has arrived for `--max-staleness-ms`, the
  data plane serves nothing itself and hands every request to the Python gateway. A revocation it could not hear about is
  never honoured for longer than that limit.
* **Governance is decided natively, in the Python gateway's order:** the key, its allowed paths, IP rules, policies
  (`403 AUTHORIZATION_ERROR`), the key's rate limit (`429`), the tenant's quota (`429`), the circuit breaker (`503`),
  then the backend. Refusals have the Python gateway's bodies and headers, including `X-Tenant-ID`.
* **Tenant quotas hold across both planes.** The snapshot carries each tenant's count from the database for the current
  minute and hour; the data plane adds what it admitted since and reports completed requests back, where they are added
  to the same usage rows the console reads. The two planes can disagree by up to about one sync interval, and a request
  in flight already counts here, so under concurrency the data plane is the stricter side, never the looser.
* **A key is read where the Python gateway reads it:** the configured header (`API_KEY_HEADER`, `X-API-Key` by
  default), and when that is absent or empty, the `Authorization` header with `Bearer ` taken out.
* **Requests without a working credential are refused here, in Python's words.** A request with no credential gets
  the gateway's `401 AUTHENTICATION_ERROR` at once. A credential this side does not know proves nothing (the key may
  have been created a moment ago), so it is handed to the Python gateway; only when that answers with its "invalid key"
  401 does the data plane remember the credential (as a digest) and refuse it itself, for ten seconds, and only while
  the snapshot is unchanged and the link unbroken. So a key works the moment it is created, a key switched back on
  works again within about one sync interval, and a client hammering with a bad key costs the Python gateway one
  request every ten seconds. A key more than two seconds past its expiry is refused here too. The wording of both
  401s arrives with the snapshot. When an authentication plugin is enabled, every such request goes to Python.
* **Bodies are checked at the protocol boundary, and refused in Python's words.** Where the Python gateway looks
  inside a request body (MCP JSON-RPC envelopes, method allow and block lists and prompt-injection patterns; the A2A
  send-message shape; ACP run requests; AG-UI envelopes), the data plane reads the body and decides one thing: is it
  certain to pass? If so it is forwarded natively. If not, because it fails a check or because this side cannot judge
  it exactly as Python would, the whole request is handed to the Python gateway, which answers it. So a refusal always
  carries the Python gateway's status, message and details, and a difference between the two implementations can only
  cost speed. `conformance/boundary.py` checks the one property this rests on (nothing the Python checks refuse is
  passed here) over tens of thousands of generated bodies.
* **Each protocol has its backend and its breaker.** A path belongs to the protocol the Python router gives it
  (`/mcp`, `/a2a`, `/agui`, `/acp` and the same under `/api`), ACP and AG-UI backends are sent the path without the
  gateway's prefix, and `X-Protocol` names the protocol. Two protocols configured with one address share a circuit
  breaker, as they do in Python.
* **Anything it cannot reproduce goes to Python.** The snapshot marks a key `native_ok: false` when something about it
  cannot be reproduced exactly (a policy that matches on headers or uses a character class in its path, a scoped IPv6
  network, a key linked to more than one tenant), and turns native serving off for every path when dynamic routes,
  transformation rules or a request-altering plugin exist. A single request also goes to Python when its path is
  percent-escaped or not ASCII, its method is not one of the plain ones, or the client's address cannot be resolved
  the way Python resolves it. The stock `request-id` plugin is reproduced natively.
* **Native answers are labelled** with `X-Served-By: dataplane`, so you can see which plane answered.
* **What it served is reported back**, so the console, metrics, SLOs and logs stay complete:
  * Counts and latencies are aggregated per method, path pattern and status, in the same histogram buckets the Python
    gateway uses, and sent exactly once per `--report-interval-ms` (default 1000). The cost does not grow with traffic.
    The Python gateway applies them to `gateway_requests_total`, `gateway_request_duration_seconds`, the RED metrics, the
    SLO tracker and `gateway_rate_limit_hits_total`, so a request answered natively counts like any other.
    `gateway_dataplane_native_requests_total` shows how much traffic the data plane answered.
  * Individual request-log rows (what the console's log view reads) are sent for every error and for successes up to
    `--log-rows-per-second` (default 1000). Rows beyond the cap are not sent; they are counted in
    `gateway_dataplane_rows_skipped_total`, never silently lost. Rows go through the normal request-log sink.
  * If a report cannot be delivered, the counts are kept for the next one; the rows are dropped and counted.
  * Upstream failures feed `gateway_errors_total` by error code and type (`ConnectError`, `ReadTimeout`,
    `RemoteProtocolError`).
  * With `--access-log`, one JSON line per native request goes to stdout in the Python gateway's `http_access` shape.
    A writer thread does the output; if it falls behind, lines are dropped and the number is printed to stderr.
  * The breaker opening or closing and keys going over their limit ride along as events, and the Python gateway fires
    the same webhooks (`circuit_breaker_open`, `circuit_breaker_closed`, `rate_limit_exceeded`) and alerts for them.
* **The backend is protected the way Python protects it.** A circuit breaker with the gateway's settings
  (`CIRCUIT_BREAKER_ENABLED`, `CIRCUIT_BREAKER_FAILURE_THRESHOLD`, `CIRCUIT_BREAKER_TIMEOUT`, read from the snapshot)
  counts requests the backend did not answer; when it opens, requests get the gateway's `503 SERVICE_UNAVAILABLE`
  without being sent, and an unreachable backend gets its `502 BACKEND_ERROR`, both with the same bodies as Python.
  The data plane's breaker is its own: it does not share state with the Python workers' breakers.

`snapshot.json` (`--snapshot`) is still accepted to bootstrap or to run without a control plane, for tests and benchmarks.
Its format is the same as what the control plane serves; see `src/snapshot.rs`.

## What it handles itself

A request is served natively only when **all** of these hold: the link is fresh; the path is `/api/...`, or belongs to
MCP, A2A, AG-UI or ACP and that protocol has an `http://` backend configured; no `fallthrough_prefixes` entry matches;
the client is not a browser (`Accept: text/html`); `X-API-Version` or `Accept-Version` resolves to an active version
with no backend of its own; the key is in the snapshot, active, unexpired and `native_ok`; and, where the
gateway checks the body, the body has a `Content-Length`, fits in memory (4 MiB, or the MCP limit if lower) and is
certain to pass. On those same paths, a request with no credential, or with one the Python gateway has just refused,
is answered here with the gateway's 401. Anything else, including OAuth2 tokens, credentials seen for the first time,
a request from a web page in another origin (one with an `Origin` header), `/metrics`, `/admin`, `/oauth2`, GraphQL
and MQTT, goes to the Python gateway untouched, so its answer is the answer.

**HTTP/2.** A client that opens its connection with the HTTP/2 preface (no TLS; `curl --http2-prior-knowledge`, a
gRPC client, a proxy configured for it) is spoken to in HTTP/2. Its requests are decided exactly as HTTP/1 requests
are, and those that go to the Python gateway or to an HTTP backend go there as HTTP/1.1. Upgrading an HTTP/1
connection with `Upgrade: h2c` is not supported, and neither is WebSocket over HTTP/2.

The gateway's own health endpoints are answered here on a plain `GET`, each for what this side can vouch for:

* **`/live`** says this process is running, with its own process id. It needs nothing from the Python gateway, so
  restarting that one never makes this one look dead.
* **The health path** (`/health`, or wherever `HEALTH_CHECK_PATH` puts it) says healthy only while the Python gateway
  answered the latest sync. One failed sync and the question goes to the Python gateway itself.
* **`/ready`** gives the Python gateway's own answer, database probe included, as of the latest sync (every
  `--sync-interval-ms`). Not ready there is not ready here, with the same 503. After a failed sync it is asked directly.

With the Python gateway down, the health path and `/ready` therefore answer 502 and `/live` still answers 200.

**WebSocket.** A handshake for `/ws/...` is relayed here when `WEBSOCKET_BACKEND_URL` is a `ws://` address and the
key is certain to be let in: sent in `X-API-Key`, known, working and allowed that path, with no IP rule or policy
configured. The backend is asked for the rest of the path and the query, and sent only the handshake, the subprotocols
offered and the caller's `Authorization` and `Cookie`; never the gateway's key. Once it accepts, the connection is a
tunnel: bytes go both ways until either side closes. If it refuses or cannot be reached, the caller gets 403. Every
other handshake (a key in `?api_key=`, an unknown key, `/agents/<slug>/...`, any IP rule or policy in force) is tunnelled
to the Python gateway, which relays or refuses as it always does.

**The agent edge.** A request for `/agents/<slug>/...` is forwarded here when the agent is registered with a plain
`http://` upstream and the request would reach it exactly as the Python gateway sends it. As there, no gateway key is
asked for and no limit, IP rule or policy applies: the agent authenticates its own callers. The agent is sent the path
after the slug, the caller's headers (`Host`, `Authorization` and `Cookie` included) and the gateway's additions, and
its answer is streamed back as it comes. The Python gateway is handed the request when:

* the slug is not registered, or its upstream is `https://` (the 404 and the TLS are Python's);
* the caller is a browser (`Accept: text/html`), a web page in another origin, or asks for a WebSocket;
* the query is anything but `key=value` pairs of unreserved characters with no key twice (Python takes a query apart
  and writes it again), a header is given twice (Python forwards the last), or the path after the slug names an API version;
* the body has no `Content-Length` or is over 1 MiB;
* the agent cannot be reached. Nothing was sent, so Python tries, answers in its own way and counts the failure in
  its circuit breaker. This side keeps no breaker for agents.

An agent that hangs up before answering gets the Python gateway's 500 from here. A compressed answer is passed on
compressed with its `Content-Encoding`; the Python gateway decompresses it first. The caller reads the same either way.

**gRPC.** A call (HTTP/2, `POST`, content type `application/grpc` or `application/grpc+...`) is relayed to
`GRPC_BACKEND_URL` when that is a plain `host:port`, over HTTP/2 without TLS, with the path unchanged and the body and
trailers streamed both ways. The Python gateway cannot carry gRPC, so there is nobody to hand a call to, and the rule
here is the reverse of everywhere else: **whatever this side cannot be sure of is refused.**

* The key comes from the key header or from `authorization: Bearer`, and must be in the snapshot, active and
  unexpired. A key created less than a sync interval ago is refused until the next sync.
* The key's allowed paths (matched against `/package.Service/Method`), the IP rules, the policies, the key's rate
  limit, its tenant's quota and the backend's circuit breaker apply as for any native request.
* A key marked `native_ok: false`, which includes every key while dynamic routes, transformation rules or
  request-altering plugins exist, is refused: those are rules this side cannot apply.
* Refusals are gRPC statuses with the request id attached: `UNAUTHENTICATED` (no key, or one that does not work),
  `PERMISSION_DENIED` (scope, IP rule, policy, or rules that cannot be applied), `RESOURCE_EXHAUSTED` (rate limit or
  quota, with `retry-after`), `UNAVAILABLE` (snapshot not fresh, circuit open, backend unreachable), `UNIMPLEMENTED`
  (no gRPC backend configured). They are counted and logged under the HTTP status they stand for (401, 403, 429, 503, 502, 501).
* The backend is sent the caller's metadata, `x-forwarded-for` and `traceparent`, and never the gateway's key. An
  `authorization` that did not carry the key is the caller's own and goes through.
* Nothing bounds a call but the caller's deadline; `DEFAULT_TIMEOUT` does not apply, so a stream may stay open. Only
  connecting to the backend is bounded (10 s). A call the caller cancels is cancelled at the backend.
* A call that reaches the backend is counted with status 200 whatever gRPC status the backend ends it with, and
  under the protocol label `http`. gRPC-Web is not handled.

| Traffic | Served by |
|---|---|
| Plain HTTP under `/api/` | data plane |
| MCP (`/mcp`, `/api/mcp`), including the boundary checks | data plane; bodies that fail or cannot be judged: Python |
| A2A (`/a2a`, `/api/a2a`), including `message:stream` | data plane; same rule |
| AG-UI (`/agui`, `/api/agui`) and ACP (`/acp`, `/api/acp`), including event streams | data plane; same rule |
| MCP `tools/call` for a tool with a registered JSON Schema; custom injection patterns | Python |
| A key sent as `Authorization: Bearer <key>` | data plane |
| No credential; a credential Python refused in the last ten seconds; a key clearly past its expiry | data plane (401) |
| A credential seen for the first time; OAuth2 tokens; two credentials of which the key does not work | Python |
| Browsers (HTML error pages) | Python |
| WebSocket under `/ws/`, key in `X-API-Key`, no IP rules or policies | data plane |
| Any other WebSocket (`?api_key=`, `/agents/<slug>`, rules or policies in force) | Python, through a tunnel |
| gRPC calls (HTTP/2, `application/grpc`) | data plane only; refused when it cannot be sure |
| Any other request over HTTP/2 | as the same request over HTTP/1 |
| `/agents/<slug>/...` to a registered `http://` agent, from a client that is not a browser | data plane |
| `/agents/<slug>/...` otherwise (browsers, unknown slugs, `https://` agents, unusual requests) | Python |
| GraphQL, MQTT, the JSON-to-gRPC bridge under `/grpc` | Python |
| Dynamic routes, transformation rules, request-altering plugins (when any exist) | Python, for every path |
| The health path, `/ready` and `/live`, on a plain `GET` | data plane |
| A request with an `Origin` header (the cross-origin headers are Python's) | Python |
| `/health/detailed`, `/metrics`, `/oauth2`, `/admin` and the console | Python |

## Known gaps (why this is not production-ready)

* **Browsers reach agents through the Python gateway.** Any request that accepts `text/html` is handed over, because
  the error pages are Python's. For an agent with a web interface that is most of its traffic; API callers are served here.

* **A WebSocket relayed here is a tunnel, not two conversations.** The Python gateway ends the caller's WebSocket and
  opens its own to the backend, so it answers pings itself, sends the backend a keep-alive ping every 20 seconds, and
  takes no message above 16 MiB from the caller. Here pings, compression and message size are between the caller
  and the backend. Messages, subprotocol and close codes are the same either way.
* **A WebSocket is checked when it opens, not while it is open.** A key revoked afterwards keeps its open connections,
  here and in the Python gateway alike. Connections are not counted, limited or written to the request log by either.
* **`wss://` backends and WebSockets with IP rules or policies are relayed by the Python gateway**, not here.

* **`/ready` can be one sync interval behind** (500 ms by default), or as long as a sync that hangs takes to fail
  (five seconds), because it repeats the Python gateway's last answer instead of asking the database itself.
* **`/metrics` is still the Python gateway's.** It is the registry for the whole gateway, including everything this
  side reports to it, so there is one place to scrape. The data plane has no metrics endpoint of its own yet: with the
  Python gateway down, nothing can be scraped.
* **Calls from web pages in another origin are not fast.** Any request with an `Origin` header is handed to the Python
  gateway, which writes the cross-origin headers.

* **A refused credential stays refused for up to ten seconds after the Python gateway would accept it** in one case
  this side cannot see: the gateway was restarted with a new `SECRET_KEY` or OAuth2 state so quickly that no sync
  failed in between. Key changes made through the admin API are not affected (they change the snapshot).
* **Some MCP traffic is still judged by Python.** A string that contains one of four letters Python's
  case-insensitive matching treats as `i`, `s` or `k` (U+0130, U+0131, U+017F, U+212A), a method name that is not ASCII
  while a method list is configured, a `tools/call` for a tool with a JSON Schema, any MCP body when custom injection
  patterns are configured, and any checked body sent without a `Content-Length`: all correct, none fast.
* **The four protocols have not been benchmarked.** Reading and parsing a body to check it costs time the plain HTTP
  path does not spend; how much has not been measured.
* **Runtime changes held only in a Python worker's memory are not visible to the snapshot:** transformation rules,
  API version changes, plugins and the MCP tool registry are per process in the Python gateway. The snapshot reflects the worker that answers
  it, so with several workers, change them through configuration and restart, or run one worker, until these are stored.
* **Reporting is by aggregate and sample, not per request.** Counts, latency and SLOs are exact; the request-log view
  shows every error but only a capped number of successes at high rates. Each key's `last_used_at` is kept current.
* **Usage is reported at least once.** If the control plane applies a report and the acknowledgement is lost, that
  interval's tenant usage and counts are sent again and counted twice. The error is on the strict side for quotas.
* **AI anomaly scoring does not see native requests**, so they never carry `X-Anomaly-Detected` or `X-Anomaly-Score`.
* **A rate-limit webhook stands for a batch, not one request.** A native 429 fires the same webhook and alert, but
  once per key and window in each report, with a `count` of the requests refused, where Python fires one per request.
* **The upstream timeout covers the wait for the backend to start answering** (`DEFAULT_TIMEOUT`, from the snapshot);
  a response that starts and then stalls is not cut off.
* **Error type names are the Python client's nearest equivalent**, chosen from three (`ConnectError`, `ReadTimeout`,
  `RemoteProtocolError`); rarer httpx error types are not distinguished.
* **Rate-limit counters are shared by all threads**, so limits are exact; Python's are per worker process. The circuit
  breaker is the data plane's own.
* **Tail latency needs dedicated cores** (see Threads). On a machine shared with the load generator and the backend,
  p95 and p99 were worse than the earlier work-stealing build at low concurrency; with pinned cores on Linux they were
  equal or better. No comparison against other gateways has been run yet.
* The `Server` response header differs (uvicorn on one side), and native answers add `X-Served-By`.

## Tests

```bash
cargo test                              # unit tests
python dataplane/conformance/run.py     # same scenarios against Python and the data plane, plus the live-link
                                        # scenarios (needs the build above)
python dataplane/conformance/boundary.py  # the body checks against the Python functions, on generated bodies
```

The conformance suite is the definition of "same behaviour": a scenario must pass on both before the data plane may
handle that kind of request.
