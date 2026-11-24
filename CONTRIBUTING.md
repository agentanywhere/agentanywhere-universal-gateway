# Contributing to AgentAnywhere Universal Gateway

Thank you for helping. This guide covers how to propose a change, how to set
up a development environment, where things live in the code, and how to make
the usual kinds of change.

By taking part you agree to the [code of conduct](CODE_OF_CONDUCT.md). Report
security problems privately, as described in [SECURITY.md](SECURITY.md), never
in a public issue.

## Contents

- [Proposing a change](#proposing-a-change)
- [Development setup](#development-setup)
- [A tour of the codebase](#a-tour-of-the-codebase)
- [How a request flows through the code](#how-a-request-flows-through-the-code)
- [Common changes](#common-changes)
- [Tests](#tests)
- [Code style](#code-style)
- [Documentation](#documentation)

## Proposing a change

1. For anything larger than a small fix, open an issue first (there are
   templates for bugs and feature requests in `.github/ISSUE_TEMPLATE/`).
2. Fork the repository and create a branch:

   ```bash
   git checkout -b fix/short-description
   ```

3. Make the change, with tests and documentation (see below).
4. Commit with a conventional prefix: `feat:`, `fix:`, `docs:`, `test:`,
   `refactor:` or `chore:`.
5. Push the branch and open a pull request. The template in
   `.github/PULL_REQUEST_TEMPLATE.md` asks what changed and how you tested it.
   A maintainer reviews it; CI must pass.

## Development setup

You need Python 3.10, 3.11 or 3.12. Docker is only needed for the Compose
stack, Node.js 22 only for the admin console, Redis and PostgreSQL only if you
work on those paths.

On Windows, see the note in the [README quick start](README.md#quick-start): use
`python`, `py -3.12 -m venv .venv` and `.venv/Scripts/activate`.

```bash
git clone https://github.com/agentanywhere/agentanywhere-universal-gateway.git
cd agentanywhere-universal-gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
python3 scripts/generate_secret_key.py --update-env
uvicorn app.main:app --port 8000 --reload
```

`make dev` runs the same `uvicorn` command on `0.0.0.0`. The
[getting-started tutorial](docs/tutorials/getting-started.md) shows how to
create a key and send a request.

For the console, in another terminal: `cd admin-ui && npm ci && npm run dev`
(port 3001, proxying `/admin` and `/api` to `127.0.0.1:8000`).

## A tour of the codebase

| Concern | Where |
|---|---|
| Application, middlewares, data-plane routes, start-up and shutdown | `app/main.py` |
| Settings (every environment variable) | `app/config.py` |
| API-key, OAuth2 and governance checks | `app/middleware/auth.py` |
| Per-key rate limiting | `app/middleware/rate_limit.py` |
| Choosing a backend and forwarding; circuit breakers | `app/routing.py` |
| Routes registered at run time | `app/route_registry.py`, `app/repositories/route_repo.py` |
| Protocol handlers that are not plain HTTP (GraphQL, MQTT, WebSocket, gRPC) | `app/protocols/` |
| MCP and A2A payload checks, OpenAPI registrations, Agent Card | `app/tier1_mcp.py`, `app/tier1_a2a.py`, `app/tier1_openapi.py`, `app/tier1_registry.py` |
| AG-UI and ACP checks, streaming detection | `app/tier2_ag_ui.py`, `app/tier2_acp.py`, `app/tier2_streaming.py` |
| Database engine and sessions | `app/database.py` |
| Database tables | `app/db_models.py` |
| Request and response models | `app/models.py` |
| Database access | `app/repositories/` |
| Tenants and quotas | `app/tenant.py`, `app/repositories/tenant_repo.py` |
| Policies, IP rules, roles, audit log | `app/governance.py` |
| Errors and their JSON envelope | `app/exceptions.py`, the handlers in `app/main.py` |
| Metrics | `app/metrics.py`, `app/observability.py` |
| Logging, redaction, trace context | `app/logging_config.py` |
| Log export, email and Slack, webhooks | `app/log_exporters.py`, `app/notifications.py`, `app/webhooks.py` |
| OAuth2 endpoints and OpenID Connect admin sign-in | `app/oauth2.py`, `app/ida_auth.py` |
| Agent edge | `app/agent_routes.py`, `app/agent_registry.py`, `app/agent_monitor.py`, `admin/agents_api.py` |
| Transformations and API versions | `app/transformers.py`, `app/versioning.py` |
| Plugins | `app/plugins/` (`base.py` is the interface, `registry.py` dispatch, `loader.py` discovery, `builtin/`) |
| Analytics (experimental) | `app/ai/`, `app/ai_service.py` |
| SDK generators | `app/sdk/` |
| Command-line tool | `app/cli/` |
| Admin API | `admin/api.py` |
| Admin console (React, Vite) | `admin-ui/` |
| Migrations (out of date) | `alembic/` |
| Sample backends | `samples/backends/` |
| Client examples and integration sketches | `examples/`, `cookbooks/` |
| Utility and documentation scripts | `scripts/` |
| TLS reverse proxy | `nginx/` |
| Tests | `tests/` |
| Documentation | `docs/` (map in `docs/README.md`) |

## How a request flows through the code

1. `logging_middleware` (`app/main.py`) is the outermost middleware. It
   assigns the request id and records metrics and logs after the response,
   including for rejected requests.
2. `auth_middleware_handler` lets public paths and `/admin` through;
   otherwise it calls `AuthMiddleware.authenticate` (key, `allowed_paths`, IP
   rules, policies), `RateLimiter.check_rate_limit` and
   `tenant_service.check_quota`, and answers 401, 403 or 429 itself through
   `_auth_reject`.
3. FastAPI routes the request: `/admin/*` to `admin/api.py`, `/graphql` and
   `/mqtt/*` to their handlers, everything else to `gateway_proxy`.
4. `gateway_proxy` runs the protocol checks, protocol plugins, the dynamic
   route lookup and request plugins, then `Router.route_request` (or
   `start_upstream_stream` for streams), then response transformations and
   plugins.

[Architecture](docs/explanation/architecture.md) has the diagram.

## Common changes

### Add a setting

1. Add a typed field to `Settings` in `app/config.py` (and to a section in
   `SECTIONS` so the admin API groups it; to `SAFE_RELOAD_FIELDS` only if it
   is safe to change on a running process; to `SENSITIVE_FIELDS` if it is a
   secret).
2. Add it to `env.example` if users should see it.
3. Add a row to `docs/reference/configuration.md`, between the
   `env-vars` markers. `python scripts/check_env_vars.py` fails until you do.

### Add an admin endpoint

1. Add the function to `admin/api.py` (or `admin/agents_api.py`) with the
   router decorator and the admin dependency:

   ```python
   @router.get("/widgets")
   async def list_widgets(
       admin_token: str = Depends(verify_admin_token),
       db: AsyncSession = Depends(get_db),
   ):
       """List widgets."""  # the first paragraph becomes the summary in the docs
       ...
   ```

   The router's prefix is `/admin`, so this serves `GET /admin/widgets`.
   Without `Depends(verify_admin_token)` the endpoint is public: the
   authentication middleware does not check `/admin` paths.
2. If it stores data, add a model to `app/db_models.py` and a repository in
   `app/repositories/`. The table is created on start-up; existing databases
   need SQL for column changes (see [Upgrade the gateway](docs/how-to/upgrade.md)).
3. Add tests using the `client` fixture, authenticating with
   `{"Authorization": f"Bearer {settings.secret_key}"}`.
4. Regenerate the endpoint list:

   ```bash
   python scripts/generate_http_reference.py
   ```

### Add a middleware

Add a function with `@app.middleware("http")` in `app/main.py`, or a Starlette
middleware class with `app.add_middleware(...)`. Starlette runs the
middleware registered **last** first. Today the order is: logging
(outermost, registered last with `app.middleware("http")(logging_middleware)`),
authentication, CORS. A middleware registered between the two runs after
logging and before authentication, and sees unauthenticated requests. A
response produced by the authentication layer should go through
`_auth_reject`, so that logging records it as a rejected request. WebSocket
routes do not pass through HTTP middlewares. Keep reusable logic in a class
under `app/middleware/` and export it from `app/middleware/__init__.py`, as
`AuthMiddleware` and `RateLimiter` are.

For changes that do not need to be in the core, a plugin is often enough
([Write a plugin](docs/how-to/write-a-plugin.md)).

### Add a protocol

For an HTTP-based protocol:

1. Add a value to `ProtocolType` in `app/models.py`.
2. Add a backend URL setting (see [Add a setting](#add-a-setting)).
3. Add a prefix branch to `Router._get_backend_url` in `app/routing.py`. The
   branches are checked in order, and `/api` is a catch-all for anything under
   it, so put a more specific prefix (like `/api/yourproto`) before it. If the
   backend expects paths without the prefix, extend
   `Router._apply_tier2_path_strip`.
4. If the payloads should be validated, write a `validate_*_payload` function
   like the ones in `app/tier2_acp.py` and call it from `gateway_proxy`.
5. If it should stream, extend `should_proxy_streaming_http` in
   `app/tier2_streaming.py`.
6. Add the prefix to the protocol detection in `logging_middleware` so the
   `protocol` metric label is right.

For a protocol that is not plain HTTP, also add a handler class in
`app/protocols/` (see `graphql_handler.py` or `mqtt_handler.py`), create it in
`app/main.py` from its setting, add a dedicated route **before** the
catch-all `/{path:path}` route, and close it in the lifespan shutdown.

Then add a sample backend under `samples/backends/<protocol>/`, tests in
`tests/test_protocols.py` or a new file, a row in
[protocol support](docs/explanation/protocol-support.md), and regenerate the
endpoint list. A `ProtocolPlugin` is the lighter alternative when request and
response JSON is enough.

## Tests

```bash
pytest                                  # everything, with coverage
pytest tests/test_routing.py -q         # one file
pytest -o addopts="" tests/test_docs_quickstart.py   # without coverage
```

- One file per area under `tests/`, `test_<area>.py`.
- `tests/conftest.py` sets a test `SECRET_KEY` before the app is imported and
  provides `db_session` (in-memory SQLite), `client` (a `TestClient` wired to
  that session), `sample_api_key` and `sample_route`.
- `tests/test_container_integration.py` runs only against a live gateway at
  `GATEWAY_URL`; it is skipped otherwise.
- `tests/test_docs_quickstart.py` replays the documented quick start.

Details: [Run the tests](docs/how-to/run-the-tests.md).

Add a test for every bug you fix and every behaviour you add. Tests must not
need network access, a running server, Redis or PostgreSQL; use the fixtures
and mock external HTTP calls.

## Code style

CI runs:

```bash
python -m compileall -q app admin
ruff check app admin tests --select F821,F823
```

`make lint` runs the same (install `ruff` first). `make format` runs `black`
and `isort`, which are not in `requirements.txt` and are not checked by CI.

Follow PEP 8, use type hints, keep functions small, and give public functions
a docstring whose first paragraph says what they do (the endpoint list uses it).

## Documentation

- Update the docs in the same pull request as the code. The
  [documentation map](docs/README.md) shows where each kind of page lives.
- State only what the code does; mark anything you could not run.
- Run the checks before you push:

  ```bash
  python scripts/check_docs.py             # links, anchors, Mermaid, heading levels
  python scripts/check_env_vars.py         # configuration reference matches the code
  python scripts/generate_http_reference.py --check   # endpoint list is current
  ```

- Preview the documentation site (Material for MkDocs) and check that it builds
  without warnings:

  ```bash
  pip install -r requirements-docs.txt
  mkdocs serve              # http://127.0.0.1:8000, rebuilds as you edit
  mkdocs build --strict     # what CI runs
  ```

  Page descriptions, structured data, `llms.txt` and the links to files outside
  `docs/` are generated by `hooks/docs_site.py`; keep the first paragraph of a
  page a plain sentence that says what the page is for.
- Do not edit `docs/reference/http-endpoints.md` by hand; change the code or
  `scripts/generate_http_reference.py` and run it.
- Use British spelling, short sentences and the canonical names in the
  [CLI reference](docs/reference/cli.md#names-and-aliases).
