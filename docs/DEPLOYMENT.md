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
- Without `DATABASE_URL` the gateway uses a SQLite file inside the container.
  It is lost when the container is removed, and with it every API key. Use
  PostgreSQL, or point `DATABASE_URL` at a SQLite file on a mounted volume that
  the container's user (uid 1000) can write.
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
credentials and published ports in this file.

## Behind a reverse proxy

The gateway speaks plain HTTP on port 8000. Terminate TLS in front of it.
`nginx/nginx.conf` is a working starting point:

- it redirects HTTP to HTTPS and expects `nginx/ssl/cert.pem` and
  `nginx/ssl/key.pem`;
- it proxies everything to `gateway:8000` (the `upstream gateway_backend` block;
  add servers there for several replicas);
- it forwards `Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`,
  `X-Forwarded-Host` and `X-Forwarded-Port`;
- it allows request bodies up to 100M and read timeouts of 300 seconds;
- its `/metrics` location has an IP allow-list that is **commented out**, and
  `/health/detailed`, `/docs`, `/redoc` and `/openapi.json` go through the
  catch-all `location /`. Restrict the ones you do not want public.

If you use another proxy, do the same: pass the original host and client
address, allow large bodies and long reads if you stream, and block `/metrics`
and `/health/detailed` from the internet.

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
