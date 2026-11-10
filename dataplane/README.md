# AgentAnywhere data plane (experimental)

A Rust proxy that sits in front of the Python gateway and handles the hot path itself: it authenticates API keys,
applies per-key rate limits, path scopes, IP rules, governance policies and tenant quotas, and forwards plain HTTP
requests, streaming bodies in both directions. Everything else is handed to the Python gateway unchanged. Design, goals and phases: [DESIGN.md](DESIGN.md).

**Status: phase 1, not for production.** The limits below are real; read them before using it.

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

`--backend` is the HTTP backend, `--control` the Python gateway. The token can also come from `DATAPLANE_TOKEN`.
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

A request is served natively only when **all** of these hold: the link is fresh; the path is `/api/...` and not another
protocol's; no `fallthrough_prefixes` entry matches; the client is not a browser (`Accept: text/html`);
`X-API-Version` or `Accept-Version` resolves to an active version with no backend of its own; and the `X-API-Key` is in
the snapshot, active, unexpired and `native_ok`. Anything else, including unknown keys, `Authorization: Bearer`
credentials, `/health`, `/admin`, `/oauth2`, WebSocket and the other protocols, goes to the Python gateway untouched, so
its answer is the answer.

## Known gaps (why this is not production-ready)

* **Runtime changes held only in a Python worker's memory are not visible to the snapshot:** transformation rules,
  API version changes and plugins are per process in the Python gateway. The snapshot reflects the worker that answers
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
```

The conformance suite is the definition of "same behaviour": a scenario must pass on both before the data plane may
handle that kind of request.
