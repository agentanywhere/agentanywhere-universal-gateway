# AgentAnywhere data plane (experimental)

A Rust proxy that sits in front of the Python gateway and handles the hot path itself: it authenticates API keys,
applies per-key rate limits and forwards plain HTTP requests, streaming bodies in both directions. Everything else is
handed to the Python gateway unchanged. Design, goals and phases: [DESIGN.md](DESIGN.md).

**Status: phase 1, not for production.** The limits below are real; read them before using it.

## Build and run

```bash
cd dataplane
cargo build --release
./target/release/dataplane --listen 127.0.0.1:8000 --snapshot snapshot.json \
    --backend http://127.0.0.1:9100 --control http://127.0.0.1:8001
```

`--backend` is the HTTP backend, `--control` the Python gateway on its internal port. Run the Python gateway with
`TRUSTED_PROXIES` set to the data plane's address so it sees the real client IP (the data plane adds `X-Forwarded-For`).

`snapshot.json` lists the keys as SHA-256 digests (never plaintext) with their limits:

```json
{"keys": [{"hash": "<sha256 hex of the key>", "id": "key-1", "rate_limit_per_minute": 100, "rate_limit_per_hour": 1000}]}
```

Optional fields: `is_active`, `expires_at` (epoch seconds), `native_ok` (false hands the key's requests to Python),
`fallthrough_prefixes`, `rate_limit_enabled`, and `api_versions` (defaults to versions 1 and 2, as shipped).

## What it handles itself

A request is served natively only when **all** of these hold: the path is `/api/...` and not another protocol's;
no `fallthrough_prefixes` entry matches; the client is not a browser (`Accept: text/html`); `X-API-Version` or
`Accept-Version` resolves to an active version with no backend of its own; and the `X-API-Key` is in the snapshot,
active, unexpired and `native_ok`. Anything else, including unknown keys, `Authorization: Bearer` credentials,
`/health`, `/admin`, `/oauth2`, WebSocket and the other protocols, goes to the Python gateway untouched, so its answer is
the answer.

## Known gaps (why this is not production-ready)

* **The snapshot is a static file.** There is no live link to the control plane yet, so a key revoked or changed after
  start-up is still honoured natively until the data plane restarts. The control-plane channel with change events and
  fail-closed re-sync is the next step.
* **No request log, usage counters or Prometheus metrics for native requests.** They are recorded only for requests
  that go through Python.
* **Rate-limit webhooks and alerts are not fired** when the data plane returns a 429.
* **Tenant quotas, IP rules and policies are not evaluated natively.** Keys they apply to must be marked
  `native_ok: false` in the snapshot; nothing produces that automatically yet.
* **No per-backend circuit breaker**, and a failed upstream gives a simplified 502 body.
* **Rate-limit counters are shared by all threads**, so limits are exact; Python's are per worker process.
* The `Server` response header differs (uvicorn on one side).

## Tests

```bash
cargo test                              # unit tests
python dataplane/conformance/run.py     # same scenarios against Python and the data plane (needs the build above)
```

The conformance suite is the definition of "same behaviour": a scenario must pass on both before the data plane may
handle that kind of request.
