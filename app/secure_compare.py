"""Constant-time credential comparison that never raises.

``hmac.compare_digest`` refuses ``str`` arguments containing non-ASCII
characters (``TypeError``). Credentials arrive from HTTP headers, which the
server decodes as latin-1, so a single byte >= 0x80 in an ``Authorization`` or
``X-API-Key`` header used to turn a wrong credential into an HTTP 500 instead
of a 401. Comparing the UTF-8 bytes keeps the comparison constant-time and
makes every wrong credential simply wrong.
"""
import hmac
from typing import Optional


def constant_time_equals(candidate: Optional[str], secret: Optional[str]) -> bool:
    """True when both values are non-empty and identical, compared in constant time."""
    if not candidate or not secret:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), secret.encode("utf-8"))
