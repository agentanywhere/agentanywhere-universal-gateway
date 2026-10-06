"""Local administrator sign-in, and credentials that must be wrong rather than fatal.

Two defects are locked here.

1. ``POST /admin/auth/login`` accepted the hard-coded pair ``admin`` /
   ``password`` and answered with a token shaped ``admin-admin-<timestamp>``.
   While any token beginning with ``admin-`` was honoured, that was a working
   default password on every deployment. Once the ``admin-`` prefix stopped
   being honoured, the same endpoint kept "succeeding" and handed the admin
   console a token every later call rejected, so nobody could sign in at all.
   Sign-in now checks the configured admin credential and returns it.

2. A credential containing a non-ASCII character made ``hmac.compare_digest``
   raise ``TypeError``, so an unauthenticated request got HTTP 500 instead of
   401, on the admin API and on the proxy path alike.
"""
import pytest

from app.config import get_settings
from app.secure_compare import constant_time_equals

SETTINGS = get_settings()
NON_ASCII = "café-" + "x" * 30


def _login(client, username, password):
    return client.post("/admin/auth/login", json={"username": username, "password": password})


def test_hardcoded_default_password_is_gone(client):
    response = _login(client, "admin", "password")
    assert response.status_code == 401


def test_sign_in_with_the_configured_credential_yields_a_usable_token(client):
    response = _login(client, "admin", SETTINGS.secret_key)
    assert response.status_code == 200
    token = response.json()["access_token"]

    # The whole point: the token the console is handed must actually work.
    listed = client.get("/admin/api-keys", headers={"Authorization": f"Bearer {token}"})
    assert listed.status_code == 200


def test_sign_in_rejects_a_wrong_username(client):
    assert _login(client, "root", SETTINGS.secret_key).status_code == 401


def test_sign_in_rejects_a_wrong_password(client):
    assert _login(client, "admin", SETTINGS.secret_key + "x").status_code == 401
    assert _login(client, "admin", "").status_code == 401


def test_admin_token_takes_over_as_the_password_when_set(client, monkeypatch):
    import admin.api as admin_api

    monkeypatch.setattr(admin_api.settings, "admin_token", "a-dedicated-admin-token-for-this-test")
    assert _login(client, "admin", SETTINGS.secret_key).status_code == 401
    response = _login(client, "admin", "a-dedicated-admin-token-for-this-test")
    assert response.status_code == 200
    assert response.json()["access_token"] == "a-dedicated-admin-token-for-this-test"


def test_sign_in_is_impossible_when_no_credential_is_configured(client, monkeypatch):
    """Placeholder SECRET_KEY and no ADMIN_TOKEN: closed, not open."""
    import admin.api as admin_api

    monkeypatch.setattr(admin_api.settings, "admin_token", "")
    monkeypatch.setattr(admin_api.settings, "secret_key", "change-me-in-production-use-strong-random-key")
    for password in ("password", "", "change-me-in-production-use-strong-random-key"):
        assert _login(client, "admin", password).status_code == 401


def test_non_ascii_admin_bearer_is_401_not_500(client):
    # Sent as raw latin-1 bytes, which is how a server receives a byte >= 0x80
    # in a header (the test client refuses to encode a non-ASCII str itself).
    response = client.get(
        "/admin/api-keys",
        headers={"Authorization": f"Bearer {NON_ASCII}".encode("latin-1")},
    )
    assert response.status_code == 401


def test_non_ascii_sign_in_password_is_401_not_500(client):
    assert _login(client, "admin", NON_ASCII).status_code == 401
    assert _login(client, "édmin", SETTINGS.secret_key).status_code == 401


def test_non_ascii_api_key_is_401_not_500(client):
    response = client.post(
        "/mcp/query", headers={"X-API-Key": NON_ASCII.encode("latin-1")}, json={"query": "x"}
    )
    assert response.status_code == 401


@pytest.mark.parametrize("candidate,secret,expected", [
    ("same-value", "same-value", True),
    ("same-value", "other-value", False),
    ("", "secret", False),
    (None, "secret", False),
    ("candidate", "", False),
    ("candidate", None, False),
    ("café", "café", True),
    ("café", "cafe", False),
])
def test_constant_time_equals(candidate, secret, expected):
    assert constant_time_equals(candidate, secret) is expected
