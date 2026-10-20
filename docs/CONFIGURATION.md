# Configuration Guide

> Reference material. It is kept in step with the code by the automated tests and by
> review, not by a line-by-line manual check. If this page and the gateway disagree,
> the gateway is right; please open an issue.

This guide explains how to configure the AgentAnywhere Universal Gateway using environment variables.

## Quick Start

1. **Copy the example configuration:**
   ```bash
   cp env.example .env
   ```

2. **Or use the setup script:**
   ```bash
   bash scripts/setup_env.sh
   ```

3. **Generate a secure secret key:**
   ```bash
   python3 scripts/generate_secret_key.py --update-env
   ```

4. **Edit `.env` file with your settings:**
   ```bash
   $EDITOR .env
   ```

## Configuration Sections

### Application Settings
- `APP_NAME`: Application name (default: "AgentAnywhere Universal Gateway")
- `APP_VERSION`: Application version
- `ENVIRONMENT`: Environment (development, staging, production)
- `DEBUG`: Enable debug mode (true/false)

### Server Configuration
- `HOST`: Server host (default: "0.0.0.0")
- `PORT`: Server port (default: 8000)
- `WORKERS`: Number of worker processes (default: 4)

### Security
- `SECRET_KEY`: Secret key for signing tokens. It has three jobs: it signs
  tokens, it is accepted as the admin credential when `ADMIN_TOKEN` is empty,
  and it is accepted as an API key. Set it to a random value.
  - Generate with: `python3 scripts/generate_secret_key.py --update-env`
  - No length is enforced. The only rule in the code is that a value starting
    with `change-me` (the placeholder shipped in `env.example`) is treated as
    unset: the admin API will not accept it as a credential, and with
    `ENVIRONMENT=production` the gateway prints a warning to stderr at startup
    but still starts. `scripts/generate_secret_key.py` produces a long random value.
- `ADMIN_TOKEN`: The credential for the admin API (`Authorization: Bearer ...`)
  and the admin console password. Empty by default. If it is empty, a
  `SECRET_KEY` that is not the placeholder is used instead. If both are
  missing or placeholder, the admin API answers 401 to everything. There is no
  default password. Set a separate `ADMIN_TOKEN` rather than relying on
  `SECRET_KEY`.
- `ADMIN_USERNAME`: The user name the admin console's sign-in form expects
  (default: "admin"). The password is `ADMIN_TOKEN` (or `SECRET_KEY`, as above).
- `ALGORITHM`: JWT algorithm (default: "HS256")
- `ACCESS_TOKEN_EXPIRE_MINUTES`: Token expiration time (default: 30)
- `API_KEY_HEADER`: Header name for API keys (default: "X-API-Key")

### CORS Configuration
- `CORS_ORIGINS`: List of allowed origins, written as a JSON array
  - Example: `CORS_ORIGINS=["http://localhost:3000","https://example.com"]`
  - **The default allows every origin.** The built-in default is
    `["*", "http://localhost:3001", "http://localhost:3000"]` and `env.example`
    ships `["*"]`, together with `CORS_ALLOW_CREDENTIALS=true`. Replace it with
    the exact origins of your front ends before you expose the gateway.
- `CORS_ALLOW_CREDENTIALS`: Allow credentials (true/false; default: true)
- `CORS_ALLOW_METHODS`: Allowed HTTP methods, as a JSON array (default: `["*"]`)
- `CORS_ALLOW_HEADERS`: Allowed headers, as a JSON array (default: `["*"]`)

### Rate Limiting
- `RATE_LIMIT_ENABLED`: Enable rate limiting (true/false)
- `RATE_LIMIT_PER_MINUTE`: Requests per minute (default: 60)
- `RATE_LIMIT_PER_HOUR`: Requests per hour (default: 1000)

Counters are kept in Redis when `RATE_LIMIT_REDIS_URL` is set, and in the
memory of each gateway process when it is not. With in-memory counters, every
worker process (see `WORKERS`) and every replica counts separately, so the
effective limit is higher than configured. Use Redis if you run more than one
process.

### Redis Configuration
- `REDIS_URL`: Redis connection URL (default: "redis://localhost:6379/0")
- `REDIS_ENABLED`: Enable Redis (true/false; default: true)
- `RATE_LIMIT_REDIS_URL`: Redis URL for rate limiting (default: unset). When it
  is unset, rate limiting uses in-memory counters. Note that `env.example` sets
  it to `redis://localhost:6379/1`, so a `.env` copied from `env.example` has it
  set; remove or comment out that line to use in-memory counters.

