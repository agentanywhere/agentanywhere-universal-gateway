# Deployment

> Reference material, written from the files in this repository. It has not been
> checked against a particular cloud or cluster. See also
> [PRODUCTION_SETUP.md](PRODUCTION_SETUP.md) for the settings to change first and
> [UPGRADING.md](UPGRADING.md) for the database schema.

## What the repository provides

| You want | What exists |
|---|---|
| A single container | `Dockerfile` (Python 3.11 slim, runs as a non-root user, listens on 8000, has a `HEALTHCHECK` on `/health`) |
| Several containers on one host | `docker-compose.yml` (gateway, Redis and the admin console by default; Postgres and Nginx under `--profile production`; Prometheus and Grafana under `--profile monitoring`; sample backends under `--profile backends`) |
| A reverse proxy with TLS | `nginx/nginx.conf` and `nginx/Dockerfile` |
| Metrics scraping | `prometheus.yml` (scrapes `gateway:8000/metrics`) |
| A published image | `.github/workflows/docker-publish.yml` pushes an image to Docker Hub when a `v*` tag is pushed |
| Kubernetes | **No manifests, Helm chart or Kustomize files.** See below for what in the application is suited to it. |

## Single container

```bash
docker build -t agentanywhere-universal-gateway .
docker run -d --name gateway -p 8000:8000 --env-file .env \
  -e DATABASE_URL=postgresql+asyncpg://user:password@db-host:5432/gateway \
  -e REDIS_URL=redis://redis-host:6379/0 \
  -e RATE_LIMIT_REDIS_URL=redis://redis-host:6379/1 \
  agentanywhere-universal-gateway
```

Notes:

- The image starts one `uvicorn` process (`CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]`).
  The `WORKERS` setting is only used when the application is started as
  `python -m app.main` or `python app/main.py` with `ENVIRONMENT=production`,
  not by this image.
- Without `DATABASE_URL` the gateway uses `./gateway.db`, a SQLite file inside
  the container. It is lost when the container is removed, and with it every
  API key. Use PostgreSQL, or mount a volume on `/app/data` (the image creates
  that directory, owned by the container's user, uid 1000) and set
  `DATABASE_URL=sqlite+aiosqlite:////app/data/gateway.db`, as
  `docker-compose.yml` does:
  `docker run ... -v gateway_data:/app/data -e DATABASE_URL=sqlite+aiosqlite:////app/data/gateway.db ...`
- Inside a container, `localhost` in a `*_BACKEND_URL` or `REDIS_URL` means the
  container itself. Use the backend's host name or address.

## Docker Compose on one host

```bash
cp env.example .env
python3 scripts/generate_secret_key.py --update-env
docker compose up -d --build                      # gateway, Redis, admin console
docker compose --profile production up -d --build # adds Postgres and Nginx
```

Read [PRODUCTION_SETUP.md](PRODUCTION_SETUP.md) first: it lists the default
credentials and published ports in this file. The `production` profile needs
`POSTGRES_PASSWORD` in `.env`, and the `monitoring` profile needs
`GRAFANA_ADMIN_PASSWORD`: those containers refuse to start (see
`docker compose logs postgres` / `grafana`) while the value is empty or a
placeholder. The default `docker compose up` does not need either.

How the Compose file configures the gateway container:

- **Database.** `DATABASE_URL` defaults to
  `sqlite+aiosqlite:////app/data/gateway.db`, on the named volume
  `gateway_data` mounted at `/app/data`. The file, and every API key in it,
  survives `docker compose down`, `up --build` and container recreation. It is
  deleted by `docker compose down -v` or `docker volume rm`. A `DATABASE_URL`
  in `.env` or the shell replaces the default; for the `production` profile's
  PostgreSQL use `postgresql+asyncpg://gateway_user:<password>@postgres:5432/gateway`
  (host `postgres`, not `localhost`). Do not set a relative SQLite path such as
  `sqlite+aiosqlite:///./gateway.db` there: it puts the file back inside the
  container. `.env` files copied from an older `env.example` contain exactly
  that line; delete it.
- **Backends.** The `environment:` block sets `MCP_BACKEND_URL`,
  `A2A_BACKEND_URL`, `HTTP_BACKEND_URL`, `WEBSOCKET_BACKEND_URL`,
  `MQTT_BROKER_URL`, `GRPC_BACKEND_URL` and `GRAPHQL_BACKEND_URL` to the sample
  backends' service names (`http://mcp-backend:8001`, ...). `environment:`
  overrides `env_file:`, so the `localhost` values that `env.example` uses for
  running outside Docker do not reach the container (there `localhost` would
  be the gateway itself, and every proxied request would get 502). The sample
  backends run with `--profile backends`. For your own backends set
  `DOCKER_MCP_BACKEND_URL`, `DOCKER_HTTP_BACKEND_URL` and so on in `.env`.
  `AG_UI_BACKEND_URL` and `ACP_BACKEND_URL` have no sample service and are read
  from `.env` as usual.

### Backing up the database volume

The default database is one SQLite file on the `gateway_data` volume. Compose
prefixes the volume with the project name (by default the directory name), so
find it with `docker volume ls | grep gateway_data`.

Online copy, while the gateway runs (SQLite's backup API gives a consistent
snapshot):

```bash
docker compose exec gateway python -c "import sqlite3; s=sqlite3.connect('/app/data/gateway.db'); d=sqlite3.connect('/app/data/backup.db'); s.backup(d); d.close(); s.close()"
docker compose cp gateway:/app/data/backup.db ./gateway-backup-$(date +%F).db
docker compose exec gateway rm /app/data/backup.db
```

Whole volume, with the gateway stopped:

```bash
docker compose stop gateway
docker run --rm -v <project>_gateway_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/gateway-data-$(date +%F).tgz -C /data .
docker compose start gateway
```

To restore, stop the gateway, extract the archive into the volume the same way
(`tar xzf ... -C /data`), and start it again. New API keys are stored as a
hash, but keys created before this release stay in plaintext until first used:
store backups as you would the keys themselves.

## Behind a reverse proxy

The gateway speaks plain HTTP on port 8000. Terminate TLS in front of it.
`nginx/nginx.conf` is a working starting point. It is a server-level file
(`upstream` and `server` blocks): `nginx/Dockerfile` and `docker-compose.yml`
install it as `/etc/nginx/conf.d/default.conf`, inside the stock `http {}`
block. (Earlier versions mounted it as `/etc/nginx/nginx.conf`, where nginx
refuses to load it.)

- it redirects HTTP to HTTPS and expects `nginx/ssl/cert.pem` and
  `nginx/ssl/key.pem`;
- it proxies everything to `gateway:8000` (the `upstream gateway_backend` block;
  add servers there for several replicas);
- it forwards `Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`,
  `X-Forwarded-Host` and `X-Forwarded-Port`. `X-Forwarded-For` is overwritten
  with the address nginx sees (`$remote_addr`), not appended to, so a client
  cannot choose it. The gateway reads it only from addresses in
  `TRUSTED_PROXIES`: set that to nginx's address (under Compose, the
  `gateway-network` subnet), or the gateway sees every request as coming from
  nginx;
