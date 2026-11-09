# Test performance

**Goal:** measure how much latency the gateway adds and how many requests per
second it handles on your hardware, in a way you can repeat and compare.

**Before you start:** the project's dependencies installed
(`pip install -r requirements.txt`). Nothing else: the suite starts its own
sample backend and gateway on free local ports, with a temporary database, no
Redis and quiet logs.

This project does not publish performance figures. The numbers depend on the
machine, the database, the number of workers and what the backend does, so
measure your own set-up.

## Steps

1. Run the suite from the repository root:

   ```bash
   python -m perf.suite --duration 20 --concurrency 32 --report perf-report.md --json perf.json
   ```

   It runs five scenarios, each for `--duration` seconds after a `--warmup`
   that is discarded:

   | Scenario | What it shows |
   |---|---|
   | `backend direct` | The sample backend with no gateway: the baseline. |
   | `gateway /health` | The gateway's own request path, no auth and no backend call. |
   | `auth rejected` | A request without a key, answered 401. |
   | `proxy GET /api/users` | A keyed GET through the gateway to the backend. |
   | `proxy POST /api/users` | A keyed POST through the gateway to the backend. |

2. Read the table. `err` counts transport failures and any status you did not
   expect (the `auth rejected` scenario expects 401). The line below the table
   is the gateway's added latency: the proxied GET minus the same GET sent
   straight to the backend.

3. Try more gateway processes, which is how the gateway scales:

   ```bash
   python -m perf.suite --workers 4 --concurrency 64
   ```

4. Gate a CI job on a limit. The command exits with status 1 when a limit is
   exceeded:

   ```bash
   python -m perf.suite --max-p95-ms 250 --max-error-rate 0.01
   ```

## Test a gateway that is already running

```bash
API_KEY=...   # a key with limits high enough not to interfere, see below
python -m perf.load_test --url http://127.0.0.1:8000 --path /api/users \
  --api-key "$API_KEY" --concurrency 32 --duration 20 --max-p95-ms 250
```

Use `--method POST --body '{"name":"Ada","email":"ada@example.com"}'` for a
write, `--header 'Name: value'` for extra headers, and `--expect-status 429` or
`401` to measure a deliberate rejection. Create the key with a high
`rate_limit_per_minute` and `rate_limit_per_hour`
([API keys](manage-api-keys.md)); otherwise you measure the rate limiter, and
most of the requests are 429. Never point this at a production gateway.

## Benchmark on separate machines

One machine running the load generator, the gateway and the backend measures all three at once. For
figures you can compare, use four hosts on one private network (gateway, backend, load generator, and
PostgreSQL if you want to compare databases), and send load at a constant rate with
[vegeta](https://github.com/tsenart/vegeta). The scripts are in `perf/fleet/`.

1. On each host: Python 3.12, `git`, and this repository in `~/gw` (the gateway and backend hosts also need
   `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`). On the load generator install
   `vegeta`; on the gateway host install `sysstat` and `py-spy` if you want CPU records and profiles.
2. On the gateway host create `~/gw/run/secrets.env` with `ADMIN_TOKEN=` and `SECRET_KEY=` (long random values,
   `chmod 600`), and, if you test PostgreSQL, `~/gw/run/pg.url` holding a `postgresql+asyncpg://` URL.
3. Start the benchmark backend on its host: `WORKERS=3 bash perf/fleet/backend_up.sh`. It answers every request
   with the same 11 bytes, so what you measure through the gateway above the direct call is the gateway's.
4. Describe the hosts in a `fleet.json` (see the docstring of `perf/fleet/ladder.py`) and run the ladder from your
   own machine:

   ```bash
   FLEET=fleet.json python -m perf.fleet.ladder --label run1 \
     --configs nodb,sqlite,sqlite-wal,postgres,postgres-batched --workers 4,8 \
     --rates 100,200,400,600,800 --duration 45 --warmup 10 \
     --gateway-env "API_KEY_LOCAL_CACHE_TTL_SECONDS=5 AI_ANALYTICS_ENABLED=false" --out results.jsonl
   python -m perf.fleet.report results.jsonl --label run1
   ```

   It restarts the gateway for every configuration (and refuses to continue if fewer workers started than you
   asked for), runs the same constant-rate attack straight at the backend as a baseline, records gateway-host
   CPU and memory with `mpstat` and `pidstat`, and writes one JSON line per run. `report` turns the file into one
   table per worker count.

| Configuration | What it is |
|---|---|
| `nodb` | request-log writes off (`REQUEST_LOG_MODE=off`); the ceiling with no persistence on the request path |
| `sqlite` | SQLite with its defaults, one write per request |
| `sqlite-wal` | SQLite with `SQLITE_JOURNAL_MODE=WAL`, `SQLITE_SYNCHRONOUS=NORMAL`, `SQLITE_BUSY_TIMEOUT_MS=5000` |
| `postgres` | PostgreSQL, one write per request |
| any of these plus `-batched` | the same, with `REQUEST_LOG_MODE=batched` (rows written in batches by one task) |

Run each rate for at least 45 seconds and discard the first 10, repeat a cell that looks odd, and keep the
backend baseline: if the backend itself slows down at a rate, the gateway figure at that rate is not the
gateway's. Near the point where a configuration stops coping, results vary from run to run.

## Read the results fairly

- The load generator, the gateway and the backend share one machine in the
  suite, so they compete for the same cores. Figures are indicative, not a
  capacity limit. For capacity, run the generator on a separate machine.
- The load is closed-loop: each worker waits for its answer before sending the
  next request, so a slow server lowers the request rate instead of queueing.
  Latency under a fixed arrival rate can look worse.
- Compare runs only with the same settings. `--report` records the Python
  version, platform, CPU count, workers, concurrency, duration and git commit
  next to the figures; keep that table if you share a result.
- Close other busy programs, and run each scenario long enough (20 seconds or
  more) for the high percentiles to settle.

## Run it as a test

`tests/perf/test_performance.py` runs a short version of the suite and checks
that every scenario serves requests with no errors, that proxied calls succeed
and that unauthenticated calls get 401. Its latency and throughput limits are
loose on purpose. It is skipped in a normal `pytest` run:

```bash
pytest -m performance tests/perf
PERF_MAX_P95_MS=250 PERF_MIN_RPS=200 pytest -m performance tests/perf   # tighter, on a dedicated runner
```

The load generator's own maths (percentiles, thresholds, error counting) is
covered by `tests/test_perf_load_test.py`, which runs with the rest of the
suite.

**Check:** the suite prints a table with no errors in the gateway scenarios,
and a `gateway overhead` line.
