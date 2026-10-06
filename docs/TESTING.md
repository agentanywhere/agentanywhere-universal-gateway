# Testing and CI

## Automated tests

```bash
pip install -r requirements.txt
pytest
```

`pytest.ini` runs the suite with coverage over `app` and `admin`. At the time
of release it is 691 passed and 2 skipped, with 75% line coverage.

One of the two skips is `tests/test_container_integration.py`, a set of
end-to-end tests that only run when a gateway answers at `GATEWAY_URL`
(default `http://localhost:8000`). If anything else is listening on that port
the tests will run against it and fail for reasons that have nothing to do
with this code. Either stop that service or point the variable at an unused
port:

```bash
GATEWAY_URL=http://127.0.0.1:59999 pytest
```

Set `REQUIRE_INTEGRATION=1` to make a missing gateway a failure instead of a skip.

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request, on Python 3.10,
3.11 and 3.12:

1. `python -m compileall -q app admin`
2. `ruff check app admin tests --select F821,F823`
3. `pytest` with coverage
4. `python -m build`

## Release workflows

Two more workflows run only when a `v*` tag is pushed. Neither has been run yet,
because nothing has been released.

- `.github/workflows/docker-publish.yml` builds the root `Dockerfile` and pushes
  it to Docker Hub. It needs the `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`
  repository secrets.
- `.github/workflows/pypi-publish.yml` builds the package and publishes it with
  PyPI Trusted Publishing. It needs a trusted publisher configured on PyPI.
