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
the Python gateway is configured with (`MCP_BACKEND_URL`, `A2A_BACKEND_URL`, `AG_UI_BACKEND_URL`, `ACP_BACKEND_URL`, `GRAPHQL_BACKEND_URL`);
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
  gateway's prefix, a GraphQL backend the path without `/api`, and `X-Protocol` names the protocol. Two protocols configured with one address share a circuit
  breaker, as they do in Python.
* **Anything it cannot reproduce goes to Python.** The snapshot marks a key `native_ok: false` when something about it
  cannot be reproduced exactly (a policy that matches on headers or uses a character class in its path, a scoped IPv6
  network, a key linked to more than one tenant), and turns native serving off for every path when
  a request-altering plugin exists, or a transformation rule whose reach this side cannot work out. Dynamic routes
  are followed here; a transformation rule is applied here when this side can do exactly what it does, and otherwise sends only the requests it applies to (see below). A single request also goes to Python when its path is
  not ASCII as sent, is not UTF-8 once its escapes are decoded or then holds a control character, `?` or `#`, its method is not one of the plain ones, or the client's address cannot be resolved
  the way Python resolves it. The stock `request-id` plugin is reproduced natively.
* **A path is read decoded and written again, as Python does it.** The Python gateway's server decodes a path's
  percent-escapes before anything looks at it, and its HTTP client drops `.` and `..` segments and escapes the path
  again on the way to a backend. This side does both the same way, so `/api/a%20b`, `/%61pi/x` and `/api/a/../b`
  match keys' paths, policies, routes and protocols as they do there and reach the backend spelled the same.
  `conformance/paths.py` compares both steps with uvicorn's and httpx's own code over generated paths.
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

A request is served natively only when **all** of these hold: the link is fresh; the path is `/api/...`, has no protocol's prefix and is not one the Python gateway answers itself
(with `HTTP_BACKEND_URL` set there), or belongs to
MCP, A2A, AG-UI, ACP or GraphQL and that protocol has an `http://` or `https://` backend configured; no `fallthrough_prefixes` entry matches;
`X-API-Version` or `Accept-Version` resolves to a version this side was told it may serve; the key is in the snapshot, active, unexpired and `native_ok`; and, where the
gateway checks the body, the body fits in memory (4 MiB, or the MCP limit if lower) and is
certain to pass. A body sent without a `Content-Length` is read until it ends or passes that size; one that passes it
goes to the Python gateway whole, what was read first and then the rest. On those same paths, a request with no credential, or with one the Python gateway has just refused,
is answered here with the gateway's 401. Anything else, including credentials seen for the first time,
`/metrics`, `/admin`, `/oauth2`
and MQTT other than publishing, goes to the Python gateway untouched, so its answer is the answer.

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

