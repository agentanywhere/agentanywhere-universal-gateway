# Troubleshooting

Find what you see in the first column. For the three most common request
errors there is a step-by-step guide:
[Troubleshoot 401, 403, 404, 502 and 429](how-to/troubleshoot-401-502-429.md).

## Contents

- [Installing and starting](#installing-and-starting)
- [Admin API and console](#admin-api-and-console)
- [Requests through the gateway](#requests-through-the-gateway)
- [Protocols](#protocols)
- [Docker](#docker)
- [Data](#data)
- [Metrics and logs](#metrics-and-logs)

## Installing and starting

| What you see | Why | What to do |
|---|---|---|
| `pip install -r requirements.txt` fails building a package | Python 3.13, or an old pip | Use Python 3.10, 3.11 or 3.12 and `pip install --upgrade pip`. |
| `error parsing value for field ...` for a list setting at start-up | A value that starts with `[` but is not valid JSON | Fix the JSON, or write the list comma-separated: `CORS_ORIGINS=https://a.example,https://b.example`. |
| `URL must include a scheme` at start-up | A backend URL without `http://` | Add the scheme. |
| `Address already in use` | Port 8000 is taken | Stop the other program, or `uvicorn app.main:app --port 8010`. |
| `[config] WARNING: SECRET_KEY is set to its default value in production` | `SECRET_KEY` is the placeholder | `python3 scripts/generate_secret_key.py --update-env`, then restart. |
| `mqtt_connection_failed` in the log | `MQTT_BROKER_URL` is set but no broker answers | Harmless if you do not use MQTT; otherwise start the broker, then restart the gateway. |
| Changes to `.env` have no effect | Settings are read at start-up, from the `.env` in the current directory | Restart from the repository root. `CONFIG_FILE` and the `LOG_EXPORT_*` batch settings must be exported, not written in `.env`. |
| Logs show `plugin_bootstrap_failed`, `governance_load_failed` or `route_registry_init_failed` | A start-up step failed; the gateway carries on without it | Read the `error` field of that log line. |

## Admin API and console

| What you see | Why | What to do |
|---|---|---|
| Every admin call answers 401 | No usable admin credential: `ADMIN_TOKEN` empty and `SECRET_KEY` a placeholder, or a different value from the one the gateway started with | Set `ADMIN_TOKEN`, restart, send `Authorization: Bearer <ADMIN_TOKEN>`. |
| `SECRET_KEY` stopped working on the admin API | `ADMIN_TOKEN` is set, so `SECRET_KEY` is refused there | Use `ADMIN_TOKEN`, or `ADMIN_ACCEPT_SECRET_KEY=true`. |
| Console sign-in fails | Wrong user name or password | User `admin` (or `ADMIN_USERNAME`), password `ADMIN_TOKEN` (or `SECRET_KEY` if `ADMIN_TOKEN` is empty). |
| Console shows network errors | The console is not behind a proxy to the gateway | Use the Compose service, or `npm run dev` in `admin-ui/` with the gateway on `localhost:8000`. |
| You lost a key you created | Keys are stored as hashes and shown only once | Create a new key and revoke the old one. |
| 422 creating a key or webhook | A required field is missing (`name` for keys; `url` and an event for webhooks) or has the wrong type | Read `details.errors`. |
| `/admin/ai/*` returns nothing useful | Those endpoints read an analytics instance that never sees traffic | See [known limitations](known-limitations.md#the-adminai-analytics-endpoints-are-experimental). |
| Webhooks, transformations or API versions disappeared | They are kept in memory | Re-create them after each start. |

## Requests through the gateway

| What you see | Why | What to do |
|---|---|---|
| 401 `API key required` | No key sent | Send `X-API-Key`. |
| 401 `Invalid or inactive API key` | Unknown, disabled or expired key, or a different database | Check the key list; see the troubleshooting guide. |
| 403 `This API key is not allowed to access this path` | The key's `allowed_paths` does not cover the path | Use a key with a wider scope. |
| 403 `AUTHORIZATION_ERROR` | An IP rule or policy denied it | Check `/admin/governance/ip-rules` and `/policies`. |
| 400 `VALIDATION_ERROR` on `/mcp/...` | The MCP boundary rejected the body (size, JSON, method, prompt-injection pattern) | Read `details`; adjust `MCP_BOUNDARY_*` if the block is wrong for you. |
| 429 `RATE_LIMIT_EXCEEDED` | Key limit or tenant quota | See the troubleshooting guide. |
| 500 `INTERNAL_ERROR` on `/agui`, `/acp`, `/ws` or `/mqtt/...` | That backend URL is not set | Set it and restart. |
| 502 `BACKEND_ERROR` | Backend unreachable or too slow | See the troubleshooting guide. Under Docker, check for `localhost` URLs. |
| 503 `circuit open` | Five failures in a row opened the breaker | Fix the backend; wait `CIRCUIT_BREAKER_TIMEOUT` seconds or restart. |
| 404 from a path you expected to work | The backend does not serve that path (the gateway forwards it as is, except `/agui` and `/acp`, which are stripped) | Check the backend's routes; use a dynamic route with `rewrite_path`. |
| `/docs`, `/redoc` or `/openapi.json` answer 404 | They are served only with `DEBUG=true` | Set `DEBUG=true` locally. |
| Large responses are slow or time out at 30 seconds | Buffered responses and `DEFAULT_TIMEOUT` | Raise `DEFAULT_TIMEOUT`; stream AG-UI and ACP with `Accept: text/event-stream`. |

## Protocols

| What you see | Why | What to do |
|---|---|---|
| WebSocket closes immediately with 1008 | `/ws/*` does not work as shipped | Connect to the backend directly, or use the agent edge. |
| WebSocket closes with 1003 | `WEBSOCKET_BACKEND_URL` is not set | (It would still not work; see above.) |
| `/grpc` answers 200 with `"status": "unsupported"` | The gRPC bridge does not work | Call the gRPC service directly. |
| 503 `MQTT broker not connected` | The broker was down when the gateway started | Start the broker, restart the gateway. |
| `/mqtt/subscribe` succeeds but no messages arrive | Messages are only logged | Subscribe to the broker directly. |
| GraphQL answers 502 with the sample backend | `env.example` points at port 4000; the sample is on 8006 | `GRAPHQL_BACKEND_URL=http://localhost:8006/graphql`. |

## Docker

| What you see | Why | What to do |
|---|---|---|
| Every proxied request answers 502 under Compose | The `backends` profile is not running, or a `DOCKER_*_BACKEND_URL` points somewhere unreachable | `docker compose --profile backends up -d`, or fix the `DOCKER_` value ([Deploy with Docker Compose](how-to/deploy-with-docker-compose.md)). |
| API keys gone under Compose | `docker compose down -v` or `make clean` deleted the `gateway_data` volume, or `.env` sets a relative SQLite `DATABASE_URL` | Restore a backup; delete the relative `DATABASE_URL` line. |
| `postgres` or `grafana` exits at once | `POSTGRES_PASSWORD` or `GRAFANA_ADMIN_PASSWORD` is empty or a placeholder | Set a generated value in `.env`. |
| `port is already allocated` | A host port is in use | Stop the other service or change the mapping; `PORT` changes the gateway's. |
| `/ready` answers 503 with PostgreSQL | The gateway started before the database | Wait for the restart, or `docker compose restart gateway`. |
| nginx keeps restarting | No certificate in `nginx/ssl/` | `bash scripts/generate_ssl_cert.sh` or copy yours. |
| `docker compose ps` shows nginx unhealthy while it works | Its healthcheck requests `http://localhost/health`, which redirects to HTTPS | Ignore it, or change the healthcheck. |
| `docker compose up --build` fails building `admin-ui` | The console's production build probably fails (`postcss.config.js` is ESM, `package.json` has no `"type": "module"`) | Run the console with `npm run dev`; see [known limitations](known-limitations.md#the-admin-consoles-production-build-probably-fails). |
| `Can't locate revision identified by '002'` from Alembic | The database was built with the old migrations | See [Upgrade the gateway](how-to/upgrade.md#databases-built-with-the-old-migrations). |

## Data

| What you see | Why | What to do |
|---|---|---|
| `table api_keys has no column named key` or similar | The database was built with the old `001`/`002` migrations | Rebuild it as in [Upgrade the gateway](how-to/upgrade.md#databases-built-with-the-old-migrations). |
| `no such column` after an upgrade | A release added a column and `alembic upgrade head` was not run | Run `alembic upgrade head`. |
| `database is locked` | Several processes writing one SQLite file | Use PostgreSQL. |
| The database keeps growing | `request_logs` is never pruned | Delete old rows on a schedule. |

## Metrics and logs

| What you see | Why | What to do |
|---|---|---|
| Logs and metrics show one address for every client | Behind a proxy with `TRUSTED_PROXIES` empty | List the proxy's address in `TRUSTED_PROXIES`. |
| Metrics jump around with several workers | Each worker has its own counters | One process per container. |
| `/metrics` answers 404 | `METRICS_ENABLED=false` or another `METRICS_PATH` | Check the settings. |
| `/metrics` or `/health/detailed` answers 401 | `OPS_ENDPOINTS_REQUIRE_ADMIN=true` | Send the admin credential. |
| `/metrics` answers 403 through nginx | Outside nginx's allow-list | Edit the `allow` lines in `nginx/nginx.conf`. |

Still stuck? See [Support](support.md).