### Database Configuration
- `DATABASE_URL`: Database connection string
  - SQLite: `sqlite+aiosqlite:///./gateway.db`
  - PostgreSQL: `postgresql+asyncpg://user:password@host:5432/database`
- `POSTGRES_DB`: PostgreSQL database name
- `POSTGRES_USER`: PostgreSQL username
- `POSTGRES_PASSWORD`: PostgreSQL password
- `POSTGRES_PORT`: PostgreSQL port (default: 5432)

### Backend Service URLs

Each protocol routes to its matching backend URL. Backends left unset are simply
not routed (requests to that prefix return a clear "backend not configured" error).

- `MCP_BACKEND_URL`: MCP backend URL (default: "http://localhost:8001")
- `A2A_BACKEND_URL`: A2A backend URL (default: "http://localhost:8002")
- `AG_UI_BACKEND_URL`: AG-UI (Agent-GUI streaming) backend URL (optional; enables `/agui` routing)
- `ACP_BACKEND_URL`: ACP (Agent Communication Protocol) backend URL (optional; enables `/acp` routing)
- `HTTP_BACKEND_URL`: HTTP/REST backend URL (optional)
- `MQTT_BROKER_URL`: MQTT broker URL (e.g., "mqtt://localhost:1883")
- `WEBSOCKET_BACKEND_URL`: WebSocket backend URL (e.g., "ws://localhost:8080")
- `GRPC_BACKEND_URL`: gRPC backend URL (e.g., "localhost:50051")
- `GRAPHQL_BACKEND_URL`: GraphQL backend URL (e.g., "http://localhost:4000/graphql")
- `DEFAULT_TIMEOUT`: Default request timeout in seconds (default: 30)

#### Tier-2 streaming (AG-UI / ACP)

AG-UI and ACP surfaces are streamed (Server-Sent Events / chunked) rather than
buffered. The gateway detects streaming when the client sends
`Accept: text/event-stream` (AG-UI, or ACP `POST .../runs`) or `?stream=1`.

- `AG_UI_BOUNDARY_ENABLED` / `ACP_BOUNDARY_ENABLED`: enable payload boundary validation (default: true)
- `PROTOCOL_AG_UI_RATE_LIMIT_PER_MINUTE` / `_PER_HOUR`: AG-UI rate limits (default: 120 / 6000)
- `PROTOCOL_ACP_RATE_LIMIT_PER_MINUTE` / `_PER_HOUR`: ACP rate limits (default: 120 / 6000)

### Logging
- `LOG_LEVEL`: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- `LOG_FORMAT`: Log format ("json" or "text")
- `LOG_EXPORT_ENABLED`: Enable log export to external systems (true/false)

### Log Export - Elasticsearch
- `ELASTICSEARCH_URL`: Elasticsearch URL (default: unset; `env.example` sets `http://localhost:9200`)
- `ELASTICSEARCH_INDEX`: Index name (default: "gateway-logs")
- `ELASTICSEARCH_ENABLED`: Enable Elasticsearch export (true/false)

### Log Export - Splunk
- `SPLUNK_URL`: Splunk HEC URL (e.g., "https://splunk.example.com:8088")
- `SPLUNK_TOKEN`: Splunk HEC token
- `SPLUNK_INDEX`: Splunk index name (default: "gateway")
- `SPLUNK_ENABLED`: Enable Splunk export (true/false)

### Email Notifications
- `SMTP_HOST`: SMTP server host (e.g., "smtp.gmail.com")
- `SMTP_PORT`: SMTP port (default: 587)
- `SMTP_USER`: SMTP username
- `SMTP_PASSWORD`: SMTP password (use app password for Gmail)
- `SMTP_USE_TLS`: Use TLS (true/false)
- `SMTP_FROM_EMAIL`: From email address
- `EMAIL_NOTIFICATIONS_ENABLED`: Enable email notifications (true/false)
- `EMAIL_ALERT_RECIPIENTS`: Comma-separated list of email addresses

### Metrics & Monitoring
- `METRICS_ENABLED`: Enable Prometheus metrics (true/false)
- `METRICS_PATH`: Metrics endpoint path (default: "/metrics")

### Health Check
- `HEALTH_CHECK_PATH`: Health check endpoint path (default: "/health")

