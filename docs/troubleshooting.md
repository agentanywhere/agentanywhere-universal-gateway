# Troubleshooting

Find what you see in the first column. For the three most common request
errors there is a step-by-step guide:
[Troubleshoot 401, 502 and 429](how-to/troubleshoot-401-502-429.md).

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
| `error parsing value for field "cors_origins"` (or `plugins_disabled`, `plugin_paths`) at start-up | A list setting written as comma-separated text | Write it as a JSON array: `CORS_ORIGINS=["https://app.example.com"]`. |
| `URL must include a scheme` at start-up | A backend URL without `http://` | Add the scheme. |
| `Address already in use` | Port 8000 is taken | Stop the other program, or `uvicorn app.main:app --port 8010`. |
| `[config] WARNING: SECRET_KEY is set to its default value in production` | `SECRET_KEY` is the placeholder | `python3 scripts/generate_secret_key.py --update-env`, then restart. |
| `mqtt_connection_failed` in the log | `MQTT_BROKER_URL` is set but no broker answers | Harmless if you do not use MQTT; otherwise start the broker, then restart the gateway. |
| Changes to `.env` have no effect | Settings are read at start-up, from the `.env` in the current directory | Restart from the repository root. Env-only variables (`OTEL_*`, `MCP_GATEWAY_PLUGIN_*`, `LOG_EXPORT_BATCH_SIZE` and the like) must be exported, not written in `.env`. |
| Logs show `plugin_bootstrap_failed`, `governance_load_failed` or `route_registry_init_failed` | A start-up step failed; the gateway carries on without it | Read the `error` field of that log line. |

## Admin API and console

| What you see | Why | What to do |
|---|---|---|
| Every admin call answers 401 | No usable admin credential: `ADMIN_TOKEN` empty and `SECRET_KEY` a placeholder, or a different value from the one the gateway started with | Set `ADMIN_TOKEN`, restart, send `Authorization: Bearer <ADMIN_TOKEN>`. |
| Console sign-in fails | Wrong user name or password | User `admin` (or `ADMIN_USERNAME`), password `ADMIN_TOKEN` (or `SECRET_KEY` if `ADMIN_TOKEN` is empty). |
| Console shows network errors | The console is not behind a proxy to the gateway | Use the Compose service, or `npm run dev` in `admin-ui/` with the gateway on `localhost:8000`. |
| Console's delete or disable button for a key answers 404 | The console sends the key instead of its id | Use `curl` with the id ([Create, rotate and revoke API keys](how-to/manage-api-keys.md)). |
| `GET /admin/api-keys` has no ids | The endpoint does not return them | Read ids from the `api_keys` table. |
| 422 `VALIDATION_ERROR` creating a key or webhook | Parameters sent as a JSON body | Put them in the query string. |
| `/admin/ai/*` returns nothing useful | Those endpoints read an analytics instance that never sees traffic | See [known limitations](known-limitations.md#the-adminai-analytics-endpoints-are-experimental). |
| Webhooks, transformations or API versions disappeared | They are kept in memory | Re-create them after each start. |

## Requests through the gateway

| What you see | Why | What to do |
|---|---|---|
| 401 `API key required` | No key sent | Send `X-API-Key`. |
| 401 `Invalid or inactive API key` | Unknown, disabled or expired key, or a different database | Check the key list; see the 401 guide. |
| A revoked key still works | Redis key cache (five minutes) | Delete `api_key:<key>` from Redis database 0. |
| 403 `AUTHORIZATION_ERROR` | An IP rule or policy denied it | Check `/admin/governance/ip-rules` and `/policies`. |
| 400 `VALIDATION_ERROR` on `/mcp/...` | The MCP boundary rejected the body (size, JSON, method, prompt-injection pattern) | Read `details`; adjust `MCP_BOUNDARY_*` if the block is wrong for you. |
| 429 `RATE_LIMIT_EXCEEDED` | Key limit or tenant quota | See the 429 guide. |
| 500 `INTERNAL_ERROR` on `/agui`, `/acp`, `/ws` or `/mqtt/...` | That backend URL is not set | Set it and restart. |
| 502 `BACKEND_ERROR` | Backend unreachable or too slow | See the 502 guide. Under Docker, check for `localhost` URLs. |
| 503 `circuit open` | Five failures in a row opened the breaker | Fix the backend; wait `CIRCUIT_BREAKER_TIMEOUT` seconds or restart. |
| 404 from a path you expected to work | The backend does not serve that path (the gateway forwards it as is, except `/agui` and `/acp`, which are stripped) | Check the backend's routes; use a dynamic route with `rewrite_path`. |
| `/docs` returns your backend's page or a 502 | `/docs` is only served with `DEBUG=true`; otherwise it falls through to the default backend | Use `DEBUG=true` locally, or read `/openapi.json`. |
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
| Every proxied request answers 502 | `.env` from `env.example` uses `localhost` backend URLs | Use service names, or delete those lines ([Deploy with Docker Compose](how-to/deploy-with-docker-compose.md#the-three-traps)). |
| API keys gone after `docker compose up --build` or `down` | SQLite file inside the recreated container | Use PostgreSQL; back up with `docker compose cp`. |
| `port is already allocated` | A host port is in use | Stop the other service or change the mapping; `PORT` changes the gateway's. |
| `/ready` answers 503 with PostgreSQL | The gateway started before the database | Wait for the restart, or `docker compose restart gateway`. |
| nginx keeps restarting | No certificate in `nginx/ssl/` | `bash scripts/generate_ssl_cert.sh` or copy yours. |

## Data

| What you see | Why | What to do |
|---|---|---|
| `table api_keys already exists` from Alembic | The gateway already created the tables | Do not use Alembic; see [Upgrade the gateway](how-to/upgrade.md). |
| `table api_keys has no column named key` | The database was built with the Alembic migrations | Start from an empty database and let the gateway create it, then copy data across. |
| `database is locked` | Several processes writing one SQLite file | Use PostgreSQL. |
| The database keeps growing | `request_logs` is never pruned | Delete old rows on a schedule. |

## Metrics and logs

| What you see | Why | What to do |
|---|---|---|
| No 401 or 429 in metrics or `/admin/logs` | Rejected requests are answered before they are recorded | Count them at your proxy. |
| `gateway_rate_limit_hits_total` stays empty | Never updated | Use proxy counts. |
| Metrics jump around with several workers | Each worker has its own counters | One process per container. |
| `/metrics` answers 404 | `METRICS_ENABLED=false` or another `METRICS_PATH` | Check the settings. |
| Rejected requests have `"request_id": null` and no `X-Request-ID` | They are answered before a request id is assigned | Match by time and client instead. |

Still stuck? See [Support](support.md).
