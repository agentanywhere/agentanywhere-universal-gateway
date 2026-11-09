"""Log redaction must not destroy uuid request ids."""
import uuid

import pytest

from app.logging_config import REDACTED, _redact_value


def test_generated_uuid4s_survive():
    ids = [str(uuid.uuid4()) for _ in range(20000)]
    # make sure some have long digit-only runs (the old failure mode)
    ids += ["12345678-1234-4234-8123-123456789012", "00000000-0000-4000-8000-000000000000"]
    for i in ids:
        assert _redact_value(i) == i
        assert _redact_value(f"request {i} done") == f"request {i} done"
    h = uuid.uuid4().hex
    assert _redact_value(h) == h
    assert _redact_value("1" * 8 + "2" * 24) == "1" * 8 + "2" * 24


@pytest.mark.parametrize("card", ["4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111", "378282246310005"])
def test_cards_still_redacted(card):
    assert REDACTED in _redact_value(card)
    assert "4111" not in _redact_value(f"pay {card} now") and "3782" not in _redact_value(f"pay {card} now")


def test_card_next_to_uuid_and_tokens():
    u = "12345678-1234-4234-8123-123456789012"
    out = _redact_value(f"{u} card 4111111111111111")
    assert out.startswith(u) and "4111111111111111" not in out
    assert REDACTED in _redact_value("Bearer abcdefghijklmnop")
    assert REDACTED in _redact_value("sk-" + "a" * 20)
    assert _redact_value({"id": u, "c": "4111111111111111"}) == {"id": u, "c": REDACTED}
