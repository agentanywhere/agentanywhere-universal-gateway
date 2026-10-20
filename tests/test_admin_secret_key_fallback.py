"""SECRET_KEY is a break-glass admin credential only while ADMIN_TOKEN is empty.

It used to be accepted on every admin endpoint even when ADMIN_TOKEN was set,
so setting ADMIN_TOKEN changed the console password but left a second,
undocumented admin credential open. The four combinations of
ADMIN_TOKEN (set / empty) and SECRET_KEY (real / placeholder) are pinned here,
through the dependency and through a real admin endpoint.
"""
import pytest
from fastapi import HTTPException

import admin.api as admin_api
from admin.api import verify_admin_token_value

REAL_SECRET = "a-real-secret-key-for-this-test-only-0123456789"
PLACEHOLDER = "change-me-in-production-use-strong-random-key"
ADMIN_TOKEN = "a-dedicated-admin-token-for-this-test"


def _accepts(token):
    try:
        verify_admin_token_value(token)
        return True
    except HTTPException as e:
        assert e.status_code == 401
        return False


@pytest.fixture
def creds(monkeypatch):
    def set_(admin_token, secret_key, accept_secret_key=False):
        monkeypatch.setattr(admin_api.settings, "admin_token", admin_token)
        monkeypatch.setattr(admin_api.settings, "secret_key", secret_key)
        monkeypatch.setattr(admin_api.settings, "admin_accept_secret_key", accept_secret_key)
    return set_


def test_admin_token_set_and_real_secret_key_only_admin_token_works(creds, client):
    creds(ADMIN_TOKEN, REAL_SECRET)
    assert _accepts(ADMIN_TOKEN)
    assert not _accepts(REAL_SECRET)
    # Through a real endpoint as well.
    ok = client.get("/admin/api-keys", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    assert ok.status_code == 200
    refused = client.get("/admin/api-keys", headers={"Authorization": f"Bearer {REAL_SECRET}"})
    assert refused.status_code == 401


def test_admin_token_set_and_placeholder_secret_key(creds):
    creds(ADMIN_TOKEN, PLACEHOLDER)
    assert _accepts(ADMIN_TOKEN)
    assert not _accepts(PLACEHOLDER)


def test_admin_token_empty_and_real_secret_key_is_the_break_glass(creds, client):
    creds("", REAL_SECRET)
    assert _accepts(REAL_SECRET)
    assert not _accepts("")
    ok = client.get("/admin/api-keys", headers={"Authorization": f"Bearer {REAL_SECRET}"})
    assert ok.status_code == 200


def test_admin_token_empty_and_placeholder_secret_key_nothing_works(creds):
    creds("", PLACEHOLDER)
    assert not _accepts(PLACEHOLDER)
    assert not _accepts("")
    assert not _accepts(None)
    assert not _accepts(ADMIN_TOKEN)


def test_opt_in_restores_accepting_both(creds):
    creds(ADMIN_TOKEN, REAL_SECRET, accept_secret_key=True)
    assert _accepts(ADMIN_TOKEN)
    assert _accepts(REAL_SECRET)


def test_opt_in_never_accepts_the_placeholder(creds):
    creds(ADMIN_TOKEN, PLACEHOLDER, accept_secret_key=True)
    assert not _accepts(PLACEHOLDER)


def test_ops_endpoints_follow_the_same_rule(creds, client, monkeypatch):
    import app.main as main

    creds(ADMIN_TOKEN, REAL_SECRET)
    monkeypatch.setattr(main.settings, "ops_endpoints_require_admin", True)
    assert client.get("/health/detailed", headers={"Authorization": f"Bearer {REAL_SECRET}"}).status_code == 401
    assert client.get("/health/detailed", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}).status_code != 401


def test_login_with_secret_key_fails_once_admin_token_is_set(creds, client):
    creds(ADMIN_TOKEN, REAL_SECRET)
    bad = client.post("/admin/auth/login", json={"username": "admin", "password": REAL_SECRET})
    assert bad.status_code == 401
