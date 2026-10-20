# Tutorial: your first authenticated request through the gateway

In this tutorial you run AgentAnywhere Universal Gateway on your own machine,
start one of the sample backends, create an API key and send a request through
the gateway to the backend. It takes about ten minutes, most of which is
`pip install`.

You will end up with three terminals: one running the gateway, one running the
sample HTTP backend, and one where you type requests.

**Note.** This tutorial runs everything with Python, without Docker. That is on
purpose. With the shipped `env.example`, the Docker Compose quick start sends
requests to `localhost` inside the gateway container and answers 502; the
[Docker Compose guide](../how-to/deploy-with-docker-compose.md) explains the
fix.

## Contents

- [What you need](#what-you-need)
- [1. Install the gateway](#1-install-the-gateway)
- [2. Create the configuration](#2-create-the-configuration)
- [3. Start the gateway](#3-start-the-gateway)
- [4. Start the sample HTTP backend](#4-start-the-sample-http-backend)
- [5. Create an API key](#5-create-an-api-key)
- [6. Send a request through the gateway](#6-send-a-request-through-the-gateway)
- [7. See what happens without a key](#7-see-what-happens-without-a-key)
- [What you did](#what-you-did)
- [How these outputs were produced](#how-these-outputs-were-produced)
- [If something went wrong](#if-something-went-wrong)
- [Next](#next)

## What you need

- Python 3.10, 3.11 or 3.12 (3.13 is not supported)
- `git` and `curl`
- Ports 8000 and 8003 free

You do not need Redis, PostgreSQL or an MQTT broker. Without Redis, the gateway
keeps rate-limit counters in memory, and the default database is a SQLite file
in the directory you start it from.

## 1. Install the gateway

```bash
git clone https://github.com/agentanywhere/agentanywhere-universal-gateway.git
cd agentanywhere-universal-gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Create the configuration

```bash
cp env.example .env
python3 scripts/generate_secret_key.py --update-env
echo "ADMIN_TOKEN=$(python3 scripts/generate_secret_key.py 2>/dev/null)" >> .env
```

The first command copies the example settings. The second replaces the
placeholder `SECRET_KEY` with a random value; the gateway refuses the
placeholder. The third adds a random `ADMIN_TOKEN`, the credential for the
admin API. `.env` already contains an empty `ADMIN_TOKEN=` line; the line you
append comes later in the file, and the later value wins.

There is no default admin password. If you skip these steps, the admin API
answers 401 to everything.

## 3. Start the gateway

```bash
uvicorn app.main:app --port 8000
```

The gateway prints JSON log lines. A line with `"event": "mqtt_connection_failed"`
is expected: `env.example` points `MQTT_BROKER_URL` at a broker you have not
started, and nothing in this tutorial uses it.

Leave it running. In a second terminal, check that it answers:

```bash
curl -s http://localhost:8000/health
```

```json
{"status":"healthy","version":"1.0.0","timestamp":"2026-10-20T19:29:25.786858","services":{}}
```

## 4. Start the sample HTTP backend

In the second terminal:

```bash
cd agentanywhere-universal-gateway
source .venv/bin/activate
python3 samples/backends/http/http_server.py
```

It listens on port 8003, which is where `HTTP_BACKEND_URL` in `env.example`
points. The gateway forwards requests under `/api/*`, `/rest/*` and `/http/*`
to it.

## 5. Create an API key

Open a third terminal and read the admin credential from `.env`:

```bash
cd agentanywhere-universal-gateway
ADMIN=$(grep '^ADMIN_TOKEN=' .env | tail -n 1 | cut -d= -f2-)
curl -s -X POST "http://localhost:8000/admin/api-keys?name=first-key" \
  -H "Authorization: Bearer $ADMIN"
```

The response describes the new key. Yours has a different `id`, `key` and
timestamps:

```json
{
  "id": "b0bf5920-d931-415d-85b6-ba009a47970f",
  "name": "first-key",
  "key": "sk-vx54enZD1I79TIgR6SB8dLB9p_XtaqLa4CkpZqbutkk",
  "key_prefix": "sk-vx54enZD",
  "rate_limit_per_minute": 60,
  "rate_limit_per_hour": 1000,
  "is_active": true,
  "created_at": "2026-10-20T19:29:25.794072",
  "expires_at": null,
  "allowed_paths": []
}
```

Copy the `key` value into a variable now. The gateway stores only a hash of
it and will never show it again (`GET /admin/api-keys` lists the `id` and
`key_prefix`, which you use to manage the key later).

```bash
API_KEY=paste-the-key-here
```

## 6. Send a request through the gateway

```bash
curl -s -X POST http://localhost:8000/api/users \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "Ada", "email": "ada@example.com"}'
```

The gateway checked the key, counted the request against the key's limits and
forwarded it to the sample backend, which created a user:

```json
{
  "id": "user_1",
  "name": "Ada",
  "email": "ada@example.com",
  "metadata": null,
  "created_at": "2026-10-20T19:29:25.848555+00:00"
}
```

Read it back the same way:

```bash
curl -s http://localhost:8000/api/users -H "X-API-Key: $API_KEY"
```

```json
[
  {
    "id": "user_1",
    "name": "Ada",
    "email": "ada@example.com",
    "metadata": null
  }
]
```

## 7. See what happens without a key

```bash
curl -s -X POST http://localhost:8000/api/users \
  -H "Content-Type: application/json" \
  -d '{"name": "Ada", "email": "ada@example.com"}'
```

```json
{
  "error": "AUTHENTICATION_ERROR",
  "message": "API key required",
  "request_id": "dca4d702-c288-40eb-ba10-8b32be8e170b",
  "timestamp": "2026-10-20T19:29:25.866307",
  "details": {
    "hint": "Include your API key in the X-API-Key header or Authorization header.",
    "documentation": "/docs#authentication"
  }
}
```

The status code is 401. The request never reached the backend. The response
carries an `X-Request-ID` header with the same value as `request_id`.

## What you did

- You configured the gateway with two secrets: `SECRET_KEY` and a separate
  `ADMIN_TOKEN`.
- You used the admin credential on the **admin plane** (`/admin/*`) to create
  an API key.
- You used the API key on the **data plane** to call a backend through the
  gateway.

The [architecture overview](../explanation/architecture.md) explains the two
planes. The [glossary](../glossary.md) defines the terms.

## How these outputs were produced

The outputs on this page come from running the same requests against the
gateway in process with FastAPI's test client, using a `.env` built from
`env.example` with the commands in step 2. The sample backend was mounted in
the same process instead of on port 8003. Timestamps, ids and keys differ on
every run. JSON is shown indented for reading; `curl` prints it on one line
(pipe it through `python3 -m json.tool` to indent it).

## If something went wrong

| What you see | What to do |
|---|---|
| `pip install` fails building a package | Check `python3 --version`. Use 3.10, 3.11 or 3.12. |
| `Address already in use` | Another program uses port 8000 or 8003. Stop it, or use `--port 8010` and change the URLs. |
| 401 `Invalid admin token` or `Authentication required` on step 5 | `ADMIN` is empty or wrong. Run `echo $ADMIN` and compare with `.env`. Restart the gateway after you edit `.env`. |
| 401 on step 6 | `API_KEY` is empty, or you created the key against a different database file. Start the gateway from the repository root. |
| 502 `BACKEND_ERROR` on step 6 | The sample backend is not running on port 8003. Check the second terminal. |

More symptoms are on the [troubleshooting page](../troubleshooting.md).

## Next

- [Tutorial: two backends, limits, quotas and metrics](two-backends-end-to-end.md)
- [Create, rotate and revoke API keys](../how-to/manage-api-keys.md)
- [Known limitations](../known-limitations.md)
