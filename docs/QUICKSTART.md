# Quick start

This is the same path as the README, with a little more explanation. Every
command here is run by hand before a release.

## 1. Install and start the gateway

You need Python 3.10, 3.11 or 3.12.

```bash
git clone https://github.com/agentanywhere/universal-gateway.git
cd universal-gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
python3 scripts/generate_secret_key.py --update-env
uvicorn app.main:app --port 8000
```

`generate_secret_key.py --update-env` writes a random `SECRET_KEY` into `.env`.
Do not skip it. The value shipped in `env.example` is a placeholder, and the
gateway refuses it: the admin API answers 401 to everything until a real key is
set. There is no default admin password.

Check that it is up:

```bash
curl http://localhost:8000/health
```

The gateway does not need Redis. Without it, rate-limit counters are kept in
memory, which is correct for a single process.

## 2. Start a sample backend

In a second terminal:

```bash
cd universal-gateway
source .venv/bin/activate
python3 samples/backends/http/http_server.py
```

It listens on port 8003, which is where `HTTP_BACKEND_URL` in `env.example`
points.

## 3. Create an API key

In a third terminal. The admin credential is `ADMIN_TOKEN`, or `SECRET_KEY`
when `ADMIN_TOKEN` is empty, which it is after step 1.

```bash
cd universal-gateway
ADMIN=$(grep '^SECRET_KEY=' .env | cut -d= -f2-)
curl -s -X POST "http://localhost:8000/admin/api-keys?name=first-key" -H "Authorization: Bearer $ADMIN"
```

The response contains the new key in its `key` field.

## 4. Make a request through the gateway

```bash
API_KEY=paste-the-key-here
curl -s -X POST http://localhost:8000/api/users -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{"name": "Ada", "email": "ada@example.com"}'
```

You get the created user back. Without the `X-API-Key` header you get 401.

## Where next

- [PROTOCOLS.md](PROTOCOLS.md): a request example and a sample backend for each protocol, and which ones work today
- [CONFIGURATION.md](CONFIGURATION.md): settings
- [API_REFERENCE.md](API_REFERENCE.md): endpoints
- [CLI.md](CLI.md): the command-line tool

## If something goes wrong

**The gateway will not start.**

- **"Address already in use" (uvicorn).** Something else is listening on port
  8000. Use `--port 8010` and change the URLs above to match.
- **`pip install` fails on dependencies.** Python 3.13 is not supported, because
  several pinned dependencies do not build on it. Use 3.10, 3.11 or 3.12.
- **`.env` changes have no effect.** Settings are read at startup. Restart the
  gateway after you edit `.env`.
- **A list setting makes startup fail.** Settings that hold a list, such as
  `CORS_ORIGINS`, must be written as a JSON array, for example
  `CORS_ORIGINS=["https://example.com"]`. A bare comma-separated value is not
  accepted.

**Admin API or console.**

- **The admin API answers 401.** `SECRET_KEY` in `.env` is still the
  placeholder (a value starting with `change-me`) and `ADMIN_TOKEN` is empty, or
  you are sending a different value from the one the gateway started with. Send
  it as `Authorization: Bearer <value>`. Restart the gateway after changing `.env`.
- **The admin console rejects the sign-in.** The user name is `ADMIN_USERNAME`
  (default `admin`) and the password is `ADMIN_TOKEN`, or `SECRET_KEY` if
  `ADMIN_TOKEN` is empty. There is no default password.
- **Two 401s that look alike.** Admin endpoints (`/admin/*`) take the admin
  credential. Everything else takes an API key from `POST /admin/api-keys`, in
  the `X-API-Key` header or as a bearer token. The admin credential is not an
  API key, except that `SECRET_KEY` is also accepted as one when it is not the
  placeholder.

**Requests through the gateway.**

- **401 "API key required" or invalid key.** The `X-API-Key` header is missing,
  or the key does not exist in the database. If you removed `gateway.db`, or are
  running in Docker without a volume for it, the keys are gone; create a new one.
- **502.** The gateway could not reach the backend for that route. Check that
  the backend is running and that the matching `*_BACKEND_URL` points at it.
  Under Docker, `localhost` in a backend URL means the gateway container itself,
  not your machine; `docker-compose.yml` uses the value from `.env` when one is
  set, so replace the `localhost` URLs copied from `env.example` with service
  names such as `http://http-backend:8003` (the sample backends need
  `docker compose --profile backends up -d --build`) or with an address the
  container can reach.
- **503 after several 502s.** The circuit breaker for that backend has opened.
  It closes again after `CIRCUIT_BREAKER_TIMEOUT` seconds.
- **429.** The key or the tenant went over its limit. The response carries a
  `Retry-After` header.

**Redis.**

- **You do not need Redis for this quick start.** Without it, rate-limit
  counters are kept in memory, which is correct for a single process only. If a
  Redis URL is configured but Redis is not reachable, rate limiting also falls
  back to in-memory counters instead of failing. The copy of `env.example`
  sets `REDIS_URL` and `RATE_LIMIT_REDIS_URL` to `localhost`; leaving them is
  harmless without Redis, or comment out `RATE_LIMIT_REDIS_URL` and set
  `REDIS_ENABLED=false`.

**Docker.**

- **"port is already allocated".** Something on the host is using 8000, 3001 or
  6379 (or 80/443/5432 with `--profile production`). Stop it, or change the
  left-hand side of the port mapping in `docker-compose.yml`. The gateway's
  host port can be set with `PORT` in `.env`.

More on running the gateway outside a laptop: [DEPLOYMENT.md](DEPLOYMENT.md).