- it allows request bodies up to 100M and read timeouts of 300 seconds;
- `/metrics` and `/health/detailed` are answered only for loopback and
  private addresses (`127.0.0.1`, `::1`, `10.0.0.0/8`, `172.16.0.0/12`,
  `192.168.0.0/16`, `fc00::/7`); everyone else gets 403. To change who may
  read them, edit the `allow` lines in both locations (one address or CIDR per
  line) and keep `deny all;` last. The check uses the address nginx sees: if
  another proxy or load balancer is in front, configure `set_real_ip_from` /
  `real_ip_header` first. Connections relayed by Docker's userland proxy
  appear to come from the bridge gateway, which is inside `172.16.0.0/12`.
- `/docs`, `/redoc` and `/openapi.json` go through the catch-all
  `location /`. The gateway serves them only with `DEBUG=true` and answers
  404 otherwise.

The allow-list does not cover clients that reach port 8000 directly (the
Compose file publishes it on all interfaces). For a check that does not depend
on the network path, set `OPS_ENDPOINTS_REQUIRE_ADMIN=true`: the gateway then
asks for the admin credential on both endpoints, and Prometheus needs the token
(see `prometheus.yml`).

If you use another proxy, do the same: pass the original host and client
address, allow large bodies and long reads if you stream, and keep `/metrics`
and `/health/detailed` off the internet.

## Kubernetes

There are no manifests in this repository. The application exposes what a
manifest would use:

- liveness: `GET /live` or `GET /health`
- readiness: `GET /ready` (returns 503 if the database check fails)
- configuration: environment variables (see [CONFIGURATION.md](CONFIGURATION.md))
- the image from the `Dockerfile`, which runs as a non-root user (uid 1000)

You would write the Deployment, Service and secrets yourself. Use PostgreSQL
and Redis, not SQLite and in-memory counters, as soon as you run more than one
replica (next section).

## More than one process or replica

Some state lives in the memory of each gateway process:

- **SQLite.** The default database is one file. Several processes on one host
  can contend for it, and replicas on different hosts cannot share it. Use
  PostgreSQL.
- **Rate-limit counters.** Without `RATE_LIMIT_REDIS_URL` each process counts
  separately, so with N processes the effective limit is up to N times higher
  than configured. Set `RATE_LIMIT_REDIS_URL`.
- **Tenant quotas.** Usage is counted after the response, so a burst of
  concurrent requests can pass a quota by a few requests.

Dynamic routes added through the admin API are stored in the database and each
process reloads them periodically (`DYNAMIC_ROUTES_ENABLED` and the reload
`DYNAMIC_ROUTES_REFRESH_SECONDS`, default 15); the process that handles the write applies it
immediately, the others pick it up on their next reload.

## Troubleshooting

Common start-up and request failures (port in use, 401s, 502/503, Redis) are
listed in [QUICKSTART.md](QUICKSTART.md#if-something-goes-wrong). For the
container:

```bash
docker compose ps                  # is the gateway healthy?
docker compose logs -f gateway     # startup errors, backend failures
curl http://localhost:8000/ready   # 503 means the database check failed
```
