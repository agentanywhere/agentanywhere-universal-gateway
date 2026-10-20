# Test suite

Pytest-based suite under `tests/`; shared fixtures and DB setup are in `conftest.py`.

## Run

```bash
pip install -r requirements.txt
pytest              # full suite + coverage (see pytest.ini)
make test           # same via Makefile
make test-cov
```

For what CI runs, see `docs/how-to/run-the-tests.md`.

## Scope

The tree includes modules for health, config, repositories, auth, rate limiting, routing, gateway proxy behavior, admin API, OAuth2, versioning, transformations, webhooks, tenants, log exporters, Slack/email notifications, plugins, observability, governance, CLI, and more. Run `pytest --collect-only -q` for the current count.

## Infrastructure

- In-memory SQLite for most tests  
- Async fixtures (`pytest-asyncio`)  
- Mocks for external HTTP, email, and cloud sinks where needed  

Coverage targets **`app`** and **`admin`** packages per `pytest.ini`.
