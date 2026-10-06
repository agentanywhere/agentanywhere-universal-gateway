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

- **The admin API answers 401.** `SECRET_KEY` in `.env` is still the
  placeholder, or you are sending a different value from the one the gateway
  started with. Restart the gateway after changing `.env`.
- **A request answers 502 or 503.** The backend for that route is not running,
  or its `*_BACKEND_URL` points somewhere else. 503 after several 502s means
  the circuit breaker has opened; it closes again after
  `CIRCUIT_BREAKER_TIMEOUT` seconds.
- **Port 8000 is taken.** Something else is listening there. Use
  `--port 8010` and change the URLs above to match.