**WebSocket.** A handshake for `/ws/...` is relayed here when `WEBSOCKET_BACKEND_URL` is a `ws://` address (or `wss://`, see TLS below) and the
key is certain to be let in: known, working, allowed that path, and refused by no IP rule or policy. A handshake goes
through those as a `GET` for its path does, from the caller's address as for any other request. The key is read where the Python gateway reads it: in the query's `api_key` (the last one given, unless it
is empty), or else in `X-API-Key`. The backend is asked for the rest of the path and the query, without `api_key` and
without empty parts, and sent only the handshake, the subprotocols
offered and the caller's `Authorization` and `Cookie`; never the gateway's key. Once it accepts, the connection is a
tunnel: bytes go both ways until either side closes. If it refuses or cannot be reached, the caller gets 403. Every
other handshake (an unknown key, a key or a name in the query that is escaped, one an IP rule or a policy refuses, a
caller's address this side cannot be sure of while such rules are in force) is tunnelled
to the Python gateway, which relays or refuses as it always does.

A handshake for `/agents/<slug>/...` is relayed here to a registered agent, at its address read as `ws://` for
`http://` and `wss://` for `https://`. As for any other request to an agent, no gateway key is asked for. The agent is
asked for the rest of the path and the query exactly as they came, and sent the same few headers. An agent behind TLS
is this side's only with `SSL_CERT_FILE` given, as for a `wss://` backend; an unknown slug is the Python gateway's to
refuse.

**OAuth2 tokens.** An access token the Python gateway issued is a credential in `Authorization: Bearer <token>`, and
the snapshot lists each by its SHA-256 digest, never the token. One that is listed and has not run out is served here
under the gateway's default limits, as the Python gateway serves it: no tenant, no allowed paths. Its caller goes
through the IP rules and the policies as a key's does, on both planes: the global rules apply, and those scoped to a
key or a tenant do not, since a token has neither. A token sent beside a key header, one that has run out, and one
issued a moment ago are the Python gateway's to decide; a revoked one is refused within about one sync interval. On a
gRPC call a token in `authorization` is a credential too, and reaches the backend as the caller's own. A WebSocket
handshake is not let in on a token alone by the Python gateway, so none is relayed here on one.

**MCP tool schemas.** A tool that comes from a registered OpenAPI document has a JSON Schema, and the Python gateway
validates a `tools/call`'s arguments against it. The snapshot carries the schemas, and this side forwards a call whose
arguments are certain to pass. It never refuses one: a call that does not fit goes to the Python gateway, which words
the refusal. A schema is followed here only if every keyword in it is one of `type`, `properties`, `required`,
`additionalProperties`, `items` (one schema), `enum` (of strings), `minimum`, `maximum`, `minLength`, `maxLength`,
`minItems`, `maxItems`, or says nothing about validity (`title`, `description`, `default`, `examples`, `format`, and
what OpenAPI adds: `example`, `nullable`, `x-...`). With any other keyword (`pattern`, `$ref`, `oneOf`, ...) the tool's
calls stay with the Python gateway, and so does a value the two sides could read differently, such as a whole number
written `2.0`. `python dataplane/conformance/boundary.py` generates schemas and calls and checks that nothing the
Python gateway refuses is forwarded here.

**MQTT publishing.** The Python gateway holds one MQTT 5 connection to the broker in `MQTT_BROKER_URL` and publishes on
it what `POST /mqtt/publish` is sent: `json.dumps` of the `payload`, never retained, at the `qos` asked for (1 if none).
While that connection stands, the snapshot names the broker and this side keeps a connection of its own to it (one for
the process, on its own thread, under the client name `gateway_dataplane_<random>`). A publish is answered here when the
connection stands and it is certain which message the Python gateway would publish: the body is a JSON object in UTF-8
with a non-empty string `topic` without wildcards, a `qos` written `0`, `1` or `2` (or none), and a payload this side
can spell exactly as Python does. Everything else goes to the Python gateway, which publishes or refuses it: a member
named twice, `NaN`, a number exactly halfway between two shortest spellings (the two languages round those
differently), a body over 1 MiB or in another encoding, and any request while this side's connection is down. A broker
behind TLS (`mqtts://`) is this side's only with `SSL_CERT_FILE` set and `SSL_CERT_DIR` not, for the reason given for
`wss://` backends: only then do both sides verify it against the same certificates. As with the Python gateway's client, the answer is given once the message is handed to the
connection, not once the broker acknowledges it; messages above quality of service 0 are kept until it does and sent
again after a reconnect.

**MQTT subscribing.** `POST /mqtt/subscribe` makes the Python gateway's connection subscribe to a topic filter; what
the broker then sends is written to that gateway's log (`mqtt_message_received`, at level info) and nothing else is done
with it. Here it makes this side's connection subscribe instead, with the same packet: one filter, no option but the
quality of service. It is answered here when the connection stands and the body is a JSON object in UTF-8 with a
`topic` the Python gateway's client takes as a filter (`+` only as a whole level, `#` only as the last) and a `qos`
written `0`, `1` or `2` (or none); everything else goes to the Python gateway, which subscribes or refuses. Each message
the broker sends is acknowledged as that client acknowledges it, and written to this process's standard output as one
line of JSON: `topic`, `payload` (the JSON the message spells, or its text under `raw`), `event`, `level`, `logger` and
`@timestamp`. **So with the data plane in front, messages for a subscription made through it appear in the data plane's
output, not in the Python gateway's log.** As there, a subscription ends with the connection: neither side subscribes
again by itself when the broker comes back.

`python dataplane/conformance/publish.py` generates bodies and checks that whatever this side would publish or
subscribe to, the Python gateway's own handler and client publish or subscribe to the same.

**The JSON bridge under `/grpc`.** A `POST` to a path that begins with `/grpc` or `/api/grpc` carries JSON
(`service`, `method`, `data`, `stream`) that the Python gateway turns into a gRPC call of its own to
`GRPC_BACKEND_URL`. Its bridge knows one service, the sample `greeter.Greeter`, and what it does depends on its
installation, which the snapshot says (`grpc_bridge`): whether the gRPC reflection package can be loaded (it is not
among the gateway's requirements, and without it no single call is ever made) and whether the sample's generated code
is found. This side does the same in each case:

* `"stream": true` with `Greeter` and `SayManyHellos`: the call is made (60 seconds at most) and each answer is
  written as a line of JSON (`application/x-ndjson`) when the server sends it. The answer starts at once with status
  200; if the call fails, it breaks off after the lines already sent, as the Python gateway's does.
* `Greeter` and `SayHello`, where the reflection package and the generated code are there: the call is made (30
  seconds at most) and answered as `{"status":"success","message":...}`; a call that fails is a 502 naming the gRPC
  status.
* Anything else: `{"status":"unsupported",...}` with the data sent back, spelled as the Python gateway spells it.

It is served here when `GRPC_BACKEND_URL` is a plain `host:port` and the body is a JSON object in UTF-8 of at most
1 MiB whose `service` and `method` are strings (or absent), whose `stream` is written `true`, `false` or `null`, and
whose `data`, where a call is made, is an object with a `name` that is a string (or absent). Every other request (any
method but `POST`, a member named twice, a number too large to hold, a transformation rule that applies to the path)
goes to the Python gateway, which answers in its own words. No dynamic route comes before the bridge, here or there.
`python dataplane/conformance/bridge.py` generates bodies and checks, in each of the three installations, that
whatever this side would do with one, the Python gateway's own handler does the same.

**Transformation rules.** A rule that changes headers, query parameters or the fields of a JSON body of a request,
rewrites its path, or changes headers or the status of an answer, is applied here, as the Python
gateway applies it: for each rule that applies, in the order the rules were made, headers are renamed, then removed,
then set, query parameters are removed, then set, fields of the body are renamed, then removed, then set, and the
path is rewritten; an answer's status is mapped by each rule in turn. The quirks are the Python gateway's and are kept: a header is found for renaming or
removing whatever its case, but is set under exactly the name the rule gives, so a header set as `X-Client` arrives
beside the caller's own `x-client`, not in its place. Any other rule is the Python gateway's to apply, and a request it
applies to is handed over: a rule that fills in a template, sets a value in a body that is not plain JSON, or
rewrites the path by a regular expression of Python's own; one that names a
header an HTTP library on the way has its own say in, or that the request is read by again after the rules (`Host`,
`Content-Length`, `Accept-Encoding` and the hop-by-hop headers on a request; on an
answer `Content-Type`, `Content-Length`, `Set-Cookie`, `Vary`, `Date`, `Server` and the rate-limit and tenant headers); one with a header name or value that is not plain ASCII
without a space at either end, or a query word of other than unreserved characters; one that maps a status to or from
one without a body (below 200, 204, 205, 304). gRPC and `POST /mqtt/publish` have handlers of their own there, which
apply no rule; a rule that matches one keeps it with the Python gateway, as before, and so does a rule that matches
an agent's path. `python dataplane/conformance/shapes.py` generates rules as the admin API takes them and exchanges to
apply them to, and checks that what the backend is sent and what the caller is given are what the Python gateway's
own rule code, HTTP client and web framework make of them.

**Given more than once.** A header a caller gives on more than one line reaches the backend with every line, and a
query parameter given more than once with every value: from the Python gateway (which used to send one of each) and
from here. The rules read one value of each, as they always did: a header's first line, a parameter's last value. A
header or parameter a rule sets, renames or removes is sent as the rule left it; any other goes on whole. The values
of one parameter are sent together, where the parameter first appeared (`?tag=a&one=1&tag=b` is sent as
`tag=a&tag=b&one=1`). `Host`, `Content-Length` and the hop-by-hop headers are sent on one line by the Python gateway;
a request that gives one of those twice is handed to it.

The query is read and written again on the way, as the Python gateway's HTTP client writes it: `?flag` becomes
`?flag=`, `+` becomes `%20`, an escape that need not be one is taken off (`%7E` becomes `~`), and a `?` with nothing
after it is dropped. A query of plain `key=value` words is sent as it came. An escape that is not UTF-8 text once read is read as the
Python gateway reads it, as a replacement character (`?q=%ff` reaches the backend as `?q=%EF%BF%BD`).

The backend is sent the caller's `Host`, as the Python gateway's HTTP client sends the one it is given, and is sent
that client's own `Accept` and `User-Agent` (which the snapshot names) when the request carries none of that name in
any spelling. A rule may name all three: the backend is sent what the rule leaves, a `Host` it takes away is the
backend's own address, and an `Accept` or `User-Agent` it takes away is the client's own again. The client's
`Accept-Encoding` is not sent from here, and a rule that names it is the Python gateway's: that gateway decompresses
what the header invites, and this side passes an answer on as the backend sent it.

A rule may name `X-API-Version` and `Accept-Version` on a request. The Python gateway reads the version again from
the headers the rules leave, and so does this side: the backend is told that version (and is sent to its backend, or
refused, if the version says so), while the caller is still told the version its own headers named. On an answer a
rule may name `Warning`, `Sunset`, `Deprecation`, `Retry-After` and the cross-origin headers: where the gateway
writes one of those itself (a version on its way out, a page from a listed origin) its own line replaces the rule's,
and where it does not the rule's stands.

A rule may name the headers the gateway itself writes. On a request those are `X-Forwarded-For`, `X-Protocol`,
`traceparent` and (with the request-id plugin) `X-Request-ID`. The Python gateway writes them after the rules, into the
same dictionary of headers the rules have just changed, where a name is its exact spelling. So a rule that sets
`X-Forwarded-For` under that spelling has the last word on it (the gateway then adds no empty one), and one that sets
`X-Protocol` or `traceparent` under the gateway's spelling is written over; a line under any other spelling
(`x-protocol`, `Traceparent`, or the caller's own, which arrive in lower case) is sent beside the gateway's, before or
after it as the dictionary has them. A rule that removes one of these finds the gateway writing its own all the same;
one that renames `X-Request-ID` moves the id the plugin added. This side does the same, line for line. It also
does so where no rule applies: a caller's own `x-forwarded-for`, `x-protocol` or `x-api-version` goes to the backend
beside the gateway's line, as from the Python gateway (until now this side sent the backend one line of each). On an
answer the gateway writes `X-Request-ID`, `X-Response-Time`, `traceparent` and, when the request resolved to a version,
`X-API-Version` over whatever a rule left under those names, on both sides.

A rule's pattern is matched, as there, against the path the backend is asked for (after a dynamic
route's rewrite, a protocol's prefix or an agent's slug has been dealt with) and, for the answer, against the request's
own path without its leading slash. The Python gateway
describes each pattern in the form this side matches exactly as it does: a glob of plain characters, `*` and `?`; a
glob with character classes, each class given as the characters Python's own `fnmatch` lets it match; `*` or `**`,
which is every path (for the rule's methods); a regular expression (a pattern that starts with `^` or ends with `$`),
matched from the start of the path; and, for such a pattern that is not a valid expression, the text the path must
then contain. A regular expression is matched here only if it means the same to both engines: plain characters, `.`,
`\d`, `\w`, classes of plain characters and ranges, groups, `|` and repetition, with `^` only in front and `$` only at
the end. A rule with any other expression (a look-around, a back-reference, a flag) or a pattern outside ASCII keeps
every path with the Python gateway. `python dataplane/conformance/patterns.py` generates patterns of every kind and
checks that this side matches each as the Python gateway's own matcher does.

A request's body is read here only for a rule that changes its fields, and only on the methods the Python gateway
reads a body for (`POST`, `PUT`, `PATCH`). A body that is a JSON object is changed by each rule in turn (fields are
named by dotted path; a field that is `null` is not renamed; whatever stands in the way of a field to set and is no
object is replaced by one) and written again in Python's own spelling: `, ` and `: ` between members, everything
outside ASCII escaped, numbers as Python prints them, a member named twice kept once. It goes on with its new length
and, if the caller did not say what it is, as `application/json`. Any other body (a list, text, nothing) goes on as
it came. A request is handed to the Python gateway whole when this side is not certain what Python would make of its
body: one that opens as an object and then is not JSON, is nested more than 64 levels deep or holds `NaN`, a number
too large to hold or one of the few whose digits the two sides might choose differently; one over 1 MiB, with a
stated length or without. A rule that sets a value which is not plain JSON is the Python gateway's to apply.

A path rewrite is done here in two forms. Without an expression: its pattern is plain text (letters, digits, `/`,
`_`, `~`, an escaped `.` or `-`), with `^` in front to replace it at the start of the path only and otherwise wherever
it stands, and its replacement is plain text too. By expression: a regular expression of the kind the two engines read
alike (the same kind a rule's path pattern may be), each match from the left replaced by plain text and by groups of
the match, written `\1` to `\9` or `\g<n>`. Either is applied to the path the backend is asked for, each rule to
what the last left, while the rules themselves were chosen by the path before any rewrite, as in the Python gateway.
A request whose rewritten path no longer starts with `/` is handed over.

Some expressions stay with the Python gateway, which rewrites every path their rule applies to:

* one that can match nothing (`a*`, `/?$`), which Python's `re.sub` replaces in more places than the engine here
  does; `^` and `$` alone, which add to the front and the end of the path, are done here;
* one that repeats a group which can match nothing (`(a*)+b`): Python's engine stops repeating at a turn that
  matches nothing, so the two can disagree on where the match ends and on what its groups held;
* one with alternatives that each hold a repetition or a group (`a*(aa)|a*a`): the engine here takes what
  alternatives begin with alike out in front of them, and where that can match in more than one way, the alternative
  that wins is no longer the first that could;
* one with anything only Python's engine reads (a look-around, a back-reference, a named group, a flag), and a
  replacement with any other escape or a group the expression does not have.

An expression reads a path beyond ASCII differently in the two engines (`\w` and `\d` take different characters),
so such a path is the Python gateway's to rewrite, and to match where a rule's own path pattern is an expression.
An expression that does not compile rewrites nothing on either side.
`python dataplane/conformance/shapes.py` puts expressions together at random and checks each rewritten path against
`re.sub`.

**API versions.** The version a request resolves to is echoed to the caller as `X-API-Version`: the one its
`Accept-Version` or `X-API-Version` header names (a leading `v` ignored), else the default. The backend is told the
same, unless the path it is asked for names a version itself (`/v2/...`, `/v2.1/...`): the path after a protocol's
prefix is taken off, after `/agents/<slug>`, or as a dynamic route or a rule rewrote it. Then the path decides, as in
the Python gateway: a version the gateway knows is the one the backend is told, whatever the headers hint, and one it
does not know means the backend is told nothing. With `strip_prefix_on_proxy` set, a known version is taken off the
path on the way (`/v2/runs` is asked for as `/runs`, `/v2` as `/`).

A version on its way out is announced here as the Python gateway announces it. The backend is told with the version:
`Warning` when the version is deprecated, `Sunset` when it has a date to go, each beside whatever the caller sent
under that name. The caller is told on every answer under the version, the gateway's own refusals included: those
two and `Deprecation: true`, over whatever the backend said. The snapshot carries the texts, so they are the Python
gateway's own.

A version that is gone (removed, or more than a minute past its sunset) is refused here as the Python gateway's
proxy handler refuses it: a 410 in its words, once the key, the limits and the body have been checked and counted,
and with no `X-API-Version` on any answer under it. A version with a backend of its own sends its requests there,
unless a dynamic route decides; the backend is followed like a route's, with a circuit breaker of its own. Kept with
the Python gateway: a version within a minute of its sunset on either side, one whose sunset date has a UTC offset
(which only the Python gateway compares), one with a backend this side cannot reach, a gone version on the way to
an agent, and a path whose first segment has a digit from another script, which Python's expression counts as a digit.

An answer's body is read only for a rule that changes its fields; without one it is the backend's, byte for byte.
Such a rule is applied here to an answer that states a length of 1 MiB or less, is not compressed and is UTF-8. As
in the Python gateway, a JSON object is changed by each rule in turn and written again in Python's spelling, unless
nothing is left in it, and then the body goes on as it came; so does a body that is no JSON, or JSON of another kind.
The answer's headers and status are changed by the same rules.

A request cannot be handed over once the backend has answered it, so an answer this side is not certain of is shown
to the Python gateway instead: `POST /internal/dataplane/shape-answer`, with the data plane's token, carries the
backend's status, header lines and body, and is answered with the status, headers and body the caller is to be given.
The code that answers a request served by the Python gateway makes that answer (`app.main.answer_from`), so the
caller is given what the Python gateway alone would have given. It is asked for an answer that is compressed, states
no length (sent in pieces) or one over 1 MiB, is not UTF-8 (no rule is applied to such an answer there), is a JSON
string (read a second time there for the object it may spell), or holds anything this side might read or write
differently (a number JSON does not have, a member named twice in a way that matters). If the Python gateway cannot
be asked, the caller gets a 502: the backend's answer is not passed on unchanged. Without a link to the Python
gateway (`--snapshot` alone), a request such a rule applies to is handed over whole, as before.

An answer the Python gateway passes on as a stream has no rule for answers applied to it. Whether it is one is
decided here as there: an A2A `message:stream` path, an AG-UI path whose caller accepts `text/event-stream` or asks
with `?sse=`, an ACP run posted by a caller that accepts events or asked for with `?stream=`. A query that is escaped,
or gives `sse` or `stream` twice, is the Python gateway's to read, and so is the request.

**Browsers.** A caller that prefers `text/html` (decided as the Python gateway decides it: `text/html` accepted, JSON
not named before it, no `X-Requested-With: XMLHttpRequest`) is shown errors as pages. A successful request is
forwarded like any other. An error answered here (401, 403, 429, 502, 503) becomes the Python gateway's page for that
status, which comes with the snapshot, with the error's code, message, hint, request id and retry time written in.
The status and headers are those of the JSON answer.

**Requests from web pages.** A request with an `Origin` header is answered here with the cross-origin headers the
Python gateway's CORS middleware writes, from `CORS_ORIGINS` and `CORS_ALLOW_CREDENTIALS` in the snapshot: a listed
origin is named back with `Vary: Origin`, `*` is answered with `*` (or with the origin itself when the request has a
cookie), and the credentials header is added when allowed, all over whatever the backend sent under those names. As
in the Python gateway, these go on answers from a backend (and the 502 or 503 standing in for one), the health
endpoints and agents; the gateway's own 401, 403 and 429 carry none. A preflight (an `OPTIONS` request with `Origin` and
`Access-Control-Request-Method`) to a path this side serves is answered here, from the middleware's own set-up as the
snapshot describes it; `conformance/preflight.py` compares the answers with the middleware's over generated set-ups.
Any other `OPTIONS` request to such a path is answered here too, as the Python gateway's proxy handler answers every
one before it looks for a key: 200, an empty body, and three headers that say any origin may ask anything
(`Access-Control-Allow-Origin: *`, the seven methods, `Access-Control-Allow-Headers: *`), with what the middleware
grants the caller's origin written over them.

**Backends behind TLS.** An `https://` backend (a protocol's, a dynamic route's, an agent's, or `--backend`) is spoken
to over TLS, HTTP/1.1, and its certificate is verified as the Python gateway's HTTP client verifies it: against the
certificates in `SSL_CERT_FILE` when that is set, otherwise against the Mozilla root certificates. A backend whose
certificate does not verify is not spoken to and counts as unreachable (`502 BACKEND_ERROR`, or for an agent the
Python gateway's answer). With `SSL_CERT_DIR` set, or a certificate file that cannot be read, this side speaks no TLS
and says so when it starts; `https://` backends are then the Python gateway's.

A gRPC backend named as `https://host:port` is spoken to over TLS, with HTTP/2 agreed in the handshake and the same
verification. A `wss://` WebSocket backend is relayed to here only when `SSL_CERT_FILE` is set for both this side and
the Python gateway: the Python gateway's WebSocket client verifies against the system's certificates and that file,
not the Mozilla set, so only with the file do both sides trust what this side trusts. If the TLS connection to a
`wss://` backend fails here, the handshake is handed to the Python gateway instead of being refused.

**Dynamic routes.** The routes registered through `/admin/routes` that are in force come with the snapshot, in the
order the Python gateway tries them: exact paths, then wildcards from the longest prefix down. A request whose path
matches one goes to that route's backend with the route's protocol name and timeout, and to its rewritten path if it
has one; otherwise the caller's path, unchanged (a protocol's prefix such as `/acp` is not taken off a routed path).
Keys, limits, rules and policies apply as for any other path. A route takes effect here one sync interval after it does there.

A route's backend need not be named as HTTP, and this side goes where the Python gateway's HTTP client goes. That
client speaks plain HTTP to a `ws://` address and HTTP over TLS to a `wss://` one, exactly as to `http://` and
`https://`, so a route to one is followed here the same way (under its own name: `ws://host` and `http://host` each
have a circuit breaker). Any other scheme (`mqtt://broker:1883`, `grpc://...`), or none (`localhost:50051`), the
client refuses before it connects anywhere: every request such a route matches is a 502 (`BACKEND_ERROR`, the same
body as for a backend that cannot be reached), counted against that backend's circuit breaker, which opens after as
many failures as any other and then answers 503. This side does the same, and sends nothing. A route whose address
this side is not sure the client reads that way (one with a user name, a space or a query, an IPv6 address without a
scheme, a scheme written in capitals that the client would speak), or whose rewrite or timeout it cannot follow,
leaves what it matches to the Python gateway; so does every such route when the gateway runs with another HTTP client.

**The agent edge.** A request for `/agents/<slug>/...` is forwarded here when the agent is registered with a plain
`http://` or `https://` upstream and the request would reach it exactly as the Python gateway sends it. As there, no gateway key is
asked for and no limit, IP rule or policy applies: the agent authenticates its own callers. The agent is sent the path
after the slug, the caller's headers (`Host`, `Authorization` and `Cookie` included) and the gateway's additions, and
its answer is streamed back as it comes. An escaped path is read decoded, slug included, and the agent is asked for it
as the Python gateway's HTTP client writes a path, as on the way to any backend (so `%2F` reaches it as `/`). The
Python gateway is handed the request when:

* the slug is not registered (the 404 is Python's);
* the caller asks for a WebSocket;
* a header about the connection is given twice (see "Given more than once"
  below), or the path after the slug names an API version that is gone;
* the body is over 1 MiB, or came without a `Content-Length` and is empty;
* the agent cannot be reached. Nothing was sent, so Python tries, answers in its own way and counts the failure in
  its circuit breaker. This side keeps no breaker for agents.

An agent that hangs up before answering gets the Python gateway's 500 from here: its error page for a browser, JSON
for anyone else, both taken from the snapshot so the words stay the Python gateway's. A compressed answer is passed on
compressed with its `Content-Encoding`; the Python gateway decompresses it first. The caller reads the same either way.

**gRPC.** A call (HTTP/2, `POST`, content type `application/grpc` or `application/grpc+...`) is relayed to
`GRPC_BACKEND_URL` when that is a plain `host:port` (HTTP/2 without TLS) or `https://host:port` (over TLS), with the path unchanged and the body and
trailers streamed both ways. The Python gateway cannot carry gRPC, so there is nobody to hand a call to, and the rule
here is the reverse of everywhere else: **whatever this side cannot be sure of is refused.**

* The key comes from the key header or from `authorization: Bearer`, and must be in the snapshot, active and
  unexpired. A key created less than a sync interval ago is refused until the next sync.
* The key's allowed paths (matched against `/package.Service/Method`), the IP rules, the policies, the key's rate
  limit, its tenant's quota and the backend's circuit breaker apply as for any native request.
* A key marked `native_ok: false` is refused, and so is a call a transformation rule applies to, and every call
  while a request-altering plugin exists: those are rules this side cannot apply. Dynamic routes do not apply to gRPC calls.
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
| MCP `tools/call` for a tool with a registered JSON Schema, arguments certain to fit | data plane |
| MCP `tools/call` whose arguments do not fit, or might not; a schema with a keyword this side does not follow; custom injection patterns | Python |
| A key sent as `Authorization: Bearer <key>` | data plane |
| No credential; a credential Python refused in the last ten seconds; a key clearly past its expiry | data plane (401) |
| An OAuth2 token in `Authorization: Bearer`, listed in the snapshot, under the IP rules and policies | data plane |
| A credential seen for the first time; two credentials of which the key does not work | Python |
| Browsers | data plane, with the gateway's error pages |
| WebSocket under `/ws/`, key in `X-API-Key` or in the query's `api_key`, let in by the IP rules and policies | data plane |
| WebSocket under `/agents/<slug>/` to a registered agent | data plane |
| Any other WebSocket (refused by an IP rule or a policy, an escaped key, an unknown slug) | Python, through a tunnel |
| gRPC calls (HTTP/2, `application/grpc`) | data plane only; refused when it cannot be sure |
| `POST /mqtt/publish` (broker at `mqtt://`, or `mqtts://` with `SSL_CERT_FILE`), body certain to publish the same message | data plane, over its own connection to the broker |
| `POST /mqtt/subscribe`, under the same conditions, a filter certain to be taken | data plane, on its own connection; the messages are written to its output |
| Any other publish or subscribe (a body this side is not sure of, `mqtts://` without `SSL_CERT_FILE`, the connection down) | Python |
| Any other request over HTTP/2 | as the same request over HTTP/1 |
| `/agents/<slug>/...` to a registered agent, browsers included | data plane |
| `/agents/<slug>/...` otherwise (unknown slugs, unusual requests) | Python |
| `/graphql/*`, `/api/graphql/*` with `GRAPHQL_BACKEND_URL` set | data plane, passed through as they came |
| Any other path (`/users`, `/rest/...`, `/http/...`, `/`) with `HTTP_BACKEND_URL` set | data plane, to `--backend` |
| The same without `HTTP_BACKEND_URL` (Python sends them to the MCP backend) | Python |
| A percent-escaped path, or one with `.` or `..` segments, on any of the above | data plane, read and written as Python does |
| A path that decodes to a control character, `?`, `#` or bytes that are not UTF-8 | Python |
| `POST` of JSON to the bridge under `/grpc`, read here exactly as Python reads it | data plane |
| Anything else under `/grpc` that is not a gRPC call | Python |
| A path that matches a dynamic route to an `http://`, `https://`, `ws://` or `wss://` backend | data plane, to the route's backend |
| A path that matches a dynamic route to a backend in another scheme, or in none | data plane: a 502, nothing is sent |
| A path that matches a dynamic route with an address, rewrite or timeout this side is not sure of | Python |
| A request a transformation rule applies to (by path pattern and method), when the rule changes headers, query parameters, the fields of the request's JSON body, the status or the headers of the answer, or rewrites the path by plain text or by an expression both engines read alike | data plane, which applies it |
| An answer whose body a rule changes, when it is short, not compressed and a JSON object this side reads as Python does | data plane, which applies the rule |
| Any other answer whose body a rule changes | data plane, with the answer shaped by Python (`/internal/dataplane/shape-answer`) |
| The same, when the rule changes a header the gateway has its own say in, rewrites the path by an expression of Python's own, fills in a template, or the request is one the two sides would read differently | Python, which applies it |
| Any transformation rule whose regular expression the two engines could read differently | Python, for every path |
| Request-altering plugins (when any exist) | Python, for every path |
| The health path, `/ready` and `/live`, on a plain `GET` | data plane |
| A request with an `Origin` header | data plane, with the gateway's cross-origin headers |
| A preflight to a path served here | data plane |
| Any other `OPTIONS` request to a path this side serves | here |
| A preflight header given twice, an `Origin` given twice, `OPTIONS` to an agent or to one of the gateway's own paths | Python |
| `/health/detailed`, `/metrics`, `/oauth2`, `/admin` and the console | Python |

## Known gaps (why this is not production-ready)

* **A WebSocket relayed here is a tunnel, not two conversations.** The Python gateway ends the caller's WebSocket and
  opens its own to the backend, so it answers pings itself, sends the backend a keep-alive ping every 20 seconds, and
  takes no message above 16 MiB from the caller. Here pings, compression and message size are between the caller
  and the backend. Messages, subprotocol and close codes are the same either way.
* **A WebSocket is checked when it opens, not while it is open.** A key revoked afterwards keeps its open connections,
  here and in the Python gateway alike. Connections are not counted, limited or written to the request log by either.
* **`wss://` backends and agents without `SSL_CERT_FILE` are relayed by the Python gateway**, not here.
* **The bridge under `/grpc` connects for each call when it must; the Python gateway's channel backs off.** After its
  gRPC server has been down, the Python gateway keeps answering `UNAVAILABLE` until its channel tries again (up to two
  minutes); this side reaches the server as soon as it is back. The server sees this side's calls under the user agent
  `agentanywhere-dataplane`, and an answer the server compresses is a failure here (none is asked for).
* **An agent's WebSocket message above 1 MiB passes here.** The Python gateway's relay closes the connection on one from the agent.

* **A message from the broker above 16 MiB ends this side's MQTT connection**, and with it the subscriptions made
  through it. The Python gateway's client takes a message of any size.
* **`/ready` can be one sync interval behind** (500 ms by default), or as long as a sync that hangs takes to fail
  (five seconds), because it repeats the Python gateway's last answer instead of asking the database itself.
* **`/metrics` is still the Python gateway's.** It is the registry for the whole gateway, including everything this
  side reports to it, so there is one place to scrape. The data plane has no metrics endpoint of its own yet: with the
  Python gateway down, nothing can be scraped.

* **A refused credential stays refused for up to ten seconds after the Python gateway would accept it** in one case
  this side cannot see: the gateway was restarted with a new `SECRET_KEY` or OAuth2 state so quickly that no sync
  failed in between. Key changes made through the admin API are not affected (they change the snapshot).
* **Some MCP traffic is still judged by Python.** A string that contains one of four letters Python's
  case-insensitive matching treats as `i`, `s` or `k` (U+0130, U+0131, U+017F, U+212A), a method name that is not ASCII
  while a method list is configured, a `tools/call` whose arguments are not certain to fit the tool's JSON Schema, any MCP body when custom injection
  patterns are configured, and any checked body too long to hold here: all correct, none fast.
* **The four protocols have not been benchmarked.** Reading and parsing a body to check it costs time the plain HTTP
  path does not spend; how much has not been measured.
* **Runtime changes held only in a Python worker's memory are not visible to the snapshot:** transformation rules,
  API version changes, plugins, OAuth2 tokens and the MCP tool registry are per process in the Python gateway. The snapshot reflects the worker that answers
  it, so with several workers, change them through configuration and restart, or run one worker, until these are stored.
* **Reporting is by aggregate and sample, not per request.** Counts, latency and SLOs are exact; the request-log view
  shows every error but only a capped number of successes at high rates. Each key's `last_used_at` is kept current.
* **Usage is reported at least once.** If the control plane applies a report and the acknowledgement is lost, that
  interval's tenant usage and counts are sent again and counted twice. The error is on the strict side for quotas.
* **A body sent without a length, where nothing here looks inside it, is passed on as it comes.** The backend is
  sent it in chunks, with `Transfer-Encoding: chunked`. The Python gateway holds every body whole and sends it with a
  `Content-Length`. Where this side reads the body (a checked path, a rule that changes it, an agent, publishing, the
  bridge), it sends the length too.
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
* **The backend is not asked for a compressed answer from here.** For a caller that sends no `Accept-Encoding`, the
  Python gateway's HTTP client asks for `gzip, deflate` and decompresses what comes back; this side asks for nothing.
  The caller is given the same either way.
* **An answer to `HEAD` keeps the length the backend stated.** The Python gateway states a length of 0.
* The `Server` response header differs (uvicorn on one side), and native answers add `X-Served-By`.

## Tests

```bash
cargo test                              # unit tests
python dataplane/conformance/run.py     # same scenarios against Python and the data plane, plus the live-link
                                        # scenarios (needs the build above)
python dataplane/conformance/boundary.py  # the body checks against the Python functions, on generated bodies
python dataplane/conformance/publish.py   # what would be published and subscribed to over MQTT, against the Python handler and client
python dataplane/conformance/bridge.py    # what JSON posted to the bridge under /grpc asks for, against the Python handler
python dataplane/conformance/patterns.py  # which paths a transformation rule's pattern matches, against Python's matcher
python dataplane/conformance/shapes.py    # what applied transformation rules make of a request and its answer, against Python's rule code
python dataplane/conformance/preflight.py # what a preflight is answered, against the CORS middleware itself
python dataplane/conformance/paths.py     # how an escaped path is read and written again, against uvicorn's and httpx's own code
```

The conformance suite is the definition of "same behaviour": a scenario must pass on both before the data plane may
handle that kind of request.
