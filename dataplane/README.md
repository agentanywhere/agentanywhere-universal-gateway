# AgentAnywhere data plane (experimental)

A Rust proxy that sits in front of the Python gateway and handles the hot path itself: it authenticates API keys,
applies per-key rate limits and forwards plain HTTP requests, streaming bodies in both directions. Everything else is
handed to the Python gateway unchanged. Design, goals and phases: [DESIGN.md](DESIGN.md).

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
for `/internal/...`. Tunables: `--sync-interval-ms` (default 500) and `--max-staleness-ms` (default 5000).

## How the data plane stays correct

* **Keys come from the control plane.** Every interval it asks for anything newer than the revision it holds. The
  revision is a hash of the content, so a change made by any Python worker, however it was made, is noticed, and a
  quiet poll costs a few bytes. Keys arrive as SHA-256 digests only.
* **Changes take effect within about one interval.** In the conformance suite a revoked key is refused 0.4 s after
  the delete, a new key is served natively after 0.3 s, and an IP rule moves keys back to Python in 0.4 s.
* **It fails closed.** Until the first answer, and again whenever no answer has arrived for `--max-staleness-ms`, the
  data plane serves nothing itself and hands every request to the Python gateway. A revocation it could not hear about is
  never honoured for longer than that limit.
* **Anything it cannot reproduce goes to Python.** The snapshot marks a key `native_ok: false` when a tenant (quota,
  usage), an IP rule, a policy or a stored path restriction applies to it, and turns native serving off for every path
  when dynamic routes, transformation rules or a request-altering plugin exist. The stock `request-id` plugin is
  reproduced natively.
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
  shows every error but only a capped number of successes at high rates. There is no per-request `http_access` log line
  for native requests. Each key's `last_used_at` is kept current (the latest use per key rides along with each report).
* **A rate-limit webhook stands for a batch, not one request.** A native 429 fires the same webhook and alert, but
  once per key and window in each report, with a `count` of the requests refused, where Python fires one per request.
* **The upstream timeout covers the wait for the backend to start answering** (`DEFAULT_TIMEOUT`, from the snapshot);
  a response that starts and then stalls is not cut off. No test exercises the timeout itself yet.
* **`gateway_errors_total` by error type is not fed** for native upstream failures; the request counters, the RED
  error counter and the request log are.
* **Tenant quotas, IP rules and policies are not evaluated natively**; keys they apply to are served by Python.
* **Rate-limit counters are shared by all threads**, so limits are exact; Python's are per worker process.
* The `Server` response header differs (uvicorn on one side), and native answers add `X-Served-By`.

## Tests

```bash
cargo test                              # unit tests
python dataplane/conformance/run.py     # same scenarios against Python and the data plane, plus the live-link
                                        # scenarios (needs the build above)
```

The conformance suite is the definition of "same behaviour": a scenario must pass on both before the data plane may
handle that kind of request.
