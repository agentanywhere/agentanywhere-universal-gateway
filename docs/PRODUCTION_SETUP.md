# Production Checklist

> Reference material. It is kept in step with the code by the automated tests and by
> review, not by a line-by-line manual check. If this page and the gateway disagree,
> the gateway is right; please open an issue.

This page lists the files and scripts in this repository that help when you run
the AgentAnywhere Universal Gateway outside a laptop, and the settings you
should change first. It describes what the files contain. It is not a statement
that a deployment built from them has been audited or hardened. Review the
configuration yourself before you put real traffic or real data through it.

For packaging, process and reverse-proxy notes see [DEPLOYMENT.md](DEPLOYMENT.md).
For moving an existing database to a newer release see [UPGRADING.md](UPGRADING.md).

## Known limitations

These are also in the [README](../README.md). Read them before you deploy.

- **gRPC and WebSocket do not work** (see [PROTOCOLS.md](PROTOCOLS.md)).
- **API keys are stored in plaintext.** The database keeps each key next to its
  SHA-256 hash, and `GET /admin/api-keys` returns keys in full. With Redis
  enabled, keys also appear in Redis key names. Protect the database, Redis and
  the admin credential accordingly.
- **`SECRET_KEY` has three jobs.** It signs tokens, it is accepted as the admin
  credential when `ADMIN_TOKEN` is empty, and it is accepted as an API key. Set
  a separate `ADMIN_TOKEN` and treat both as secrets.
