"""The break-glass SECRET_KEY must never be written to logs or handed on as a client id."""
from types import SimpleNamespace
from unittest.mock import patch

from app.config import get_settings
from app.main import safe_key_label


def test_a_key_with_an_id_is_labelled_by_its_id():
    assert safe_key_label(SimpleNamespace(id="abc-123", key="sk-secret")) == "abc-123"


def test_a_key_without_an_id_is_labelled_by_a_hash_not_the_key():
    label = safe_key_label(SimpleNamespace(id=None, key="sk-very-secret-value"))
    assert label.startswith("key-") and len(label) == 16
    assert "sk-very-secret-value" not in label
    assert label == safe_key_label(SimpleNamespace(id=None, key="sk-very-secret-value"))      # stable
    assert label != safe_key_label(SimpleNamespace(id=None, key="sk-another"))


def test_nothing_is_labelled_when_there_is_no_key():
    assert safe_key_label(None) is None
    assert safe_key_label(SimpleNamespace(id=None, key=None)) is None


def test_the_secret_key_never_reaches_the_log_context(client):
    secret = get_settings().secret_key
    calls = []
    with patch("app.main.bind_request_context", side_effect=lambda **kw: calls.append(kw)):
        client.get("/mcp/some/thing", headers={"X-API-Key": secret})
    assert calls, "the request context was never bound"
    assert all(secret not in str(v) for kw in calls for v in kw.values())
