# Deploy with Docker Compose

**Goal:** run the gateway, Redis and the admin console with the repository's
`docker-compose.yml`, optionally with the sample backends, PostgreSQL, nginx
and monitoring.

**Before you start:** Docker with the Compose plugin (`docker compose`), and
ports 8000, 3001 and 6379 free (more for the optional profiles). This page is
written from `docker-compose.yml`, the Dockerfiles, `env.example` and
`tests/test_deployment_files.py`; the commands were not run for this release.

## Contents

- [What the Compose file runs](#what-the-compose-file-runs)
- [Things to know](#things-to-know)
- [Steps](#steps)
- [Run the sample backends](#run-the-sample-backends)
- [Point the gateway at your own backends](#point-the-gateway-at-your-own-backends)
- [Use PostgreSQL instead of SQLite](#use-postgresql-instead-of-sqlite)
- [Add monitoring](#add-monitoring)
- [Day-to-day commands](#day-to-day-commands)
- [Without Compose: one container or Kubernetes](#without-compose-one-container-or-kubernetes)
- [If it did not work](#if-it-did-not-work)

## What the Compose file runs

| Service | Profile | Port on the host | Notes |
|---|---|---|---|
| `gateway` (container `mcp_a2a_gateway`) | default | `${BIND_ADDRESS:-127.0.0.1}:${PORT:-8000}` (this machine only unless `BIND_ADDRESS=0.0.0.0`) | Built from `Dockerfile`: Python 3.11, one uvicorn process, user `gateway` (uid 1000). Reads `.env`. SQLite at `/app/data/gateway.db` on the `gateway_data` volume. |
| `redis` | default | `127.0.0.1:6379` | No password. Rate-limit counters (database 1) and the API-key cache (database 0). |
| `admin-ui` | default | `${BIND_ADDRESS:-127.0.0.1}:3001` (this machine only unless `BIND_ADDRESS=0.0.0.0`) | Console, served by nginx, which proxies `/admin` and `/api` to `gateway:8000`. |
| `postgres` | `production` | `127.0.0.1:${POSTGRES_PORT:-5432}` | PostgreSQL 15. Refuses to start while `POSTGRES_PASSWORD` is empty or starts with `change-me`. |
| `nginx` | `production` | 80 and 443 | TLS proxy; needs certificates in `nginx/ssl/`. |
| `prometheus` | `monitoring` | 9090 | Scrapes `gateway:8000/metrics`. |
| `grafana` | `monitoring` | 3000 | Refuses to start while `GRAFANA_ADMIN_PASSWORD` is empty or `admin`. No dashboards are provisioned. |
| `mcp-backend`, `a2a-backend`, `http-backend`, `websocket-backend`, `mqtt-broker`, `grpc-backend` | `backends` | 8001, 8002, 8003, 8080, 1883 and 9001, 50051 | Sample backends. No AG-UI, ACP or separate GraphQL sample in this file; GraphQL goes to the HTTP sample's `/graphql`. |

The gateway's `environment:` block, which overrides `.env`, sets:

- `REDIS_URL` and `RATE_LIMIT_REDIS_URL` to the `redis` service;
- `DATABASE_URL` to `sqlite+aiosqlite:////app/data/gateway.db`, unless `.env`
  or the shell sets `DATABASE_URL`;
- `MCP_BACKEND_URL`, `A2A_BACKEND_URL`, `HTTP_BACKEND_URL`,
  `WEBSOCKET_BACKEND_URL`, `MQTT_BROKER_URL`, `GRPC_BACKEND_URL` and
  `GRAPHQL_BACKEND_URL` to the sample backends' service names, unless
  `DOCKER_MCP_BACKEND_URL` and the other `DOCKER_` variants are set. The
  `*_BACKEND_URL` lines of `.env` (which `env.example` sets to `localhost` for
  running outside Docker) do not reach the container.

## Things to know

1. **Data survives recreation, not `down -v`.** The SQLite file is on the
   `gateway_data` volume. `docker compose down`, `up --build` and container
   recreation keep it; `docker compose down -v`, `make clean` and
   `docker volume rm` delete it. A relative SQLite `DATABASE_URL` in `.env`
   (older `env.example` files had one) puts the file back inside the
   container: delete that line.
2. **Published ports listen on localhost.** Every port the Compose file
   publishes, except nginx's 80 and 443, binds to `127.0.0.1` by default.
   To reach the gateway or the console from another machine without nginx,
   set `BIND_ADDRESS=0.0.0.0` in `.env`; that opens the port to the network
   and skips nginx's TLS and allow-list for `/metrics` and `/health/detailed`,
   so firewall it and set `OPS_ENDPOINTS_REQUIRE_ADMIN=true`.

## Steps

1. Create `.env` with real secrets:

   ```bash
   cp env.example .env
   python3 scripts/generate_secret_key.py --update-env
   echo "ADMIN_TOKEN=$(python3 scripts/generate_secret_key.py 2>/dev/null)" >> .env
   ```

   On Windows, use `python` in place of `python3` (the `python3` command is the
   Microsoft Store shortcut and fails). See the Windows note in the [quick start](../../README.md#quick-start).

2. Set `CORS_ORIGINS` to your front ends' origins if browsers on other origins
   call the gateway (the default allows the console on port 3001), and
   `TRUSTED_PROXIES` if a proxy sits in front
   ([Secure the gateway for production](secure-for-production.md)).

3. Start:

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

If the console started, open <http://localhost:3001> and sign in as `admin`
with your `ADMIN_TOKEN`.

## Run the sample backends

```bash
docker compose --profile backends up -d --build
```

`POST /api/users` and `POST /mcp/query` then reach the samples, as in the
[tutorials](../tutorials/getting-started.md). Start the backends before the
gateway, or restart the gateway afterwards, if you use MQTT: the gateway
connects to the broker only at start-up. The `websocket-backend` and
`grpc-backend` samples run, but the gateway cannot pass traffic to them.

## Point the gateway at your own backends

In `.env`, set the `DOCKER_` variant for each backend, with an address the
container can reach:

```bash
DOCKER_HTTP_BACKEND_URL=http://10.0.0.5:8003
DOCKER_MCP_BACKEND_URL=http://mcp.internal:8001
```

To reach a service on the Docker host, use the host's address on the Docker
network (on Docker Desktop, `host.docker.internal`). `AG_UI_BACKEND_URL` and
`ACP_BACKEND_URL` have no `DOCKER_` variant and are read from `.env` as usual.
Restart the gateway after the change.

## Use PostgreSQL instead of SQLite

1. In `.env`:

   ```bash
   POSTGRES_PASSWORD=<a generated password>
   DATABASE_URL=postgresql+asyncpg://gateway_user:<the same password>@postgres:5432/gateway
   ```

   Use the host `postgres`, not `localhost`. `gateway_user` and `gateway` are
   the defaults of `POSTGRES_USER` and `POSTGRES_DB`. PostgreSQL applies the
   password only when its volume is first initialised.

2. Start PostgreSQL and the gateway. The `production` profile also starts
   nginx, which needs certificates
   ([Run behind nginx with TLS](run-behind-nginx-tls.md)); to start only
   PostgreSQL with the default services, name the services:

   ```bash
   docker compose --profile production up -d --build postgres gateway redis
   ```

3. Optionally create or check the schema with Alembic (the gateway also
   creates its tables on first start):

   ```bash
   docker compose exec gateway alembic upgrade head
   ```

The gateway's `depends_on` names only Redis, so on a first start the gateway
may come up before PostgreSQL is ready; `restart: unless-stopped` restarts it.
`curl -s http://localhost:8000/ready` answers 200 once the database is
reachable. Data lives in the `postgres_data` volume.

## Add monitoring

```bash
docker compose --profile monitoring up -d
```

Set `GRAFANA_ADMIN_PASSWORD` in `.env` first. Prometheus is at
<http://localhost:9090> and scrapes the gateway every 15 seconds; with
`OPS_ENDPOINTS_REQUIRE_ADMIN=true`, give it the token (the commented
`authorization:` block in `prometheus.yml`). Grafana is at
<http://localhost:3000>; add Prometheus (`http://prometheus:9090`) as a data
source. See [Monitor the gateway](monitor.md).

## Day-to-day commands

```bash
docker compose logs -f gateway          # JSON logs
docker compose restart gateway          # pick up .env changes
docker compose down                     # stop; keeps volumes, including the database
```

Backups: [Back up and restore](back-up-and-restore.md). `make up`,
`make down`, `make logs` and `make monitoring` wrap the same commands.
`make clean` runs `docker compose down -v` (deleting every volume, including
`gateway_data`) and `docker system prune -f`.

## Without Compose: one container or Kubernetes

The image from the root `Dockerfile` runs on its own:

```bash
docker build -t agentanywhere-universal-gateway .
docker run -d --name gateway -p 8000:8000 --env-file .env \
  -v gateway_data:/app/data \
  -e DATABASE_URL=sqlite+aiosqlite:////app/data/gateway.db \
  agentanywhere-universal-gateway
```

It runs one `uvicorn` process as user `gateway` (uid 1000), listens on 8000,
has a `HEALTHCHECK` on `/health`, and creates `/app/data` owned by that user
for a mounted volume. Without a `DATABASE_URL`, the database is `./gateway.db`
inside the container and is lost with it. `docker-compose.yml` also sets CPU
and memory limits for each service (the gateway: 2 CPUs and 2G, reserving
0.5 CPU and 512M); set your own elsewhere.

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
| 502 `BACKEND_ERROR` for every request | The `backends` profile is not running, or a `DOCKER_*_BACKEND_URL` is unreachable | Start the profile, or fix the address. |
| Keys gone after an update | The volume was deleted, or `.env` sets a relative SQLite `DATABASE_URL` | Restore a backup; delete the line. |
| `postgres` or `grafana` exits at once | Its password is empty or a placeholder | Set `POSTGRES_PASSWORD` or `GRAFANA_ADMIN_PASSWORD`. |
| `port is already allocated` | Something on the host uses 8000, 3001, 6379 (or 80, 443, 5432, 3000, 9090, 8001 to 8003, 8080, 1883, 9001, 50051 with profiles) | Stop it, or change the left side of the port mapping. `PORT` in `.env` changes the gateway's host port. |
| The console's sign-in fails | Wrong password, or the gateway is not healthy | The password is `ADMIN_TOKEN` (or `SECRET_KEY` if `ADMIN_TOKEN` is empty). Check `docker compose logs gateway`. |
| `ports are not available: exposing port TCP 127.0.0.1:6379 ... Only one usage of each socket address` (Windows wording) | A Redis or Memurai already runs on the host and holds 6379 | Find it: `netstat -ano \| grep ":6379 "` in Git Bash (the last column is the process id), or `Get-NetTCPConnection -LocalPort 6379` in PowerShell. Stop that service (on Windows this needs an administrator prompt), or change the left side of the `redis` port mapping in `docker-compose.yml` to, for example, `127.0.0.1:6380:6379`. `PORT` in `.env` only changes the gateway's host port, not Redis's. The gateway reaches Redis inside the Compose network, so it is unaffected by the host port. |
| `/ready` answers 503 | The database is not reachable | Check `DATABASE_URL` and `docker compose ps postgres`. |
| 503 `MQTT broker not connected` | The broker started after the gateway | `docker compose restart gateway`. |
| `nginx` shows unhealthy | Its healthcheck requests `http://localhost/health`, which redirects | See [known limitations](../known-limitations.md#the-nginx-healthcheck-probably-reports-unhealthy). |
