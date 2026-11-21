# Run the tests

**Goal:** run the automated test suite, or part of it, the way continuous
integration does.

**Before you start:** Python 3.10, 3.11 or 3.12 with the project's
dependencies installed (`pip install -r requirements.txt`, which includes
pytest and its plugins). No server, database or Redis is needed: the tests use
an in-memory SQLite database and FastAPI's test client.

## Steps

1. Run the whole suite from the repository root:

   ```bash
   pytest
   ```

   `pytest.ini` adds `-v --strict-markers --tb=short` and coverage for `app`
   and `admin`, written to the terminal, `htmlcov/` and `coverage.xml`.

2. Run part of it:

   ```bash
   pytest tests/test_rate_limit.py                     # one file
   pytest tests/test_auth.py -k invalid                # tests whose name contains "invalid"
   pytest -o addopts="" -q tests/test_docs_quickstart.py   # without coverage, quieter
   ```

3. Run the documentation checks (standard library only, no dependencies):

   ```bash
   python scripts/check_docs.py
   python scripts/check_env_vars.py
   python scripts/generate_http_reference.py --check   # needs the dependencies
   ```

**Check:** pytest ends with a summary line of passed and skipped tests, and no
failures. `pytest --collect-only -q` prints the current number of tests.

## What is skipped

- `tests/perf/` starts a real stack and runs only with `pytest -m performance`; see
  [Test performance](performance-testing.md).

- A test in `tests/test_log_exporters.py` is skipped when `boto3` is not
  installed (it is not in `requirements.txt`).
- `tests/test_container_integration.py` runs only when a gateway answers at
  `GATEWAY_URL` (default `http://localhost:8000`). If something else listens
  on that port, those tests run against it and fail for reasons unrelated to
  the code. Point the variable at an unused port, or set
  `REQUIRE_INTEGRATION=1` to turn a missing gateway into a failure:

  ```bash
  GATEWAY_URL=http://127.0.0.1:59999 pytest
  ```

- The PostgreSQL cases in `tests/test_migrations.py` run only when
  `GATEWAY_TEST_POSTGRES_URL` is set to a `postgresql+asyncpg://` URL of an
  empty, disposable database (they drop every table in it). CI does not set
  it.
- A test in `tests/test_followup_fixes.py` is skipped when `bash` or
  `scripts/quickstart_test.sh` is missing.

## How the tests are organised

- One file per area, named `tests/test_<area>.py`: `test_auth.py`,
  `test_rate_limit.py`, `test_routing.py`, `test_admin_api.py`,
  `test_tenants.py`, `test_plugins.py`, `test_cli.py` and so on.
- `tests/conftest.py` sets a non-placeholder `SECRET_KEY` before the app is
  imported, and provides fixtures: `db_session` (in-memory SQLite with every
  table), `client` (a `TestClient` whose database dependency is the test
  session), `sample_api_key` and `sample_route`.
- Admin calls in tests authenticate with `settings.secret_key`.
- Markers `unit`, `integration` and `asyncio` are declared; `asyncio_mode = auto`
  runs `async def` tests without a decorator. `make test-unit` and
  `make test-integration` select by marker.
- `tests/test_docs_quickstart.py` replays the getting-started tutorial against
  the app, with the sample HTTP backend mounted in process, and runs the two
  documentation checkers.

## What continuous integration runs

`.github/workflows/ci.yml`, on every push to `main` or `master` and every pull
request, on Python 3.10, 3.11 and 3.12:

1. `python -m compileall -q app admin`
2. `ruff check app admin tests --select F821,F823`
3. The documentation checks above
4. `pytest` with coverage
5. `python -m build`

`make lint` runs steps 1 and 2 locally (install `ruff` first).

A separate `dataplane` job builds and tests the experimental Rust data plane
and checks it against the Python gateway; see
[Run the Rust data plane](run-the-data-plane.md#run-its-tests). You do not
need Rust to run the Python suite.

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError` | Dependencies not installed, or a different interpreter | Activate the virtual environment; `pip install -r requirements.txt`. |
| Installation fails on Python 3.13 | Not supported | Use 3.10 to 3.12. |
| `test_container_integration.py` fails | Something else answers on port 8000 | Set `GATEWAY_URL` to an unused port. |
| `generate_http_reference.py --check` fails | A route changed | Run `python scripts/generate_http_reference.py` and commit the page. |
| `check_env_vars.py` fails | A setting was added, removed or renamed | Update `docs/reference/configuration.md`. |