- **`/metrics` and `/health/detailed` need no credential.** Anyone who can reach
  the gateway can read them. The same is true of `/docs`, `/redoc` and
  `/openapi.json`. See the list of public paths in
  [API_REFERENCE.md](API_REFERENCE.md#authentication).
- **Tenant usage is counted after the response.** A burst of concurrent
  requests can pass a tenant quota by a few requests.
- **A WebSocket API key in the query string is written to the access log.**
- **The analytics endpoints (`/admin/ai/*`) are experimental.**

## Defaults to change before you expose anything

| Where | Default as shipped | What to do |
|---|---|---|
| `SECRET_KEY` | A placeholder starting with `change-me`. The gateway refuses it as an admin credential. In `ENVIRONMENT=production` it prints a warning but still starts. | Run `python3 scripts/generate_secret_key.py --update-env`. |
| `ADMIN_TOKEN` | Empty, so `SECRET_KEY` is the admin credential. | Set a separate random value. |
| `CORS_ORIGINS` | Allows every origin (`env.example` ships `["*"]`, with `CORS_ALLOW_CREDENTIALS=true`). | Set it to the exact origins of your front ends, as a JSON array. |
| `/metrics`, `/health/detailed` | Open to anyone who can reach the gateway. | Block them at your firewall or reverse proxy, or allow only your monitoring network. In `nginx/nginx.conf` the `/metrics` block has an IP allow-list that is commented out; `/health/detailed` is passed through by the catch-all `location /`. |
| Redis in `docker-compose.yml` | No password. Port 6379 is published on `127.0.0.1` only. | Keep it on the Compose network or localhost. If you need remote access, add a password and a firewall rule. |
| PostgreSQL in `docker-compose.yml` (`production` profile) | `POSTGRES_PASSWORD` falls back to the public placeholder `change-me-in-production`. Port 5432 is published on `127.0.0.1` only. | Set `POSTGRES_PASSWORD` in `.env` before you start the profile. |
| Grafana in `docker-compose.yml` (`monitoring` profile) | Admin user `admin`, password `admin` unless `GRAFANA_ADMIN_PASSWORD` is set. Port 3000 is published on all interfaces. | Set `GRAFANA_ADMIN_PASSWORD` in `.env`, and restrict port 3000. |
| Prometheus in `docker-compose.yml` (`monitoring` profile) | No authentication. Port 9090 is published on all interfaces. | Restrict port 9090. |
| Admin console (port 3001) | Published on all interfaces; sign-in needs the admin credential. | Put it behind your reverse proxy and restrict who can reach it. |
| Sample backends (`backends` profile) | No authentication; ports 8001-8003, 8080, 1883, 9001 and 50051 are published on all interfaces. | Use them for testing only. |

## Checklist

Tick these off yourself. Nothing in this repository checks them for you.

- [ ] `SECRET_KEY` is a generated random value, and `ADMIN_TOKEN` is a different one.
- [ ] `CORS_ORIGINS` lists your real origins and not `*`.
- [ ] TLS is terminated in front of the gateway (see below), with a real certificate.
- [ ] `/metrics`, `/health/detailed` and, if you want them private, `/docs`,
      `/redoc` and `/openapi.json` are blocked or restricted at the proxy or firewall.
- [ ] The database is PostgreSQL, or you accept SQLite's limits (see [DEPLOYMENT.md](DEPLOYMENT.md)).
- [ ] The database file or volume is backed up, and you know how to restore it.
      Remember it holds API keys in plaintext.
- [ ] Redis and PostgreSQL are not reachable from outside the host or network.
- [ ] Default passwords (Grafana, PostgreSQL) are changed.
- [ ] Log rotation is set up.
- [ ] You have read [UPGRADING.md](UPGRADING.md) and know how you will apply schema changes.
- [ ] Health checks respond: `/health`, `/ready` and `/live`.

## What this repository provides

### HTTPS/TLS with Nginx

- **Location**: `nginx/nginx.conf`, built by `nginx/Dockerfile`, started by the
  `nginx` service in the `production` profile of `docker-compose.yml`.
- **What the configuration contains**: an HTTP to HTTPS redirect, TLS 1.2 and
  1.3, a set of security headers (HSTS, X-Frame-Options and others), WebSocket
  upgrade headers, an upstream block you can extend with more servers, and a
  `/health` location.
- It expects `nginx/ssl/cert.pem` and `nginx/ssl/key.pem` to exist; the directory
  is mounted read-only into the container and is git-ignored.

```bash
# Self-signed certificates, for development and testing only
bash scripts/generate_ssl_cert.sh

# For production, use certificates from your CA, for example Let's Encrypt.
```

Start with HTTPS:

```bash
docker compose --profile production up -d
```

Access via http://localhost (redirects to HTTPS) or https://localhost. This
profile also starts the `postgres` service.

### PostgreSQL and Alembic

- **Alembic**: `alembic/`, with one baseline migration, `0001_baseline_schema.py`,
  that creates the full schema. It is safe to run on a database the gateway has
  already created; see [UPGRADING.md](UPGRADING.md).
- **Setup script**: `scripts/setup_postgres.sh` creates the database if it does
  not exist and prints the `DATABASE_URL` to use.

Alembic uses the same `DATABASE_URL` (and the same `asyncpg` driver) as the
gateway, so no extra driver is needed.

```bash
export POSTGRES_DB=gateway
export POSTGRES_USER=gateway_user
export POSTGRES_PASSWORD=your-secure-password

bash scripts/setup_postgres.sh

# Update .env
echo "DATABASE_URL=postgresql+asyncpg://gateway_user:your-secure-password@localhost:5432/gateway" >> .env
```

The `postgres` service in `docker-compose.yml` is part of the `production`
profile. Its port is published on `127.0.0.1` only.

### Log rotation

- **Script**: `scripts/rotate_logs.sh`
- **Settings**: retention 30 days (`RETENTION_DAYS`), maximum size 100M (`MAX_SIZE`)

```bash
# Manual rotation
bash scripts/rotate_logs.sh

# Automatic rotation (add to crontab)
0 0 * * * /path/to/agentanywhere-universal-gateway/scripts/rotate_logs.sh
```

The script uses `logrotate` if it is installed and falls back to manual
rotation otherwise.

### Resource limits in Docker

`docker-compose.yml` sets CPU and memory limits and reservations on the
`gateway`, `redis`, `postgres`, `admin-ui` and `nginx` services (for example the
gateway: limit 2 CPU and 2G, reservation 0.5 CPU and 512M). Check them against
your hardware.

### Secret key generation

```bash
# Print a key
python3 scripts/generate_secret_key.py

# Generate a key and write it into .env
python3 scripts/generate_secret_key.py --update-env
```

### All-in-one script

`scripts/setup_production.sh` runs these steps in order: generate a secret key,
generate self-signed certificates if none exist, set up PostgreSQL if
`USE_POSTGRES=true` and `POSTGRES_PASSWORD` are set, check for `logrotate`,
check that `docker-compose.yml` has resource limits, and run `alembic upgrade
head` if PostgreSQL is enabled (it stops if the migration fails). It does not set `ADMIN_TOKEN`, `CORS_ORIGINS` or
any password other than `POSTGRES_PASSWORD`.

```bash
export POSTGRES_PASSWORD=your-secure-password
export USE_POSTGRES=true  # optional

bash scripts/setup_production.sh
```

## Monitoring

### Health checks

- Gateway: `http://localhost:8000/health` (liveness), `/live`, and `/ready`
  (which checks the database)
- Nginx: `http://localhost/health`
- PostgreSQL and Redis: Docker healthchecks in `docker-compose.yml`

### Metrics

- Gateway: `http://localhost:8000/metrics` (no credential; see above)
- Prometheus: `http://localhost:9090` with `docker compose --profile monitoring up -d`

## Troubleshooting

### SSL certificate issues

```bash
# Regenerate self-signed certificates
rm -rf nginx/ssl/*
bash scripts/generate_ssl_cert.sh
docker compose restart nginx
```

### Database connection issues

```bash
# Check PostgreSQL status
docker compose ps postgres

# Check logs
docker compose logs postgres

# Test connection
docker compose exec postgres psql -U gateway_user -d gateway
```

### Log rotation issues

```bash
# Check log directory permissions
ls -la logs/

# Manual rotation
bash scripts/rotate_logs.sh

# Check disk space
df -h
```

More failure modes are in [QUICKSTART.md](QUICKSTART.md#if-something-goes-wrong).

## Additional resources

- [Nginx SSL Configuration](https://nginx.org/en/docs/http/configuring_https_servers.html)
- [Alembic Documentation](https://alembic.sqlalchemy.org/)
- [Docker Resource Limits](https://docs.docker.com/config/containers/resource_constraints/)
- [Let's Encrypt](https://letsencrypt.org/)
