"""Admin authentication must reject anything that is not a configured secret.

These assertions exist because the obvious permissive shortcuts — accept any
token with a known prefix, or accept any token above some length when SSO is
not configured — are easy to reintroduce while making a local workflow more
convenient, and each one silently opens all 124 admin endpoints to anybody.

Admin auth is therefore allowlist-only: an explicitly configured ADMIN_TOKEN,
or SECRET_KEY when it is not the placeholder shipped in env.example. Nothing
else authenticates, and both comparisons are constant-time.
"""

import pytest

from app.config import get_settings
from admin.api import _is_placeholder_secret, verify_admin_token_value
from fastapi import HTTPException

SETTINGS = get_settings()


@pytest.mark.parametrize("token", [
    "a" * 21,
    "x" * 200,
    "not-a-real-token-but-long-enough",
    "eyJhbGciOiJIUzI1NiJ9.not.a.real.jwt.at.all",
])
def test_long_arbitrary_strings_are_rejected(token):
    """A long arbitrary string is not a credential."""
    with pytest.raises(HTTPException) as e:
        verify_admin_token_value(token)
    assert e.value.status_code == 401


@pytest.mark.parametrize("token", [
    "admin-",
    "admin-anything",
    "admin-" + "z" * 100,
])
def test_admin_prefixed_strings_are_rejected(token):
    """A recognisable prefix is not a credential either."""
    with pytest.raises(HTTPException) as e:
        verify_admin_token_value(token)
    assert e.value.status_code == 401


def test_the_configured_secret_key_still_authenticates():
    """Single-operator deployments authenticate with the configured key."""
    assert verify_admin_token_value(SETTINGS.secret_key) == SETTINGS.secret_key


def test_empty_and_missing_tokens_are_rejected():
    for token in (None, ""):
        with pytest.raises(HTTPException) as e:
            verify_admin_token_value(token)
        assert e.value.status_code == 401


def test_placeholder_secret_is_never_a_valid_credential():
    """env.example ships this value, so it is public in this repository.
    Accepting it would authenticate anyone who read the repo."""
    assert _is_placeholder_secret("change-me-in-production")
    assert _is_placeholder_secret("change-me-in-production-use-strong-random-key")
    assert _is_placeholder_secret("")
    assert not _is_placeholder_secret(SETTINGS.secret_key)

    with pytest.raises(HTTPException):
        verify_admin_token_value("change-me-in-production-use-strong-random-key")
