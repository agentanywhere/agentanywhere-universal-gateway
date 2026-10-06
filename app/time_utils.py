"""Centralised time helpers for the gateway.

``datetime.datetime.utcnow()`` is deprecated from Python 3.12 onwards, but the
database columns in this project are timezone-*naive* (``Column(DateTime)`` with
``default=datetime.utcnow``) and much of the code compares "now" against those
stored values. Mixing naive and timezone-aware datetimes raises ``TypeError``,
so we standardise on a single helper that returns a **naive UTC** timestamp
without the deprecated call.

This keeps behaviour identical to the historic ``datetime.utcnow()`` (same
``isoformat()`` output, safe comparisons with DB values) while giving us one
place to evolve time handling (e.g. move to fully timezone-aware storage) later.
"""
from datetime import datetime, timezone

__all__ = ["utcnow"]


def utcnow() -> datetime:
    """Return the current UTC time as a naive ``datetime`` (no tzinfo).

    Equivalent to the deprecated ``datetime.utcnow()`` but implemented on top of
    the timezone-aware ``datetime.now(timezone.utc)`` so it does not emit a
    ``DeprecationWarning``.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
