"""CORS: a wildcard origin never goes out together with credentials."""
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

import app.main as gateway_main
from app.config import Settings, cors_middleware_options

REPO = Path(__file__).resolve().parent.parent
EVIL = "https://evil.example"


def _settings(**kw):
    return Settings(_env_file=None, **kw)


def _app(options):
    app = FastAPI()
    app.add_middleware(CORSMiddleware, **options)

    @app.get("/x")
    def x():
        return {"ok": True}

    return app


def test_wildcard_with_credentials_disables_credentials_and_warns():
    options, warning = cors_middleware_options(
        _settings(cors_origins=["*"], cors_allow_credentials=True)
    )
    assert options["allow_origins"] == ["*"]
    assert options["allow_credentials"] is False
    assert warning and "CORS_ORIGINS" in warning


def test_wildcard_mixed_with_explicit_origins_still_disables_credentials():
    options, warning = cors_middleware_options(
        _settings(cors_origins=["*", "http://localhost:3001"], cors_allow_credentials=True)
    )
    assert options["allow_credentials"] is False
    assert warning


def test_wildcard_without_credentials_is_left_alone():
    options, warning = cors_middleware_options(
        _settings(cors_origins=["*"], cors_allow_credentials=False)
    )
    assert options["allow_credentials"] is False
    assert warning is None


def test_explicit_origins_keep_credentials():
    origins = ["http://localhost:3001", "https://console.example.com"]
    options, warning = cors_middleware_options(
        _settings(cors_origins=origins, cors_allow_credentials=True)
    )
    assert options["allow_origins"] == origins
    assert options["allow_credentials"] is True
    assert warning is None


def test_code_default_has_no_wildcard():
    s = _settings()
    assert "*" not in s.cors_origins
    assert "http://localhost:3001" in s.cors_origins


def test_wildcard_config_never_allows_credentials_for_a_foreign_origin():
    raw = {"allow_origins": ["*"], "allow_credentials": True,
           "allow_methods": ["*"], "allow_headers": ["*"]}
    # The defect: passed straight to Starlette, "*" plus credentials echoes
    # any origin back with credentials allowed.
    unsafe = TestClient(_app(raw)).get("/x", headers={"Origin": EVIL, "Cookie": "s=1"})
    assert unsafe.headers.get("access-control-allow-origin") == EVIL
    assert unsafe.headers.get("access-control-allow-credentials") == "true"

    options, _ = cors_middleware_options(
        _settings(cors_origins=["*"], cors_allow_credentials=True)
    )
    client = TestClient(_app(options))

    res = client.get("/x", headers={"Origin": EVIL, "Cookie": "session=1"})
    # Starlette may still name the origin when a cookie is sent, but without
    # Allow-Credentials the browser does not hand the response to the page.
    assert res.headers.get("access-control-allow-origin") in {"*", EVIL}
    assert "access-control-allow-credentials" not in res.headers

    plain = client.get("/x", headers={"Origin": EVIL})
    assert plain.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in plain.headers

    pre = client.options("/x", headers={
        "Origin": EVIL, "Access-Control-Request-Method": "GET",
    })
    assert pre.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in pre.headers


def test_explicit_origins_allow_only_those_with_credentials():
    options, _ = cors_middleware_options(
        _settings(cors_origins=["http://localhost:3001"], cors_allow_credentials=True)
    )
    client = TestClient(_app(options))

    ok = client.get("/x", headers={"Origin": "http://localhost:3001"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:3001"
    assert ok.headers["access-control-allow-credentials"] == "true"

    bad = client.get("/x", headers={"Origin": EVIL})
    assert "access-control-allow-origin" not in bad.headers


def test_gateway_app_uses_the_safe_options():
    cors = [m for m in gateway_main.app.user_middleware if m.cls is CORSMiddleware]
    assert len(cors) == 1
    kwargs = cors[0].kwargs if hasattr(cors[0], "kwargs") else cors[0].options
    expected, _ = cors_middleware_options(gateway_main.settings)
    assert kwargs == expected
    assert not ("*" in kwargs["allow_origins"] and kwargs["allow_credentials"])


def test_env_example_lists_the_admin_console_origins():
    line = next(
        l for l in (REPO / "env.example").read_text().splitlines()
        if l.startswith("CORS_ORIGINS=")
    )
    origins = json.loads(line.split("=", 1)[1])
    assert "*" not in origins
    assert "http://localhost:3001" in origins
    # The console really is on 3001: Vite dev server and docker-compose.
    assert "port: 3001" in (REPO / "admin-ui" / "vite.config.ts").read_text()
    assert '"3001:80"' in (REPO / "docker-compose.yml").read_text()
