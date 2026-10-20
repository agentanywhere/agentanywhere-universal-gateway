"""The client address of a request, with X-Forwarded-For handled safely.

``X-Forwarded-For`` is written by whoever sends the request, so it is only
evidence when the connection comes from a proxy you run. The rules:

* With ``TRUSTED_PROXIES`` empty (the default) the header is ignored and the
  client address is the socket peer.
* When the socket peer is in ``TRUSTED_PROXIES``, the header is read from the
  right. Each proxy appends the address it received the connection from, so
  the right-most entry that is not itself a trusted proxy is the client. The
  left part of the header is whatever the client chose to send and is never
  used.
* When the peer is not a trusted proxy, the header is ignored.

``TRUSTED_PROXIES`` entries are IP addresses or CIDR ranges
(``10.0.0.0/8``, ``::1``).
"""
from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Iterable, List, Optional, Tuple, Union

from app.config import get_settings

_Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


@lru_cache(maxsize=32)
def _parse_networks(entries: Tuple[str, ...]) -> Tuple[_Network, ...]:
    networks: List[_Network] = []
    for entry in entries:
        entry = (entry or "").strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            # A host name or a typo is not an address; ignore it rather than
            # trusting something we cannot match.
            continue
    return tuple(networks)


def _parse_ip(value: str) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    value = (value or "").strip().strip('"')
    if not value:
        return None
    # "[2001:db8::1]:443" and "203.0.113.5:8080" forms.
    if value.startswith("["):
        value = value[1:].split("]", 1)[0]
    elif value.count(":") == 1:
        value = value.split(":", 1)[0]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def is_trusted(address: Optional[str], trusted: Iterable[str]) -> bool:
    networks = _parse_networks(tuple(trusted or ()))
    if not networks:
        return False
    ip = _parse_ip(address or "")
    if ip is None:
        return False
    return any(ip in net for net in networks)


def resolve_client_ip(
    peer: Optional[str],
    forwarded_for: Optional[str],
    trusted: Iterable[str],
) -> Optional[str]:
    """Pure function behind :func:`client_ip`, for tests and other callers."""
    trusted = tuple(trusted or ())
    if not forwarded_for or not is_trusted(peer, trusted):
        return peer
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    for hop in reversed(hops):
        if not is_trusted(hop, trusted):
            ip = _parse_ip(hop)
            # A garbage entry is not an address; stop and fall back to the
            # last proxy we trust rather than reporting attacker text.
            return str(ip) if ip is not None else peer
    # Every hop is a trusted proxy: the left-most one is the closest we get.
    ip = _parse_ip(hops[0]) if hops else None
    return str(ip) if ip is not None else peer


def client_ip(request) -> Optional[str]:
    """The client address for IP rules, rate limits, logs and metrics."""
    client = getattr(request, "client", None)
    peer = getattr(client, "host", None) if client else None
    try:
        headers = request.headers
        forwarded_for = headers.get("x-forwarded-for") if headers is not None else None
    except Exception:  # noqa: BLE001 - stand-in request objects
        forwarded_for = None
    trusted = getattr(get_settings(), "trusted_proxies", None) or ()
    return resolve_client_ip(peer, forwarded_for, trusted)


__all__ = ["client_ip", "is_trusted", "resolve_client_ip"]