### Circuit Breaker
- `CIRCUIT_BREAKER_ENABLED`: Enable circuit breaker (true/false)
- `CIRCUIT_BREAKER_FAILURE_THRESHOLD`: Failure threshold (default: 5)
- `CIRCUIT_BREAKER_TIMEOUT`: Timeout in seconds (default: 60)

## Docker Configuration

When using Docker Compose, the `.env` file is automatically loaded. The `docker-compose.yml` file uses:
- `env_file: .env` - Loads all variables from `.env`
- Environment variable overrides for Docker-specific networking

## Environment-Specific Configuration

### Development
```bash
ENVIRONMENT=development
DEBUG=true
LOG_LEVEL=DEBUG
CORS_ORIGINS=["*"]
```

### Production
```bash
ENVIRONMENT=production
DEBUG=false
LOG_LEVEL=INFO
CORS_ORIGINS=["https://yourdomain.com"]
SECRET_KEY=<generate-strong-key>
ADMIN_TOKEN=<generate-a-second-strong-value>
```

## Security Best Practices

1. **Never commit `.env` file** - It's in `.gitignore`
2. **Use strong secret keys** - Generate with the provided script
3. **Restrict CORS origins** - The default and `env.example` allow every origin; set `CORS_ORIGINS` to your real origins before production
4. **Use environment-specific values** - Different values for dev/staging/prod
5. **Rotate secrets regularly** - Especially `SECRET_KEY` and database passwords

## Validation

The configuration is validated using Pydantic Settings:
- Type checking
- Required field validation
- Default value assignment
- Environment variable parsing

## Troubleshooting

### Configuration not loading
- Ensure `.env` file exists in project root
- Check file permissions
- Verify variable names match exactly (case-insensitive)

### Docker environment variables
- Docker Compose automatically loads `.env`
- Use `${VARIABLE:-default}` syntax for defaults
- Override in `docker-compose.yml` for Docker-specific values

### List of all configuration options
See `env.example` for the complete list of available configuration options.

### Legacy `mcp-gateway` names

The project was first called MCP Gateway. Some names still carry that spelling
(`MCP_GATEWAY_PLUGIN_PATH`, `MCP_GATEWAY_PLUGIN_DISABLED`, `MCP_GATEWAY_URL`,
`MCP_GATEWAY_TOKEN`, the `mcp_gateway.plugins` entry-point group, the
`mcp-gateway` command and `scripts/mcp-gateway`, and example log paths and
service names in `env.example`). They are compatibility aliases for existing
installations and are kept on purpose; the code reads them under those exact
names, so do not rename them.



## Optional features that are off by default

Both features below are off unless you set the variables shown. They were
checked before release only as far as each paragraph says.

### OpenID Connect sign-in for the admin console

Set `IDA_ISSUER` to the issuer URL of an OpenID Connect provider that signs
access tokens with RS256 (Keycloak is the provider this was written against).
`GET /admin/auth/config` then reports `"enabled": true`, the console shows a
single sign-on button, and the admin API accepts access tokens from that
issuer in addition to the local admin credential.

| Variable | Meaning |
|---|---|
| `IDA_ISSUER` | Issuer URL, for example `https://sso.example.com/realms/demo` |
| `IDA_BROWSER_ISSUER` | Issuer URL as the browser sees it, if different |
| `IDA_JWKS_BASE` | Where the gateway fetches signing keys, if different from the issuer |
| `IDA_ADMIN_AUDIENCE` | Client id the token must be issued for. Default `gateway-admin` |
| `IDA_ADMIN_ROLES` | Comma-separated realm roles, one of which the user must hold. Default `platform_admin,tenant_admin` |

What was checked: with `IDA_ISSUER` set, the config endpoint reports the
issuer, a forged token is rejected with 401, and the local admin credential
still works. A full sign-in against a live provider was not run for this
release; the token validation is covered by `tests/test_ida_auth.py`.

### Agent edge: `/agents/<slug>/...`

A path-prefix reverse proxy. With `AGENT_EDGE_ENABLED=true` and a slug map, a
request to `/agents/orders-agent/health` is forwarded to `/health` on that
slug's upstream, for HTTP and WebSocket.

```bash
AGENT_EDGE_ENABLED=true
AGENT_ROUTES_JSON={"orders-agent":"http://10.0.0.10:8090"}
```

**The edge does not apply gateway API keys, rate limits or quotas.** It
forwards the caller's `Authorization` header and cookies and leaves
authentication to the upstream. Only route a slug to a service that
authenticates its own callers.

What was checked: off by default; with the two variables set, a request under
a mapped slug reaches the upstream and an unknown slug answers 404.
