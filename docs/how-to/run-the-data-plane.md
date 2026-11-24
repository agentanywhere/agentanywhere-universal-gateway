# Run the Rust data plane (experimental)

**Goal:** put the optional Rust data plane in front of the gateway, so that it
serves the common requests itself and hands everything else to the Python
gateway.

**Before you start:** a gateway that already works on its own (see
[your first authenticated request](../tutorials/getting-started.md)), and the
Rust toolchain (stable, from [rustup.rs](https://rustup.rs)). Nothing else:
the data plane has no other dependencies.

The data plane is experimental and not for production. Read
[its README](https://github.com/agentanywhere/agentanywhere-universal-gateway/blob/main/dataplane/README.md)
for what it serves and its limits before you use it.

## Installation has not changed

You need Rust only to try the data plane. The
[Docker Compose quick start](deploy-with-docker-compose.md), the Python
install and the Docker image are unchanged: they run the Python gateway
alone, as before, and do not build or start the data plane.

## Steps

1. Build it from the repository root:

   ```bash
   cd dataplane
   cargo build --release
   cd ..
   ```

   The binary is `dataplane/target/release/dataplane`.

2. Move the Python gateway to an internal port. Give it a shared secret, and
   trust the data plane as the proxy in front of it:

   ```bash
   DATAPLANE_TOKEN=change-me TRUSTED_PROXIES=127.0.0.1 uvicorn app.main:app --port 8001
   ```

   Use a long random value for the token. `/internal/dataplane`, which the data
   plane reads its keys, rules and backends from, is served only when
   `DATAPLANE_TOKEN` is set.

3. Start the data plane on the public port, pointing at your HTTP backend (here
   the tutorial's sample backend on port 8003) and at the Python gateway:

   ```bash
   ./dataplane/target/release/dataplane --listen 0.0.0.0:8000 \
       --backend http://127.0.0.1:8003 \
       --control http://127.0.0.1:8001 --control-token change-me
   ```

   The MCP, A2A, AG-UI, ACP and GraphQL backends come from the Python gateway's
   own settings, so they are set in one place.

4. Send a request as before, to port 8000:

   ```bash
   curl -s http://127.0.0.1:8000/api/users -H "X-API-Key: $API_KEY"
   ```

   Callers see the same answers, refusals and headers as from the Python
   gateway. Keys, rules and the console stay in the Python gateway; a change
   there reaches the data plane within about one sync interval
   (`--sync-interval-ms`, 500 by default).

## Check it is working

The data plane has its own metrics on an address of its own:

```bash
./dataplane/target/release/dataplane --listen 0.0.0.0:8000 \
    --backend http://127.0.0.1:8003 \
    --control http://127.0.0.1:8001 --control-token change-me \
    --metrics-listen 127.0.0.1:9101
curl http://127.0.0.1:9101/metrics
```

`dataplane_requests_served_total` counts what it answered itself and
`dataplane_requests_handed_over_total` what it passed to the Python gateway.
`dataplane_snapshot_fresh` is `1` while it is current enough to serve; until
then, and whenever the Python gateway stops answering for
`--max-staleness-ms` (5000 by default), it hands every request over.

## Stop using it

Stop the data plane and start the Python gateway on the public port again,
without `DATAPLANE_TOKEN`. Nothing is stored in the data plane.

## Run its tests

```bash
cd dataplane && cargo test && cd ..
python dataplane/conformance/run.py
```

The second command starts both planes and checks that they answer the same
scenarios the same way. CI runs both on every push.
