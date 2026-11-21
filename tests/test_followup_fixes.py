"""Regression tests for the follow-up fixes:

1. ``POST /admin/api-keys`` now accepts and persists ``expires_at`` /
   ``expires_in_days`` / ``allowed_paths``.
2. The version manager's headers (``X-API-Version``, ``Warning``,
   ``Sunset``, ``Deprecation``) now appear on responses, not only on
   forwarded upstream requests.
3. The observability tracer auto-creates a span per request and a
   child ``backend.forward`` span around the upstream call.
4. ``scripts/quickstart_test.sh`` picks a Python interpreter that has
   the project dependencies installed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from app.time_utils import utcnow
from pathlib import Path

import pytest

from app.config import get_settings
from app.observability import tracer

settings = get_settings()


@pytest.fixture
def admin_token():
    return settings.secret_key


# ---------------------------------------------------------------------------
# 1. API key expiry
# ---------------------------------------------------------------------------


def test_create_api_key_with_expires_at(client, admin_token):
    soon = (utcnow() + timedelta(days=3)).isoformat()
    res = client.post(
        f"/admin/api-keys?name=expires-soon&expires_at={soon}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["expires_at"], "expires_at should be persisted"
    assert body["name"] == "expires-soon"


def test_create_api_key_with_expires_in_days(client, admin_token):
    res = client.post(
        "/admin/api-keys?name=expires-90-days&expires_in_days=90",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["expires_at"]
    exp = datetime.fromisoformat(body["expires_at"])
    # Should be 90 days +/- a couple of seconds.
    delta = exp - utcnow()
    assert timedelta(days=89, hours=23) < delta < timedelta(days=90, hours=1)


def test_create_api_key_rejects_past_expiry(client, admin_token):
    past = (utcnow() - timedelta(days=1)).isoformat()
    res = client.post(
        f"/admin/api-keys?name=already-past&expires_at={past}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 400


def test_create_api_key_rejects_invalid_iso(client, admin_token):
    res = client.post(
        "/admin/api-keys?name=bad-date&expires_at=not-a-date",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 400


def test_create_api_key_rejects_both_expiry_styles(client, admin_token):
    iso = (utcnow() + timedelta(days=1)).isoformat()
    res = client.post(
        f"/admin/api-keys?name=clash&expires_at={iso}&expires_in_days=30",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 400


def test_create_api_key_with_allowed_paths(client, admin_token):
    res = client.post(
        "/admin/api-keys?name=scoped&allowed_paths=/mcp,/api",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["allowed_paths"] == ["/mcp", "/api"]


# ---------------------------------------------------------------------------
# 2. Version headers echoed on the response
# ---------------------------------------------------------------------------


def test_versioned_request_returns_version_headers(client, admin_token):
    # Register a version we'll deprecate.
    client.post(
        "/admin/versions",
        json={"version": "9", "default": False},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    client.post(
        "/admin/versions/9/deprecate",
        json="2099-01-01T00:00:00",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    # Need a valid API key for the proxy path (gateway requires auth).
    key_res = client.post(
        "/admin/api-keys?name=version-test",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    api_key = key_res.json()["key"]

    res = client.get("/v9/something", headers={"X-API-Key": api_key})
    # We don't care about the body (backend returns 502 in tests); the
    # headers are added by the request middleware regardless.
    assert res.headers.get("X-API-Version") == "9"
    assert "Warning" in res.headers
    assert "deprecated" in res.headers["Warning"].lower()
    assert "Sunset" in res.headers

    # Cleanup.
    client.delete(
        "/admin/versions/9?hard=true",
        headers={"Authorization": f"Bearer {admin_token}"},
    )


def test_default_version_does_not_set_warning(client, admin_token):
    """The default version (1) is active — no Warning/Sunset header."""
    key_res = client.post(
        "/admin/api-keys?name=default-version",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    api_key = key_res.json()["key"]

    res = client.get("/v1/foo", headers={"X-API-Key": api_key})
    assert res.headers.get("X-API-Version") == "1"
    assert "Warning" not in res.headers
    assert "Sunset" not in res.headers


# ---------------------------------------------------------------------------
# 3. Auto request-lifecycle span
# ---------------------------------------------------------------------------


def test_request_creates_trace_in_buffer(client, admin_token):
    tracer.clear()  # start from a clean ring buffer

    key_res = client.post(
        "/admin/api-keys?name=trace-test",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    api_key = key_res.json()["key"]

    # Hit a proxy route — the middleware should open one parent span
    # and the routing layer should open a `backend.forward` child span.
    client.get("/api/anything", headers={"X-API-Key": api_key})

    traces = tracer.get_recent_traces(limit=10)
    assert traces, "no trace recorded"
    # Find a trace that includes a backend.forward span.
    candidate = next(
        (t for t in traces if any(s["name"] == "backend.forward" for s in t["spans"])),
        None,
    )
    assert candidate is not None, "no trace included a backend.forward span"
    span_names = {s["name"] for s in candidate["spans"]}
    parent_name_prefix = any(
        s["name"].startswith("GET ") for s in candidate["spans"]
    )
    assert parent_name_prefix, f"no GET parent span; saw {span_names}"


def test_traceparent_id_propagates_into_tracer(client, admin_token):
    """When the client sends a ``traceparent`` header, the in-process
    tracer should reuse the same trace id for its parent span."""
    tracer.clear()

    key_res = client.post(
        "/admin/api-keys?name=trace-id-test",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    api_key = key_res.json()["key"]

    incoming_trace = "0af7651916cd43dd8448eb211c80319c"
    client.get(
        "/api/whatever",
        headers={
            "X-API-Key": api_key,
            "traceparent": f"00-{incoming_trace}-b7ad6b7169203331-01",
        },
    )

    traces = tracer.get_recent_traces(limit=10)
    matching = [t for t in traces if t["trace_id"] == incoming_trace]
    assert matching, f"expected a trace with id {incoming_trace}, got {[t['trace_id'] for t in traces]}"


# ---------------------------------------------------------------------------
# 4. quickstart_test.sh interpreter selection
# ---------------------------------------------------------------------------


def test_quickstart_script_picks_python_with_requests():
    """The ``choose_python`` helper in the quickstart script should
    surface a Python interpreter that can ``import requests``.

    We invoke the bash helper directly in a sub-shell so we don't depend
    on the gateway being up. Needs only bash and the script.
    """
    if shutil.which("bash") is None:  # pragma: no cover
        pytest.skip("bash not available")
    script = Path("scripts/quickstart_test.sh")
    if not script.exists():  # pragma: no cover
        pytest.skip("quickstart script not present")

    # Sourcing the whole script would run the smoke tests against a gateway,
    # so copy just the helper out of it: from the `choose_python()` line to
    # the first line that starts with `}`. This is done in Python rather than
    # with sed, because Git for Windows puts bash on PATH but not sed.
    lines = script.read_text(encoding="utf-8").splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("choose_python()")),
        None,
    )
    assert start is not None, "helper not found in script"
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("}"))
    extracted = "\n".join(lines[start : end + 1]) + "\n"

    # Put the current interpreter first on PATH so the helper finds one that
    # can import requests, locally and in CI.
    interpreter_dir = os.path.dirname(sys.executable)
    env = os.environ.copy()
    env["PATH"] = f"{interpreter_dir}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        ["bash", "-c", f"{extracted}\nchoose_python"],
        capture_output=True,
        text=True,
        env=env,
    )
    chosen = result.stdout.strip()
    assert chosen, f"choose_python returned nothing. stderr={result.stderr}"
    # Confirm the chosen interpreter actually has 'requests'.
    rc = subprocess.run(
        [chosen, "-c", "import requests"],
        capture_output=True,
    ).returncode
    assert rc == 0, f"{chosen} cannot import requests"
