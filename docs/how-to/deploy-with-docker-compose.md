# Deploy with Docker Compose

**Goal:** run the gateway, Redis and the admin console with the repository's
`docker-compose.yml`, optionally with the sample backends, PostgreSQL, nginx
and monitoring, and avoid the three traps in the shipped defaults.

**Before you start:** Docker with the Compose plugin (`docker compose`), and
ports 8000, 3001 and 6379 free (more for the optional profiles). This page is
written from `docker-compose.yml`, the Dockerfiles and `env.example`; the
commands were not run for this release.

## Contents

- [What the Compose file runs](#what-the-compose-file-runs)
- [The three traps](#the-three-traps)
- [Steps](#steps)
- [Run the sample backends](#run-the-sample-backends)
- [Use PostgreSQL instead of SQLite](#use-postgresql-instead-of-sqlite)
- [Add monitoring](#add-monitoring)
- [Day-to-day commands](#day-to-day-commands)
- [Without Compose: one container or Kubernetes](#without-compose-one-container-or-kubernetes)
- [If it did not work](#if-it-did-not-work)

## What the Compose file runs

| Service | Profile | Port on the host | Notes |
|---|---|---|---|
| `gateway` (container `mcp_a2a_gateway`) | default | `${PORT:-8000}` on all interfaces | Built from `Dockerfile`: Python 3.11, one uvicorn process, user uid 1000. Reads `.env`. |
| `redis` | default | `127.0.0.1:6379` | No password. Used for rate-limit counters (database 1) and the API-key cache (database 0). |
| `admin-ui` | default | 3001 on all interfaces | Console, served by nginx, which proxies `/admin` and `/api` to `gateway:8000`. |
| `postgres` | `production` | `127.0.0.1:${POSTGRES_PORT:-5432}` | PostgreSQL 15. |
| `nginx` | `production` | 80 and 443 | TLS proxy; needs certificates in `nginx/ssl/`. |
| `prometheus` | `monitoring` | 9090 | Scrapes `gateway:8000/metrics`. |
| `grafana` | `monitoring` | 3000 | No dashboards are provisioned. |
| `mcp-backend`, `a2a-backend`, `http-backend`, `websocket-backend`, `mqtt-broker`, `grpc-backend` | `backends` | 8001, 8002, 8003, 8080, 1883 and 9001, 50051 | Sample backends. No AG-UI, ACP or separate GraphQL sample in this file; GraphQL defaults to the HTTP sample's `/graphql`. |

The gateway's `environment:` block sets `REDIS_URL` and `RATE_LIMIT_REDIS_URL`
to the `redis` service, and gives each backend URL a default that uses the
service names, **unless `.env` sets that variable**. Compose substitutes
`${VAR:-default}` from your shell and from `.env`.

## The three traps

1. **Backend URLs from `env.example` break routing.** `env.example` sets
   `MCP_BACKEND_URL=http://localhost:8001`, `HTTP_BACKEND_URL=http://localhost:8003`
   and so on. A `.env` copied from it overrides the Compose defaults, and
   inside the container `localhost` is the gateway itself, so requests answer
   502. Fix: edit `.env` as in step 2 below.
2. **The database is inside the container.** `DATABASE_URL` defaults to
   `sqlite+aiosqlite:///./gateway.db`, which is `/app/gateway.db` in the
   container. Only `./logs` is mounted. Recreating the container (for example
   `docker compose down` then `up`, or `up --build` after a change) loses every
   API key, tenant and route. Fix: use PostgreSQL, or back the file up.
3. **Revocation is delayed.** Redis is on, so validated keys are cached for
   five minutes, and revoked keys keep working for up to that long. See
   [Create, rotate and revoke API keys](manage-api-keys.md#revoke-a-key).

## Steps

1. Create `.env` with real secrets:

   ```bash
   cp env.example .env
   python3 scripts/generate_secret_key.py --update-env
   echo "ADMIN_TOKEN=$(python3 scripts/generate_secret_key.py 2>/dev/null)" >> .env
   ```

2. Point the backend URLs at addresses the container can reach. In `.env`,
   either delete the backend lines so the Compose defaults apply, or set them
   to service names:

   ```bash
   MCP_BACKEND_URL=http://mcp-backend:8001
   A2A_BACKEND_URL=http://a2a-backend:8002
   HTTP_BACKEND_URL=http://http-backend:8003
   GRAPHQL_BACKEND_URL=http://http-backend:8003/graphql
   MQTT_BROKER_URL=mqtt://mqtt-broker:1883
   ```

   For your own backends, use their host names or addresses. To reach a
   service on the Docker host, use the host's address on the Docker network
   (on Docker Desktop, `host.docker.internal`).

3. Set `CORS_ORIGINS` to your front ends' origins (see
   [Secure the gateway for production](secure-for-production.md#cors)).

4. Start:

   ```bash
   docker compose up -d --build
   ```

**Check:**

```bash
docker compose ps                       # gateway "healthy" after about 30 seconds
curl -s http://localhost:8000/health    # {"status":"healthy",...}
ADMIN=$(grep '^ADMIN_TOKEN=' .env | tail -n 1 | cut -d= -f2-)
curl -s -X POST "http://localhost:8000/admin/api-keys?name=first-key" -H "Authorization: Bearer $ADMIN"
```

Open <http://localhost:3001> and sign in as `admin` with your `ADMIN_TOKEN`.

## Run the sample backends

```bash
docker compose --profile backends up -d --build
```

With step 2 done, `POST /api/users` and `POST /mcp/query` reach the samples,
as in the [tutorials](../tutorials/getting-started.md). Start the backends
before the gateway, or restart the gateway afterwards, if you use MQTT: the
gateway connects to the broker only at start-up. The `websocket-backend` and
`grpc-backend` samples run, but the gateway cannot pass traffic to them.

## Use PostgreSQL instead of SQLite

1. In `.env`:

   ```bash
   POSTGRES_PASSWORD=<a strong password>
   DATABASE_URL=postgresql+asyncpg://gateway_user:<the same password>@postgres:5432/gateway
   ```

   `gateway_user` and `gateway` are the defaults of `POSTGRES_USER` and
   `POSTGRES_DB`.

2. Start PostgreSQL and the gateway. The `production` profile also starts
   nginx, which needs certificates
   ([Run behind nginx with TLS](run-behind-nginx-tls.md)); to start only
   PostgreSQL with the default services, name the services:

   ```bash
   docker compose --profile production up -d --build postgres gateway redis admin-ui
   ```

3. Do **not** run `alembic upgrade head` (see
   [known limitations](../known-limitations.md#alembic-migrations-do-not-match-the-models)).
   The gateway creates its tables on first start.

The gateway's `depends_on` names only Redis, so on a first start the gateway
may come up before PostgreSQL is ready; `restart: unless-stopped` restarts it.
Check `curl -s http://localhost:8000/ready`, which answers 200 once the
database is reachable. Data lives in the `postgres_data` volume.

## Add monitoring

```bash
docker compose --profile monitoring up -d
```

Prometheus is at <http://localhost:9090> and scrapes the gateway every 15
seconds. Grafana is at <http://localhost:3000>; set `GRAFANA_ADMIN_PASSWORD`
in `.env` first, then add Prometheus (`http://prometheus:9090`) as a data
source. See [Monitor the gateway](monitor.md).

## Day-to-day commands

```bash
docker compose logs -f gateway          # JSON logs
docker compose restart gateway          # pick up .env changes
docker compose cp gateway:/app/gateway.db ./gateway.db.bak   # back up SQLite
docker compose down                     # stop; keeps volumes, loses the SQLite file in the container
```

`make up`, `make down`, `make logs` and `make monitoring` wrap the same
commands. `make clean` runs `docker compose down -v` (deleting the Redis,
PostgreSQL, Prometheus and Grafana volumes) and `docker system prune -f`.

## Without Compose: one container or Kubernetes

The image from the root `Dockerfile` runs on its own:

```bash
docker build -t agentanywhere-universal-gateway .
docker run -d --name gateway -p 8000:8000 --env-file .env \
  -e DATABASE_URL=postgresql+asyncpg://user:password@db-host:5432/gateway \
  -e RATE_LIMIT_REDIS_URL=redis://redis-host:6379/1 \
  agentanywhere-universal-gateway
```

It runs one `uvicorn` process as user `gateway` (uid 1000), listens on 8000
and has a `HEALTHCHECK` on `/health`. `docker-compose.yml` also sets CPU and
memory limits for each service (the gateway: 2 CPUs and 2G, reserving 0.5 CPU
and 512M); set your own when you run the image elsewhere.

There are no Kubernetes manifests or Helm chart in the repository. What a
manifest would use: `GET /live` or `GET /health` for liveness, `GET /ready`
for readiness (503 when the database is unreachable), environment variables
for configuration ([configuration reference](../reference/configuration.md)),
PostgreSQL rather than SQLite, and Redis for rate-limit counters as soon as
there is more than one replica
([design decisions](../explanation/design-decisions.md#running-more-than-one-worker-or-replica)).

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| 502 `BACKEND_ERROR` for every request | `localhost` backend URLs from `env.example` | Step 2. |
| Keys stopped working after an update | The container was recreated and the SQLite file went with it | Use PostgreSQL; restore from your copy. |
| `port is already allocated` | Something on the host uses 8000, 3001, 6379 (or 80, 443, 5432, 3000, 9090, 8001 to 8003, 8080, 1883, 9001, 50051 with profiles) | Stop it, or change the left side of the port mapping. `PORT` in `.env` changes the gateway's host port. |
| The console's sign-in fails | Wrong password, or the gateway is not healthy | The password is `ADMIN_TOKEN` (or `SECRET_KEY` if `ADMIN_TOKEN` is empty). Check `docker compose logs gateway`. |
| `/ready` answers 503 | The database is not reachable | Check `DATABASE_URL` and `docker compose ps postgres`. |
| 503 `MQTT broker not connected` | The broker started after the gateway | `docker compose restart gateway`. |
