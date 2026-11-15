"""The working set the Rust data plane keeps in memory, and the internal endpoint that serves it.

The data plane (see ``dataplane/DESIGN.md``) authenticates API keys itself, so it needs the keys, as
SHA-256 digests only (never plaintext), their limits, and which of them it may serve without asking
this gateway. It polls ``GET /internal/dataplane/snapshot?since=<revision>`` a couple of times a second.
The revision is a hash of the snapshot's content, so any change, however it was made and by whichever
worker, changes it, and an unchanged snapshot costs a tiny response.

The snapshot also carries what this gateway enforces on a key's requests, compiled into a form the data
plane evaluates itself: the key's path restriction, IP rules, policies, its tenant's quota, and the
trusted proxies behind which the client address is read. Anything the data plane cannot evaluate exactly
as this gateway does (a policy that matches on headers, a quota that is not a whole number, ...) marks
the keys it applies to ``native_ok: false``. Dynamic routes, transformation rules and request-altering
plugins turn native serving off for every path. When in doubt the answer is "ask the Python gateway",
which is always correct and only slower.

A key may arrive in the configured header or as ``Authorization: Bearer``; the data plane reads both as
this gateway does. An OAuth2 access token is listed by its digest and accepted there too, under the IP rules
and policies as here (see ``_tokens``). It also answers the 401 for a request with no credential, and for a credential this
gateway has just refused (it learns that from the ``X-Dataplane-Auth`` response header, never from the
snapshot, so a key created a moment ago is never refused by mistake). The wording comes from here.

MCP, A2A, AG-UI and ACP requests are served by the data plane too, so the snapshot names each protocol's
backend and the settings of the checks this gateway runs on their bodies (``app/tier1_mcp.py`` and its
siblings). The data plane forwards a body only when it is certain to pass them and hands every other
request here, so the refusals, and their wording, stay this gateway's.

Tenant usage is shared through the database: the data plane reports the requests it served per tenant,
they are added to the same counters this gateway's own requests feed, and every snapshot answer carries
the current totals, so a tenant's quota holds across both.

The endpoint exists only when ``DATAPLANE_TOKEN`` is set, requires it in ``X-Dataplane-Token`` and
belongs on an internal port: the data plane refuses ``/internal/`` from clients.
"""
from __future__ import annotations

import asyncio
import fnmatch
import functools
import hashlib
import ipaddress
import json
import os
import re
import time
from datetime import datetime, time as dtime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.db_models import (
    APIKeyModel,
    IPRuleModel,
    PolicyModel,
    TenantAPIKeyModel,
    TenantModel,
    TenantUsageModel,
)
from app.routing import graphql_base
from app.secure_compare import constant_time_equals

router = APIRouter(prefix="/internal/dataplane", include_in_schema=False)

# One snapshot per process at most this often, however many data planes poll.
CACHE_SECONDS = 0.25
_cache: Optional[tuple] = None
_lock = asyncio.Lock()


def _epoch(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def _versions() -> Dict[str, Any]:
    from app.versioning import VersionStatus, version_manager

    return {
        "default": version_manager.default_version,
        "strip_prefix": bool(version_manager.strip_prefix_on_proxy),
        "versions": [
            {
                "version": v.version,
                "native_ok": v.status == VersionStatus.ACTIVE
                and not v.backend_url
                and v.deprecated_date is None
                and v.sunset_date is None,
            }
            for v in version_manager.versions.values()
        ],
    }


def _own_paths(settings) -> Optional[List[str]]:
    """How the paths this gateway answers itself begin, when every other path with no protocol's
    prefix goes to the HTTP backend (``Router._get_backend_url``). ``None`` when they go elsewhere
    (no HTTP backend: the MCP one takes them), or when a route here could match any path."""
    if not settings.http_backend_url:
        return None
    from app.main import DOCS_PATHS, app, gateway_proxy

    # What the authentication middleware lets through or answers by how the path begins.
    own = {"/admin", "/internal/dataplane", "/oauth2", "/agents/", *DOCS_PATHS}
    for route in app.routes:
        if getattr(route, "endpoint", None) is gateway_proxy:
            continue
        literal = route.path.split("{", 1)[0]
        if literal in ("", "/"):
            return None
        own.add(literal)
    return sorted(own)


def _backends(settings) -> Dict[str, Optional[str]]:
    """Where the router sends each protocol the data plane can carry (``Router._get_backend_url``)."""
    return {
        "mcp": settings.mcp_backend_url or None,
        "a2a": settings.a2a_backend_url or None,
        "ag_ui": settings.ag_ui_backend_url or None,
        "acp": settings.acp_backend_url or None,
        "graphql": graphql_base(settings.graphql_backend_url) if settings.graphql_backend_url else None,
    }


def _websocket(settings) -> Optional[str]:
    """The WebSocket server ``/ws/...`` connections are relayed to, when the data plane may relay
    them itself: a ``ws://`` or ``wss://`` address, with the key read from the stock header or the
    query. A handshake goes through the key's allowed paths, the IP rules and the policies as a
    ``GET`` for its path does (``AuthMiddleware.authenticate``), and the data plane judges it so."""
    from app import main as gw

    url = getattr(gw.websocket_handler, "backend_url", None)
    if not isinstance(url, str) or not url.startswith(("ws://", "wss://")) or not url.isascii() or "?" in url or "#" in url:
        return None
    # Behind TLS, this gateway's WebSocket client verifies against the system's certificates and
    # SSL_CERT_FILE; the data plane only against that file. Without one, the two would not trust alike.
    if url.startswith("wss://") and (not os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR")):
        return None
    if settings.api_key_header != "X-API-Key":
        return None
    return url


_AGENT_SLUG = re.compile(r"[A-Za-z0-9._~-]+")
_AGENT_UPSTREAM = re.compile(r"https?://(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?")


def _agents() -> Dict[str, str]:
    """The agents of the agent edge the data plane may forward to itself: slug to upstream, for
    those with a plain ``http://`` or ``https://`` upstream. ``/agents/<slug>/...`` needs no gateway key (the agent
    authenticates its own callers), so the route is all there is to know. A slug that is not here
    is this gateway's to answer, registered or not."""
    from app import main as gw

    routes = gw.agent_edge_config.snapshot()["routes"]
    return {slug: base for slug, base in sorted(routes.items())
            if isinstance(slug, str) and isinstance(base, str) and _AGENT_SLUG.fullmatch(slug) and _AGENT_UPSTREAM.fullmatch(base)}


def _tokens(settings) -> list:
    """The OAuth2 access tokens this gateway accepts as credentials (``Authorization: Bearer <token>``),
    each by its SHA-256 digest, never the token itself. A token's caller goes through the IP
    rules and the policies as a key's does, with no key id and no tenant: the global rules apply,
    the ones scoped to a key or a tenant do not. It has no allowed paths, and its limits are the
    gateway's defaults. Tokens live in this process's memory, so only the ones this worker issued
    are known here."""
    from app.api_key_cache import hash_key
    from app.oauth2 import oauth2_manager

    listed = []
    for token, token_id in list(oauth2_manager._access_index.items()):
        stored = oauth2_manager.tokens.get(token_id)
        # One that could never arrive in a header as it is stored is never matched here either.
        if stored is None or not isinstance(token, str) or not token.isascii() or token != token.strip() or not token:
            continue
        if stored.is_expired():
            continue
        listed.append({
            "hash": hash_key(token),
            # What this gateway counts a refused request under when the credential is not a database key.
            "id": "non-database-key",
            "name": f"oauth2:{stored.provider}",
            "expires_at": _epoch(stored.expires_at),
            "rate_limit_per_minute": int(settings.rate_limit_per_minute or 0),
            "rate_limit_per_hour": int(settings.rate_limit_per_hour or 0),
        })
    return sorted(listed, key=lambda t: t["hash"])


def _transform_rules() -> Optional[list]:
    """The transformation rules that are switched on, in the order they are applied: for each, its
    path pattern and its methods (none means all), and what it does when the data plane can do the
    same (``_rule_actions``). A rule matches a request by the path the backend is asked for and,
    for the answer, by the request's path without its leading slash (``TransformationRule.matches``).
    The data plane applies the rules it was told the actions of and leaves a request any other
    rule applies to with this gateway.

    Each pattern is described in the form the data plane matches exactly as this gateway does
    (see ``_rule_pattern``). With a rule whose pattern has no such form, the answer is ``None``
    and every path stays here."""
    from app.transformers import transformation_manager

    out = []
    for rule in transformation_manager.list_rules():
        if not rule.enabled:
            continue
        described = _rule_pattern(rule.path_pattern)
        methods = [m.upper() for m in (rule.methods or []) if isinstance(m, str)]
        if described is None or len(methods) != len(rule.methods or []):
            return None
        try:
            actions = _rule_actions(rule)
        except Exception:  # noqa: BLE001 - a rule this code did not expect is applied here
            actions = None
        out.append({**described, "methods": sorted(set(methods)), "native": actions is not None, **(actions or {})})
    return out


_HEADER_NAME = re.compile(r"[A-Za-z0-9_-]+")
# No space at either end, where the two sides' HTTP libraries may differ.
_HEADER_VALUE = re.compile(r"(?:[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?)?")
# What a query is written with unchanged by every HTTP library on the way.
_QUERY_WORD = re.compile(r"[A-Za-z0-9._~-]*")
# Headers a rule's change to which is not the last word on them: the gateway, its HTTP client or
# its server reads or writes them too, each side in its own way.
_REQUEST_HEADERS_KEPT = frozenset((
    "host", "content-length", "transfer-encoding", "connection", "keep-alive", "upgrade", "te", "trailer", "expect",
    "proxy-authenticate", "proxy-authorization", "proxy-connection", "accept", "accept-encoding", "user-agent",
    "x-forwarded-for", "x-protocol", "traceparent", "x-request-id", "x-api-version", "accept-version",
))
_RESPONSE_HEADERS_KEPT = frozenset((
    "content-type", "content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive", "upgrade",
    "te", "trailer", "proxy-authenticate", "proxy-authorization", "set-cookie", "date", "server", "vary", "retry-after",
    "x-request-id", "x-response-time", "x-api-version", "traceparent", "warning", "sunset", "deprecation",
    "x-served-by", "x-dataplane-auth", "x-content-type-options",
))
_RESPONSE_PREFIXES_KEPT = ("access-control-", "x-ratelimit-", "x-tenant-", "x-anomaly-")
# The statuses a rule may turn into one another here: those that carry a body, as every server
# on the way writes them.
_STATUS_MAPPED = frozenset(range(200, 600)) - {204, 205, 304}


def _header_actions(cfg: Any, kept: frozenset, prefixes: tuple = ()) -> Optional[Dict[str, Any]]:
    """A rule's changes to headers (``_apply_header_ops``): renamed, then removed, then set."""

    def name(n: Any) -> bool:
        return (isinstance(n, str) and _HEADER_NAME.fullmatch(n) is not None
                and n.lower() not in kept and not n.lower().startswith(prefixes))

    def value(v: Any) -> bool:
        return isinstance(v, str) and _HEADER_VALUE.fullmatch(v) is not None

    rename, remove, to_set = cfg.headers_rename, cfg.headers_remove, cfg.headers_set
    if not (isinstance(rename, dict) and isinstance(remove, list) and isinstance(to_set, dict)):
        return None
    if not all(name(old) and name(new) for old, new in rename.items()):
        return None
    # Two names for one header to rename: the second finds it gone, which is an error there.
    if len({old.lower() for old in rename}) != len(rename):
        return None
    if not all(name(n) for n in remove) or not all(name(n) and value(v) for n, v in to_set.items()):
        return None
    return {"rename": [[old, new] for old, new in rename.items()], "remove": sorted({n.lower() for n in remove}),
            "set": [[n, v] for n, v in to_set.items()]}


def _plain_json(value: Any, depth: int = 0) -> bool:
    """Whether a value a rule sets is plain JSON: what it is once in a body is then what the
    data plane makes of its JSON text."""
    if value is None or type(value) is bool:
        return True
    if type(value) is int:
        return abs(value) < 10 ** 300
    if type(value) is float:
        return value == value and value not in (float("inf"), float("-inf"))
    if type(value) is str:
        return _encodes(value)
    if depth >= 16:
        return False
    if type(value) is list:
        return all(_plain_json(item, depth + 1) for item in value)
    if type(value) is dict:
        return all(type(k) is str and _encodes(k) and _plain_json(v, depth + 1) for k, v in value.items())
    return False


def _encodes(text: str) -> bool:
    """Whether text can be written as UTF-8 (half a surrogate pair cannot)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _body_actions(cfg: Any) -> Optional[Dict[str, Any]]:
    """A rule's changes to the fields of a request's body (``_apply_body_ops``): renamed, then
    removed, then set. A value to set goes as its JSON text."""
    rename, remove, to_set = cfg.body_rename, cfg.body_remove, cfg.body_set
    if not (isinstance(rename, dict) and isinstance(remove, list) and isinstance(to_set, dict)):
        return None
    paths = [*rename.keys(), *rename.values(), *remove, *to_set.keys()]
    if not all(type(path) is str and _encodes(path) for path in paths):
        return None
    if not all(_plain_json(value) for value in to_set.values()):
        return None
    fields = {"body_rename": [[old, new] for old, new in rename.items()], "body_remove": list(remove),
              "body_set": [[path, json.dumps(value)] for path, value in to_set.items()]}
    # Most rules change no body, and say nothing of it.
    return {name: steps for name, steps in fields.items() if steps}


# A path rewrite that is no expression at all: plain text (a dot or a dash may be escaped), with
# `^` in front or not, replaced by plain text.
_REWRITE_FROM = re.compile(r"\^?(?:[A-Za-z0-9/_~]|\\[.\-])+")
_REWRITE_TO = re.compile(r"[A-Za-z0-9/_.~-]*")
# In a replacement: a group by its number, in either of the spellings `re.sub` reads as one.
_REWRITE_GROUP = re.compile(r"\\([1-9])(?![0-9])|\\g<(0|[1-9][0-9]?)>")


def _rewrite_parts(replacement: str, groups: int) -> Optional[List[Any]]:
    """A replacement as ``re.sub`` reads it: plain text, and groups of the match by number.
    ``None`` for any other (another escape, a group the expression does not have)."""
    parts: List[Any] = []
    at = 0
    for found in list(_REWRITE_GROUP.finditer(replacement)) + [None]:
        text = replacement[at:found.start() if found else len(replacement)]
        if not _REWRITE_TO.fullmatch(text):
            return None
        if text:
            parts.append(text)
        if found:
            group = int(found.group(1) or found.group(2))
            if group > groups:
                return None
            parts.append(group)
            at = found.end()
    return parts


def _engines_differ(pattern: str) -> bool:
    """Whether an expression of the shared kind is one whose matches the two engines can still
    find differently, though both take it:

    * it repeats a group that can match nothing, as ``(a*)+`` does. ``re`` stops repeating at a
      turn that matches nothing, so where such a match ends, and what its groups held, can
      differ;
    * it has alternatives that each hold a repetition or a group, as ``a*(aa)|a*a`` does. The
      data plane's engine takes what alternatives begin with alike out in front of them
      (``a*(?:(aa)|a)``), and where that can match in more than one way, the alternative that
      wins is no longer the first that could.

    Which alternatives begin alike is the other engine's to say, so every set of alternatives
    that could is counted."""
    # For each group open, and the expression itself: where it starts, and for each of its
    # alternatives so far, whether it holds a repetition or a group.
    frames: List[List[Any]] = [[0, [False]]]
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":
            i += 1
            while i < n and pattern[i] != "]":
                i += 2 if pattern[i] == "\\" else 1
        elif c == "(":
            frames[-1][1][-1] = True
            if pattern.startswith("?:", i + 1):
                i += 2
            frames.append([i + 1, [False]])
        elif c == "|":
            frames[-1][1].append(False)
        elif c in "*+?{":
            frames[-1][1][-1] = True
        elif c == ")" and len(frames) > 1:
            start, alternatives = frames.pop()
            if len(alternatives) > 1 and all(alternatives):
                return True
            if pattern[i + 1:i + 2] in ("*", "+", "{"):
                try:
                    if re.fullmatch(pattern[start:i], "") is not None:
                        return True
                except re.error:
                    return True
        i += 1
    return len(frames) != 1 or (len(frames[0][1]) > 1 and all(frames[0][1]))


def _path_actions(cfg: Any) -> Optional[Dict[str, Any]]:
    """A rule's rewrite of the path the backend is asked for (``_apply_path_rewrite``):

    * plain text replaced by plain text, wherever it stands or at the start of the path only:
      ``{"path": {"from", "to", "start"}}``;
    * an expression that means the same to both engines and cannot match nothing, replaced by
      plain text and groups of the match: ``{"path": {"regex", "parts"}}``, each part text or a
      group's number. ``$`` alone, which adds to the end of the path, is described this way too.
      Not one whose matches the engines can find differently (``_engines_differ``).

    Nothing for a rule that rewrites nothing, which includes one whose expression does not
    compile; ``None`` for any other. An expression that can match nothing is replaced in more
    places by ``re.sub`` than by the data plane's engine."""
    rewrite = cfg.path_rewrite
    if not rewrite:
        return {}
    if type(rewrite) is not dict:
        return None
    pattern, replacement = rewrite.get("pattern"), rewrite.get("replacement", "")
    if not pattern:
        return {}
    if type(pattern) is not str or type(replacement) is not str:
        return None
    try:
        compiled = re.compile(pattern)
    except re.error:
        return {}
    except Exception:  # noqa: BLE001 - whatever else compiling raises is raised per request here
        return None
    plain = bool(_REWRITE_TO.fullmatch(replacement))
    if plain and (pattern == "^" or _REWRITE_FROM.fullmatch(pattern)):
        start = pattern.startswith("^")
        return {"path": {"from": pattern[start:].replace("\\", ""), "to": replacement, "start": start}}
    if not (pattern.isascii() and _SHARED_REGEX.fullmatch(pattern)):
        return None
    if pattern != "$" and compiled.fullmatch("") is not None:
        return None
    parts = _rewrite_parts(replacement, compiled.groups)
    if parts is None:
        return None
    if _engines_differ(pattern):
        return None
    return {"path": {"regex": pattern, "parts": parts}}


def _rule_actions(rule: Any) -> Optional[Dict[str, Any]]:
    """What a transformation rule does, when the data plane can do exactly the same: change
    headers, query parameters and the fields of a JSON body of the request, rewrite its path, and
    change headers, the status and the fields of a JSON body of the answer. ``None`` for a rule
    that does anything else (fills in a template, rewrites the path by an expression the two
    engines read differently, or is code), or that names a header, a value or a status the two
    sides might treat differently. Such a rule is applied here: the data plane hands over the
    requests it applies to."""
    if rule.condition is not None or rule.transform_request is not None or rule.transform_response is not None:
        return None
    out: Dict[str, Any] = {"request": None, "response": None}
    for side in ("request", "response"):
        cfg = rule.request_config if side == "request" else rule.response_config
        if cfg is None:
            continue
        # A template is filled in by an engine of this side's.
        if cfg.template:
            return None
        if side == "request":
            actions = _header_actions(cfg, _REQUEST_HEADERS_KEPT)
            fields, path = _body_actions(cfg), _path_actions(cfg)
            remove, to_set = cfg.query_remove, cfg.query_set
            if actions is None or fields is None or path is None or not (isinstance(remove, list) and isinstance(to_set, dict)):
                return None
            actions.update(fields)
            actions.update(path)
            if not all(isinstance(q, str) for q in remove):
                return None
            if not all(isinstance(k, str) and k and _QUERY_WORD.fullmatch(k) and isinstance(v, str) and _QUERY_WORD.fullmatch(v)
                       for k, v in to_set.items()):
                return None
            actions["query_remove"] = list(remove)
            actions["query_set"] = [[k, v] for k, v in to_set.items()]
        else:
            actions = _header_actions(cfg, _RESPONSE_HEADERS_KEPT, _RESPONSE_PREFIXES_KEPT)
            fields, statuses = _body_actions(cfg), cfg.status_map
            if actions is None or fields is None or not isinstance(statuses, dict):
                return None
            # An answer whose body the data plane is not certain of, it shows to this gateway
            # (`shape_answer`), which then applies the rule.
            actions.update(fields)
            mapped = []
            for was, becomes in statuses.items():
                if not (isinstance(was, str) and type(becomes) is int and becomes in _STATUS_MAPPED):
                    return None
                # A key that is not a status as Python writes one never matches anything.
                if was.isascii() and was.isdigit() and str(int(was)) == was:
                    if int(was) not in _STATUS_MAPPED:
                        return None
                    mapped.append([int(was), becomes])
            actions["status"] = mapped
        out[side] = actions
    return out


# A regular expression that means the same to Python's `re` and to the data plane's engine: plain
# characters, `.`, a few escapes, classes of plain characters and ranges, groups, alternatives and
# repetition, with `^` only in front and `$` only at the end. Paths are ASCII without line ends,
# where `.`, `\d`, `\w` and `$` agree. Anything else (a look-around, a back-reference, a flag) is
# this gateway's to match.
_SHARED_REGEX = re.compile(r"""\^?(?:
      [A-Za-z0-9/_~:@!,;=&'\ -] | \\[./\-_~dw] | \. | (?:[*+?]|\{\d+(?:,\d*)?\})\??(?![*+?{]) | \((?:\?:|(?!\?)) | \) | \|
    | \[\^?(?: [A-Za-z0-9]-[A-Za-z0-9] | [A-Za-z0-9/_.~:@!,;=&'\ ] | \\[.\-/\]dw] )+\]
    )*\$?""", re.X)


def _rule_pattern(pattern: Any) -> Optional[Dict[str, Any]]:
    """A transformation rule's path pattern, as ``TransformationRule.matches`` reads it:

    * ``*`` and ``**`` match every path: ``{"all": True}``;
    * one that starts with ``^`` or ends with ``$`` is a regular expression, matched from the start
      of the path: ``{"regex": ...}`` when it means the same to both engines, and, when it does not
      compile here, the text the path must contain instead: ``{"contains": ...}``;
    * any other is a glob. Without character classes: ``{"glob": ...}``. With them:
      ``{"parts": [...]}``, each part ``*``, ``?``, ``{"lit": text}`` or ``{"set": characters}``,
      where the characters are the ASCII ones Python's own ``fnmatch`` lets the class match."""
    if not isinstance(pattern, str) or not pattern.isascii():
        return None
    if pattern in ("*", "**"):
        return {"all": True}
    if pattern.startswith("^") or pattern.endswith("$"):
        try:
            re.compile(pattern)
        except re.error:
            return {"contains": pattern}
        except Exception:  # noqa: BLE001 - whatever else compiling raises is raised per request here
            return None
        return {"regex": pattern} if _SHARED_REGEX.fullmatch(pattern) else None
    if "[" not in pattern:
        return {"glob": pattern}
    parts: List[Any] = []
    literal, i, n = "", 0, len(pattern)
    while i < n:
        c = pattern[i]
        i += 1
        if c == "[":
            # Where the class ends, found as `fnmatch.translate` finds it.
            j = i
            if j < n and pattern[j] == "!":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j < n:
                one = re.compile(fnmatch.translate(pattern[i - 1:j + 1]))
                if literal:
                    parts.append({"lit": literal})
                parts.append({"set": "".join(ch for ch in map(chr, range(128)) if one.match(ch))})
                literal, i = "", j + 1
                continue
        if c in "*?":
            if literal:
                parts.append({"lit": literal})
            parts.append(c)
            literal = ""
        else:
            literal += c
    if literal:
        parts.append({"lit": literal})
    return {"parts": parts}


_PAGE_SLOTS = {"code": "@@code@@", "message": "@@message@@", "hint": "@@hint@@", "request_id": "@@request-id@@"}
_PAGE_RETRY_AFTER = 987654321
_PAGE_STATUSES = (401, 403, 429, 502, 503)


def _error_pages() -> Optional[Dict[str, str]]:
    """The pages a browser is shown for the errors the data plane answers itself, one for each
    status and for each shape the page takes (with or without a hint; for a 429, with or without
    a countdown). Where the error's own words go there is a marker, which the data plane replaces
    with the words, escaped as HTML; the seconds of a countdown stand as ``987654321``. Keys are
    ``<status>:<hint><countdown>``, as in ``429:11``. ``None`` if a page does not come out with
    its markers where they are expected, and browsers then stay with this gateway."""
    from app import main as gw

    pages: Dict[str, str] = {}
    try:
        for status in _PAGE_STATUSES:
            for hint in (False, True):
                for countdown in ((False, True) if status == 429 else (False,)):
                    details: Dict[str, Any] = {}
                    if hint:
                        details["hint"] = _PAGE_SLOTS["hint"]
                    if countdown:
                        details["retry_after"] = _PAGE_RETRY_AFTER
                    page = gw._error_html(status, {
                        "error": _PAGE_SLOTS["code"], "message": _PAGE_SLOTS["message"],
                        "request_id": _PAGE_SLOTS["request_id"], "details": details,
                    })
                    expected = {"code": 1, "message": 1, "request_id": 1, "hint": int(hint)}
                    if any(page.count(_PAGE_SLOTS[slot]) != n for slot, n in expected.items()):
                        return None
                    if page.count(str(_PAGE_RETRY_AFTER)) != (2 if countdown else 0):
                        return None
                    pages[f"{status}:{int(hint)}{int(countdown)}"] = page
    except Exception:  # noqa: BLE001 - pages this code cannot render: browsers stay with this gateway
        return None
    return pages


def _cross_origin() -> Optional[Dict[str, Any]]:
    """How this gateway answers a request from a web page in another origin (one with an
    ``Origin`` header): the origins it names back, and whether it says credentials are allowed.
    These are the options its CORS middleware was started with. ``None`` when they include
    something the data plane does not reproduce, and such requests then stay here."""
    from app import main as gw

    options = dict(gw._cors_options)
    origins = options.pop("allow_origins", None)
    credentials = options.pop("allow_credentials", None)
    methods = options.pop("allow_methods", None)  # these two only matter to a preflight
    headers = options.pop("allow_headers", None)
    plain = isinstance(origins, list) and all(isinstance(o, str) and o.isascii() for o in origins) and isinstance(credentials, bool)
    if options or not plain:
        return None
    return {"any_origin": "*" in origins, "origins": sorted(set(origins) - {"*"}), "credentials": credentials,
            "preflight": _preflight(origins, credentials, methods, headers)}


def _preflight(origins, credentials, methods, headers) -> Optional[Dict[str, Any]]:
    """How the CORS middleware answers a preflight, read off a middleware set up with the same
    options, so that the data plane's answers are its answers. ``None`` when that cannot be put
    in plain words (the preflight then stays here)."""
    from starlette.middleware.cors import CORSMiddleware

    try:
        cors = CORSMiddleware(None, allow_origins=origins, allow_credentials=credentials,
                              allow_methods=methods, allow_headers=headers)
        said = [[str(name), str(value)] for name, value in cors.preflight_headers.items()]
        names = [str(m) for m in cors.allow_methods] + [str(h) for h in cors.allow_headers]
        if not all(text.isascii() for pair in said for text in pair) or not all(n.isascii() for n in names):
            return None
        return {
            "headers": said,
            "explicit_origin": bool(cors.preflight_explicit_allow_origin),
            "methods": [str(m) for m in cors.allow_methods],
            "all_headers": bool(cors.allow_all_headers),
            "allow_headers": [str(h) for h in cors.allow_headers],
        }
    except Exception:  # noqa: BLE001 - options this code did not expect: answer preflights here
        return None


_ROUTE_REWRITE = re.compile(r"/[A-Za-z0-9._~/-]*")


def _dynamic_routes(settings) -> list:
    """The routes registered through ``/admin/routes`` that are in force, in the order this gateway
    tries them (``route_registry.match``): exact paths, then wildcards from the longest prefix down.
    A matching route decides where a request goes, under which protocol name, to which path and
    with which timeout. One the data plane cannot follow exactly has no ``backend`` here, and
    requests it matches are this gateway's. With dynamic routes switched off there are none."""
    if not getattr(settings, "dynamic_routes_enabled", False):
        return []
    from app import route_registry

    out = []
    for match_path, is_wildcard, _length, cfg in route_registry._ROUTES:
        backend = (cfg.backend_url or "").rstrip("/")
        rewrite = cfg.rewrite_path or None
        timeout = cfg.timeout
        follows = (
            _AGENT_UPSTREAM.fullmatch(backend) is not None
            and (rewrite is None or _ROUTE_REWRITE.fullmatch(rewrite) is not None)
            and isinstance(timeout, int) and not isinstance(timeout, bool) and 0 < timeout <= 86_400
            and cfg.protocol.value.isascii()
        )
        out.append({
            "path": match_path,
            "wildcard": bool(is_wildcard),
            "backend": backend if follows else None,
            "protocol": cfg.protocol.value,
            "timeout_seconds": timeout if follows else None,
            "rewrite": rewrite if follows else None,
        })
    return out


_REQUEST_ID_GOES_HERE = "@@request-id@@"


def _agent_failure() -> Optional[Dict[str, Any]]:
    """How this gateway answers when an agent's connection fails before it answers: the message
    of its 500, and the page a browser is shown, cut in two where the request id goes (the data
    plane writes the id in, escaped as HTML). ``None`` if the page cannot be cut that way."""
    from app import main as gw

    try:
        page = gw._error_html(500, gw.internal_error_data(_REQUEST_ID_GOES_HERE))
    except Exception:  # noqa: BLE001 - a page this code cannot render: the data plane leaves agents to us
        return None
    parts = page.split(_REQUEST_ID_GOES_HERE)
    if len(parts) != 2:
        return None
    return {"message_json": _dumps(gw.INTERNAL_ERROR_MESSAGE), "page": parts}


@functools.lru_cache(maxsize=1)
def _greeter_stubs() -> bool:
    """Whether the bridge under ``/grpc`` finds the sample service's generated code, looked for
    exactly as the bridge looks for it. Asked once: what is installed does not change while running."""
    import os
    import sys

    from app.protocols import grpc_handler

    for proto_path in (os.path.join(os.path.dirname(grpc_handler.__file__), "../../samples/backends/grpc"),
                       "/app/samples/backends/grpc", "samples/backends/grpc"):
        if os.path.exists(proto_path):
            sys.path.insert(0, proto_path)
            try:
                import greeter_pb2  # noqa: F401
                import greeter_pb2_grpc  # noqa: F401
                return True
            except ImportError:
                continue
    return False


@functools.lru_cache(maxsize=1)
def _grpc_reflection() -> bool:
    """Whether the reflection package the bridge asks for before any unary call can be loaded."""
    try:
        import grpc_reflection.v1alpha.reflection  # noqa: F401
        from grpc_reflection.v1alpha import reflection_pb2  # noqa: F401
        return True
    except ImportError:
        return False


def _grpc_bridge() -> Optional[Dict[str, Any]]:
    """How this gateway serves JSON posted under ``/grpc``, for the data plane to serve it the same
    way: the server its handler calls (only a plain ``host:port``, which is all its channel takes),
    whether the reflection package is installed (without it no unary call is made) and whether the
    sample service's generated code is. ``None``: the data plane leaves the bridge to this gateway."""
    from app import main as gw

    target = getattr(gw.grpc_handler, "backend_url", None)
    if not isinstance(target, str) or not _GRPC_TARGET.fullmatch(target):
        return None
    try:
        return {"target": f"http://{target}", "reflection": _grpc_reflection(), "stubs": _greeter_stubs()}
    except Exception:
        return None


_GRPC_TARGET = re.compile(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\]):[0-9]{1,5}")


def _grpc(settings) -> Optional[str]:
    """The gRPC server the data plane relays calls to: ``GRPC_BACKEND_URL`` when it is a plain
    ``host:port`` (how a gRPC address is usually written), as an ``http://`` address. gRPC calls reach
    only the data plane, which speaks HTTP/2; this gateway cannot carry them."""
    target = (settings.grpc_backend_url or "").strip()
    scheme = "http://"
    for named in ("http://", "https://"):
        if target.startswith(named):
            scheme, target = named, target[len(named):].rstrip("/")
    return f"{scheme}{target}" if _GRPC_TARGET.fullmatch(target) else None


_MQTT_HOST = re.compile(r"[A-Za-z0-9.-]+")


def _mqtt() -> Optional[Dict[str, Any]]:
    """The MQTT broker ``POST /mqtt/publish`` publishes to, while this gateway's own client is
    connected to it: the data plane then keeps a connection of its own, and publishes and
    subscribes (``POST /mqtt/subscribe``) there too.
    Behind TLS (``mqtts://``), this gateway's client verifies the broker against the system's
    certificates and SSL_CERT_FILE; the data plane only against that file. Without one, the two
    would not trust alike, and the broker is left to this gateway."""
    from app import main as gw

    handler = gw.mqtt_handler
    url = getattr(handler, "broker_url", None)
    if handler is None or not handler.connected or not isinstance(url, str) or not url.startswith(("mqtt://", "mqtts://")):
        return None
    if url.startswith("mqtts://") and (not os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR")):
        return None
    try:
        host, port, tls = handler._parse_broker_url()
    except Exception:
        return None
    if not _MQTT_HOST.fullmatch(host) or not 0 < port < 65536:
        return None
    return {"host": host, "port": port, "tls": bool(tls)}


def _boundary(settings) -> Dict[str, Any]:
    """The settings of the body checks, in the form the data plane applies them. Names are lower-cased
    here because that is how the checks compare them."""
    from app.tier1_registry import tier1_registry

    try:
        # Only a tool with a non-empty schema has its arguments validated.
        index = tier1_registry.tool_input_schema_index()
        schema_tools = sorted(name for name, schema in index.items() if schema)
        schemas_known = True
    except Exception:  # noqa: BLE001 - a registry this code cannot read: let Python judge MCP bodies
        index, schema_tools, schemas_known = {}, [], False
    # The schemas themselves, so the data plane can forward a call whose arguments are certain to
    # pass. One it does not fully understand, or that is not sent, keeps the tool's calls here.
    tool_schemas = {}
    for name in schema_tools:
        try:
            tool_schemas[name] = json.loads(json.dumps(index[name], allow_nan=False))
        except Exception:  # noqa: BLE001 - not plain JSON: this gateway judges that tool's calls
            continue
    return {
        "mcp": {
            "enabled": bool(settings.mcp_boundary_enabled),
            "max_body_bytes": max(0, int(settings.mcp_boundary_max_body_bytes or 0)),
            "blocked_methods": sorted({str(m).lower() for m in (settings.mcp_boundary_blocked_methods or [])}),
            "allowed_methods": sorted({str(m).lower() for m in (settings.mcp_boundary_allowed_methods or [])}),
            "block_injection": (settings.mcp_boundary_prompt_injection_action or "block").lower() != "audit",
            # The data plane carries the default patterns only; with others, MCP bodies are judged here.
            "default_patterns": schemas_known and not list(settings.mcp_boundary_injection_patterns or []),
            "schema_tools": schema_tools,
            "tool_schemas": tool_schemas,
        },
        "a2a": {
            "enabled": bool(settings.a2a_boundary_enabled),
            "enforce_send_message_shape": bool(settings.a2a_enforce_send_message_shape),
        },
        "acp": {"enabled": bool(getattr(settings, "acp_boundary_enabled", True))},
        "ag_ui": {"enabled": bool(getattr(settings, "ag_ui_boundary_enabled", True))},
    }


def metric_protocol(path: str) -> str:
    """The protocol label this gateway's request metrics give a path (the logging middleware's rule)."""
    for part, name in (("/mcp", "mcp"), ("/a2a", "a2a"), ("/agui", "ag_ui"), ("/acp", "acp"), ("/mqtt", "mqtt"),
                       ("/ws", "websocket"), ("/websocket", "websocket"), ("/grpc", "grpc")):
        if part in path:
            return name
    return "http"


def log_protocol(path: str) -> str:
    """The protocol this gateway's request log gives a path: as the metrics, and GraphQL by name."""
    name = metric_protocol(path)
    return "graphql" if name == "http" and "/graphql" in path else name


def _refusals(auth_plugins: bool) -> Optional[Dict[str, Any]]:
    """The two 401s for a request without a usable credential, as this gateway words them. ``None``
    when a plugin can authenticate a request by other means: then only this gateway can tell."""
    from app.middleware import auth

    if auth_plugins:
        return None
    return {
        "missing_key": {"message_json": _dumps(auth.MISSING_KEY_MESSAGE), "details_json": _dumps(auth.missing_key_details())},
        "invalid_key": {"message_json": _dumps(auth.INVALID_KEY_MESSAGE), "details_json": _dumps(auth.invalid_key_details())},
    }


class _NotReproducible(Exception):
    """Something the data plane cannot evaluate exactly as this gateway does."""


# Paths this gateway answers before its health route is reached, or under which it is never looked for.
_NOT_A_HEALTH_PATH = ("/admin", "/internal", "/oauth2", "/agents/")


def _ops(settings) -> Dict[str, Any]:
    """What the data plane needs to answer the health path itself: the path and the version it
    reports. No path when this gateway would not answer it with its health route."""
    from app import main as gw

    path = settings.health_check_path
    plain = isinstance(path, str) and path.startswith("/") and path.isascii() and "%" not in path and "?" not in path
    if not plain or path in gw.DOCS_PATHS or path.startswith(_NOT_A_HEALTH_PATH):
        path = None
    return {"health_path": path, "version_json": _dumps(settings.app_version)}


async def readiness_note() -> Dict[str, Any]:
    """This gateway's answer to ``/ready`` right now, for the data plane to give in its place until
    the next snapshot answer. Sent with every answer; never part of the revision."""
    from app import main as gw

    status_code, body = await gw.readiness()
    return {"status": status_code, "body_json": _dumps(body)}


def _dumps(value: Any) -> str:
    """JSON exactly as this gateway's error responses render it, for the data plane to embed as is."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _net(value: Any) -> Optional[str]:
    """A CIDR as ``ipaddress`` reads it, in canonical text; None when it does not parse (and so never matches)."""
    if not isinstance(value, str):
        raise _NotReproducible("a CIDR that is not text")
    try:
        text = str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        return None
    if "%" in text:
        raise _NotReproducible("a scoped IPv6 network")
    return text


def _trusted_proxies(settings) -> List[str]:
    out = []
    for entry in getattr(settings, "trusted_proxies", None) or ():
        if not isinstance(entry, str):
            raise _NotReproducible("a trusted proxy that is not text")
        net = _net(entry.strip()) if entry.strip() else None
        if net:
            out.append(net)
    return out


def _ip_rules(rules) -> List[Dict[str, Any]]:
    """Enabled IP rules in the order ``IPRuleEngine`` indexes them, without those it ignores."""
    out = []
    for r in rules:
        if r.scope not in ("global", "tenant", "api_key") or r.action not in ("allow", "deny"):
            continue
        if r.scope != "global" and r.scope_id is None:
            continue
        out.append({
            "scope": r.scope,
            "scope_id": r.scope_id if r.scope != "global" else None,
            "allow": r.action == "allow",
            "net": _net(r.cidr),
            # What a refusal by this rule says, after the client address.
            "details_json": _dumps({"rule_id": r.id, "scope": r.scope}),
        })
    return out


def _minutes(text: Any) -> int:
    h, m = text.split(":")
    t = dtime(int(h), int(m))
    return t.hour * 60 + t.minute


def _policy(p) -> Optional[Dict[str, Any]]:
    """One policy as ``PolicyConditions.matches`` and ``PolicyEngine.evaluate`` read it; None when it can never match."""
    cond = p.conditions or {}
    action = p.action or {"type": "allow"}
    if not isinstance(cond, dict) or not isinstance(action, dict):
        raise _NotReproducible("a policy that is not an object")
    if cond.get("headers"):
        raise _NotReproducible("a policy that matches on headers")
    path = cond.get("path") or None
    if path is not None and (not isinstance(path, str) or "[" in path):
        raise _NotReproducible("a path pattern with a character class")
    lists = {}
    for name in ("methods", "tenants", "roles", "cidrs"):
        value = cond.get(name, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise _NotReproducible(f"policy {name} that is not a list of text")
        lists[name] = value
    # API-key requests carry no role, so a policy that requires one never matches them.
    if lists["roles"] and "" not in lists["roles"]:
        return None
    window = None
    tod = cond.get("time_of_day")
    if tod:
        if not isinstance(tod, dict):
            raise _NotReproducible("a time window that is not an object")
        start, end = tod.get("start"), tod.get("end")
        if start and end:
            try:
                window = [_minutes(start), _minutes(end)]
            except (ValueError, AttributeError):
                window = None  # unreadable times do not restrict, as in _time_in_window
    kind = action.get("type") or "allow"
    if not isinstance(kind, str):
        raise _NotReproducible("a policy action that is not text")
    reason = action.get("reason") or f"denied by policy '{p.name}'"
    if not isinstance(reason, str):
        raise _NotReproducible("a policy reason that is not text")
    return {
        "path": path,
        "methods": [m.upper() for m in lists["methods"]],
        "tenants": lists["tenants"],
        # null: any address. A list: the address must be in one of them (unreadable CIDRs never match).
        "cidrs": [n for n in map(_net, lists["cidrs"]) if n] if lists["cidrs"] else None,
        "time": window,
        "deny": kind.lower() == "deny",
        "message_json": _dumps(reason),
        "details_json": _dumps({"policy_id": p.id}),
    }


def _scope(patterns: Any) -> Optional[Dict[str, Any]]:
    """A key's ``allowed_paths`` as ``path_is_allowed`` reads them: ``["all"]``, ``["prefix", text]`` (the
    normalised path starts with it) or ``["under", base]`` (the path is ``base`` or below it)."""
    from app.middleware.auth import _normalise_scope_path

    if not patterns:
        return None
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise _NotReproducible("allowed_paths that are not a list of text")
    rules: List[list] = []
    for raw in patterns:
        pattern = raw.strip()
        if not pattern:
            continue
        if pattern in ("*", "/", "/*"):
            rules = [["all"]]
            break
        if pattern.endswith("/*"):
            pattern = pattern[:-2]
        elif pattern.endswith("*"):
            prefix = _normalise_scope_path(pattern[:-1]) or ""
            if pattern[:-1].endswith("/") and prefix != "/":
                prefix += "/"
            rules.append(["prefix", prefix])
            continue
        base = _normalise_scope_path(pattern)
        if base is not None:
            rules.append(["under", base])
    return {"rules": rules, "patterns_json": _dumps(patterns)}


def _tenant(t) -> Dict[str, Any]:
    quota = t.quota or {}
    if not isinstance(quota, dict):
        raise _NotReproducible("a quota that is not an object")
    limits = {}
    for window in ("minute", "hour"):
        value = quota.get(f"requests_per_{window}")
        if value is None:
            limits[window] = None
        elif isinstance(value, bool) or not isinstance(value, int):
            raise _NotReproducible("a quota that is not a whole number")
        else:
            limits[window] = value if value > 0 else None
    try:
        details = _dumps({"tenant_id": t.tenant_id, "quota": quota})
    except (TypeError, ValueError):
        raise _NotReproducible("a quota that does not render as JSON")
    # What a refusal over this tenant's quota says, after the limit that was hit.
    return {"minute": limits["minute"], "hour": limits["hour"], "details_json": details}


async def build_snapshot(db: AsyncSession) -> Dict[str, Any]:
    settings = get_settings()
    keys = (await db.execute(select(APIKeyModel).where(APIKeyModel.is_active.is_(True)))).scalars().all()
    links = (await db.execute(
        select(TenantAPIKeyModel.tenant_id, TenantAPIKeyModel.api_key_id, TenantAPIKeyModel.api_key_hash)
    )).all()
    tenant_rows = {t.tenant_id: t for t in (await db.execute(select(TenantModel))).scalars().all()} if links else {}
    # The orders the engines evaluate in (IPRuleEngine.reload, PolicyEngine.reload).
    rules = (await db.execute(
        select(IPRuleModel).where(IPRuleModel.enabled.is_(True)).order_by(IPRuleModel.created_at.asc())
    )).scalars().all()
    policy_rows = (await db.execute(
        select(PolicyModel).where(PolicyModel.enabled.is_(True))
        .order_by(PolicyModel.priority.asc(), PolicyModel.created_at.asc())
    )).scalars().all()

    from app.plugins import PluginType, plugin_registry
    from app.transformers import transformation_manager

    # Rules and policies apply to every key. If any of them cannot be reproduced, every key's requests
    # go to this gateway, as they all did before the data plane evaluated these itself.
    try:
        trusted = _trusted_proxies(settings)
        ip_rules = _ip_rules(rules)
        policies = [c for c in map(_policy, policy_rows) if c is not None]
        global_gate = False
    except _NotReproducible:
        trusted, ip_rules, policies, global_gate = [], [], [], True
    except Exception:  # noqa: BLE001 - a rule this code did not expect: let Python decide
        trusted, ip_rules, policies, global_gate = [], [], [], True

    # Plugins that can hook, transform or take over a request (an auth provider only adds a way in).
    # The stock request-id plugin only stamps X-Request-ID, which the data plane does itself, so it
    # does not count; the snapshot says whether it is on so the data plane behaves the same either way.
    active = [p for p in plugin_registry.list() if p.enabled] if getattr(settings, "plugins_enabled", False) else []
    inject_request_id = any(p.name == "request-id" and p.plugin_type == PluginType.TRANSFORM for p in active)
    request_plugins = any(
        p.plugin_type != PluginType.AUTH_PROVIDER and p.name != "request-id" for p in active
    )
    dynamic_routes = _dynamic_routes(settings)
    transform_rules = _transform_rules()
    # The data plane leaves a request a rule applies to here; only rules it cannot match keep every path.
    all_paths_via_python = bool(request_plugins) or transform_rules is None
    auth_plugins = any(p.plugin_type == PluginType.AUTH_PROVIDER for p in active)
    # The health and metrics endpoints are this gateway's own and need no key, wherever they are
    # configured to live: if that is under a prefix the data plane serves, it must leave them alone.
    own_paths = sorted({
        p for p in (settings.health_check_path, settings.metrics_path)
        if p and p.startswith(("/api", "/mcp", "/a2a", "/agui", "/acp"))
    })

    # This gateway finds a key's tenant by the key's digest (TenantRepository.get_tenant_for_api_key).
    by_hash: Dict[str, list] = {}
    for tenant_id, _key_id, key_hash in links:
        by_hash.setdefault(key_hash, []).append(tenant_id)
    linked_ids = {key_id for _tenant_id, key_id, _hash in links}

    tenants: Dict[str, Any] = {}
    unusable_tenants = set()
    entries = []
    for k in sorted(keys, key=lambda k: k.key_hash):
        native_ok = not global_gate
        tenant_id = None
        scope = None
        owners = by_hash.get(k.key_hash, [])
        if len(owners) > 1 or (not owners and k.id in linked_ids):
            native_ok = False  # an association this gateway itself resolves oddly: let it
        elif owners:
            row = tenant_rows.get(owners[0])
            # An inactive tenant's keys work as keys without a tenant.
            if row is not None and row.is_active:
                tenant_id = row.tenant_id
                if tenant_id not in tenants and tenant_id not in unusable_tenants:
                    try:
                        tenants[tenant_id] = _tenant(row)
                    except _NotReproducible:
                        unusable_tenants.add(tenant_id)
                if tenant_id in unusable_tenants:
                    native_ok, tenant_id = False, None
        try:
            scope = _scope(k.allowed_paths or [])
        except _NotReproducible:
            native_ok = False
        entry = {
            "hash": k.key_hash,
            "id": k.id,
            "is_active": True,
            "expires_at": _epoch(k.expires_at),
            "rate_limit_per_minute": int(k.rate_limit_per_minute or 0),
            "rate_limit_per_hour": int(k.rate_limit_per_hour or 0),
            "native_ok": native_ok,
        }
        if native_ok and tenant_id is not None:
            entry["tenant"] = tenant_id
        if native_ok and scope is not None:
            entry["allowed_paths"] = scope
        entries.append(entry)
    snap: Dict[str, Any] = {
        "keys": entries,
        "tenants": tenants,
        "trusted_proxies": trusted,
        "ip_rules": ip_rules,
        "policies": policies,
        "fallthrough_prefixes": ["/"] if all_paths_via_python else own_paths,
        "rate_limit_enabled": bool(settings.rate_limit_enabled),
        "inject_request_id": inject_request_id,
        "circuit_breaker": {
            "enabled": bool(settings.circuit_breaker_enabled),
            "failure_threshold": int(settings.circuit_breaker_failure_threshold),
            "timeout_seconds": int(settings.circuit_breaker_timeout),
        },
        "upstream_timeout_seconds": float(settings.default_timeout),
        "api_versions": _versions(),
        "backends": _backends(settings),
        "own_paths": None if all_paths_via_python else _own_paths(settings),
        "boundary": _boundary(settings),
        # Where a key is read from (the Authorization header is always the second place), and how a
        # request without a usable one is refused.
        "key_header": settings.api_key_header,
        "refusals": _refusals(auth_plugins),
        "ops": _ops(settings),
        "websocket": _websocket(settings),
        "grpc": _grpc(settings),
        "grpc_bridge": _grpc_bridge(),
        "mqtt": _mqtt(),
        "agents": _agents(),
        "agent_failure": _agent_failure(),
        "dynamic_routes": dynamic_routes,
        "cross_origin": _cross_origin(),
        "error_pages": _error_pages(),
        "transform_rules": transform_rules,
        "tokens": _tokens(settings),
    }
    snap["revision"] = hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()
    return snap


async def tenant_usage(db: AsyncSession, tenant_ids) -> Dict[str, Any]:
    """Requests each tenant has used in the current minute and hour, from the counters every request
    feeds, whichever plane served it. Sent with every snapshot answer; never part of the revision."""
    from app.repositories.tenant_repo import TenantRepository

    minute = TenantRepository._window_bounds("minute")
    hour = TenantRepository._window_bounds("hour")
    used: Dict[str, list] = {}
    if tenant_ids:
        rows = await db.execute(
            select(TenantUsageModel.tenant_id, TenantUsageModel.window, func.sum(TenantUsageModel.request_count))
            .where(or_(
                and_(TenantUsageModel.window == "minute", TenantUsageModel.window_start == minute),
                and_(TenantUsageModel.window == "hour", TenantUsageModel.window_start == hour),
            ))
            .group_by(TenantUsageModel.tenant_id, TenantUsageModel.window)
        )
        for tenant_id, window, total in rows.all():
            if tenant_id in tenant_ids:
                used.setdefault(tenant_id, [0, 0])[0 if window == "minute" else 1] = int(total or 0)
    return {"minute_start": _epoch(minute), "hour_start": _epoch(hour), "used": used}


async def current_snapshot(db: AsyncSession) -> Dict[str, Any]:
    global _cache
    async with _lock:
        now = time.monotonic()
        if _cache is not None and now - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        snap = await build_snapshot(db)
        _cache = (now, snap)
        return snap


def reset_cache() -> None:
    global _cache
    _cache = None


def _authorize(token: Optional[str]) -> None:
    expected = get_settings().dataplane_token
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    if not token or not constant_time_equals(token, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


# --- what the data plane served ----------------------------------------------------------------

# Upper bounds of gateway_request_duration_seconds; the data plane buckets with the same ones.
_BOUNDS = (0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0)


class ReportedAggregate(BaseModel):
    method: str
    path: str  # one real path from the group; the label is derived here
    status: int
    rejected: bool = False
    client_id: Optional[str] = None
    tenant: Optional[str] = None
    # For a 5xx the data plane produced itself: the error code of its body, and for a failed call
    # to the backend the kind of failure, named as this gateway's HTTP client names it.
    error_code: Optional[str] = None
    error_type: Optional[str] = None
    buckets: list = Field(default_factory=list)  # [[count, seconds]] per bucket, then +Inf


class ReportedRow(BaseModel):
    request_id: str
    method: str
    path: str
    client_ip: str = "0.0.0.0"
    api_key_id: Optional[str] = None
    tenant_id: Optional[str] = None
    status: int
    duration_ms: float
    user_agent: Optional[str] = None
    error: Optional[str] = None


class ReportedKeyUse(BaseModel):
    id: str
    at: int  # epoch seconds of the key's latest use


class ReportedEvent(BaseModel):
    """Something this gateway announces by webhook and alert when it happens here: the circuit breaker
    opening or closing (``circuit_breaker``) or a key going over its limit (``rate_limit_exceeded``)."""

    type: str
    # circuit_breaker
    state: Optional[str] = None
    previous_state: Optional[str] = None
    backend_url: Optional[str] = None
    failure_count: int = 0
    failure_threshold: int = 0
    opened_at: float = 0.0
    timeout_seconds: int = 0
    # rate_limit_exceeded
    api_key_id: Optional[str] = None
    api_key_name: Optional[str] = None  # sent for a credential that is not a database key (an OAuth2 token)
    client_ip: Optional[str] = None
    limit: int = 0
    window: Optional[str] = None
    path: Optional[str] = None
    count: int = 1  # how many refused requests this one event stands for


class ReportedTenantUsage(BaseModel):
    """Requests served for one tenant, admitted in one minute (epoch seconds of its start)."""

    tenant_id: str
    minute: int
    requests: int = 0
    errors: int = 0
    bytes_out: int = 0


class DataplaneReport(BaseModel):
    tenant_usage: list[ReportedTenantUsage] = Field(default_factory=list, max_length=5000)
    events: list[ReportedEvent] = Field(default_factory=list, max_length=2000)
    keys_used: list[ReportedKeyUse] = Field(default_factory=list, max_length=5000)
    aggregates: list[ReportedAggregate] = Field(default_factory=list, max_length=5000)
    rows: list[ReportedRow] = Field(default_factory=list, max_length=5000)
    rows_skipped: int = 0


def apply_report(report: DataplaneReport) -> Dict[str, int]:
    """Feed what the data plane served into the same metrics, SLOs and request log a request through
    this gateway would have fed. Counts are exact; each latency bucket is applied as that many
    observations at the bucket's mean, which keeps both the bucket counts and the sum exact."""
    from app import main as gw
    from app.models import RequestLog
    from app.observability import slo_tracker

    counted = 0
    for agg in report.aggregates:
        metric_path = gw._rejected_path_label(agg.path) if agg.rejected else gw.obs_metric_path_label(agg.path)
        protocol = metric_protocol(agg.path)
        # A request the data plane forwarded to an agent feeds the agent board, as one proxied here does.
        agent = gw.agent_edge_config.resolve(agg.path)
        for i, pair in enumerate(agg.buckets[: len(_BOUNDS) + 1]):
            n, total = int(pair[0]), float(pair[1])
            if n <= 0:
                continue
            mean = total / n
            counted += n
            if agent is not None:
                from app.agent_monitor import agent_monitor

                for _ in range(n):
                    agent_monitor.record_request(agent.slug, agg.status, round(mean * 1000, 1))
            for _ in range(n):
                gw.obs_record_request(
                    method=agg.method, path=metric_path, status_code=agg.status,
                    duration_seconds=mean, protocol="http", tenant=agg.tenant,
                )
            if gw.settings.metrics_enabled:
                gw.metrics.request_count.labels(
                    method=agg.method, path=metric_path, status_code=agg.status, protocol=protocol
                ).inc(n)
                hist = gw.metrics.request_duration.labels(method=agg.method, path=metric_path, protocol=protocol)
                for _ in range(n):
                    hist.observe(mean)
                gw.metrics.dataplane_native_requests.labels(
                    status_class=f"{agg.status // 100}xx"
                ).inc(n)
                if agg.rejected and agg.status == 429:
                    gw.metrics.rate_limit_hits.labels(client_id=agg.client_id or "anonymous").inc(n)
                # The error counter, as _log_handled_error and the proxy's own handler feed it.
                if agg.status >= 500 and agg.error_code:
                    gw.metrics.error_count.labels(error_type=agg.error_code, protocol="gateway").inc(n)
                if agg.status >= 500 and agg.error_type:
                    gw.metrics.error_count.labels(error_type=agg.error_type, protocol="unknown").inc(n)

    skipped = max(0, int(report.rows_skipped))
    for row in report.rows:
        log = RequestLog(
            request_id=row.request_id, method=row.method, path=row.path, client_ip=row.client_ip or "0.0.0.0",
            api_key=None, status_code=row.status, response_time_ms=row.duration_ms,
            user_agent=row.user_agent, error=row.error, protocol=log_protocol(row.path),
        )
        gw.request_log_writer.submit(log, row.api_key_id)
        if gw.log_export_manager.exporters:
            entry = {
                "request_id": row.request_id, "method": row.method, "path": row.path,
                "status_code": row.status, "duration_ms": row.duration_ms, "client_ip": row.client_ip,
                "timestamp": gw.utcnow().isoformat(), "user_agent": row.user_agent,
                "api_key_id": row.api_key_id, "tenant_id": row.tenant_id, "protocol": log_protocol(row.path),
            }
            if gw.log_export_manager.is_running():
                gw.log_export_manager.enqueue(entry)
    if skipped and gw.settings.metrics_enabled:
        gw.metrics.dataplane_rows_skipped.inc(skipped)
    return {"requests": counted, "rows": len(report.rows), "rows_skipped": skipped}


# Webhook and alert deliveries in flight; held here so they are not collected before they finish.
_background: set = set()


def _in_background(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def apply_events(db: AsyncSession, events: list) -> int:
    """Announce what happened in the data plane exactly as this gateway announces its own: the same
    webhook event types and payloads, the same alerts (which keep their own thresholds and intervals).
    Deliveries run in the background so a slow receiver never delays the report.

    A rate-limit event stands for every request one key had refused in one window since the last
    report (``count``), where this gateway fires one webhook per refused request."""
    from app.notifications import notification_manager
    from app.webhooks import WebhookEventType, webhook_manager

    limited = [e for e in events if e.type == "rate_limit_exceeded"]
    names: Dict[str, Optional[str]] = {}
    ids = {e.api_key_id for e in limited if e.api_key_id}
    if ids:
        rows = await db.execute(select(APIKeyModel.id, APIKeyModel.name).where(APIKeyModel.id.in_(ids)))
        names = {row[0]: row[1] for row in rows.all()}

    announced = 0
    for e in events:
        if e.type == "circuit_breaker" and e.state == "open":
            message = f"Circuit breaker opened after {e.failure_count} failures"
            _in_background(webhook_manager.trigger_webhook(WebhookEventType.CIRCUIT_BREAKER_OPEN, {
                "backend_url": e.backend_url, "state": "open", "previous_state": e.previous_state,
                "failure_count": e.failure_count, "failure_threshold": e.failure_threshold,
                "opened_at": e.opened_at, "timeout_seconds": e.timeout_seconds, "message": message,
            }))
        elif e.type == "circuit_breaker" and e.state == "closed":
            message = "Circuit breaker closed after successful request"
            _in_background(webhook_manager.trigger_webhook(WebhookEventType.CIRCUIT_BREAKER_CLOSED, {
                "backend_url": e.backend_url, "state": "closed", "previous_state": e.previous_state,
                "failure_count": e.failure_count, "message": message,
            }))
        elif e.type == "rate_limit_exceeded":
            if e.api_key_name:  # not a database key: it has a name and no id, as when refused here
                e.api_key_id = None
            _in_background(webhook_manager.trigger_webhook(WebhookEventType.RATE_LIMIT_EXCEEDED, {
                "api_key_id": e.api_key_id, "api_key_name": e.api_key_name or names.get(e.api_key_id or ""),
                "client_ip": e.client_ip, "limit": e.limit, "window": e.window, "path": e.path,
                "count": max(1, e.count),
            }))
            _in_background(notification_manager.send_rate_limit_alert(
                api_key_id=e.api_key_id, client_ip=e.client_ip or "", limit=e.limit,
                window=e.window or "", path=e.path or "",
            ))
            announced += 1
            continue
        else:
            continue  # a kind this version does not know: ignore rather than refuse the whole report
        _in_background(notification_manager.send_circuit_breaker_alert(
            backend_url=e.backend_url or "", state=e.state, previous_state=e.previous_state or "",
            failure_count=e.failure_count, message=message,
        ))
        announced += 1
    return announced


async def apply_tenant_usage(db: AsyncSession, usage: list) -> int:
    """Add what the data plane served for each tenant to the counters this gateway's own requests feed,
    in the windows the requests were admitted in. Committed before the report is answered, so the next
    snapshot answer already includes it."""
    from app.repositories.tenant_repo import TenantRepository

    applied = 0
    for u in usage:
        if u.requests <= 0:
            continue
        when = datetime.fromtimestamp(u.minute, timezone.utc).replace(tzinfo=None)
        try:
            await TenantRepository.increment_usage(
                db, u.tenant_id, request_count=u.requests, error_count=max(0, u.errors),
                bytes_out=max(0, u.bytes_out), now=when,
            )
            applied += u.requests
        except Exception:  # noqa: BLE001 - a tenant deleted meanwhile must not refuse the whole report
            await db.rollback()
    return applied


async def apply_key_usage(db: AsyncSession, uses: list) -> int:
    """Move each key's ``last_used_at`` forward to the data plane's latest use (never backwards)."""
    n = 0
    for use in uses:
        when = datetime.fromtimestamp(use.at, timezone.utc).replace(tzinfo=None)
        result = await db.execute(
            update(APIKeyModel)
            .where(APIKeyModel.id == use.id)
            .where(or_(APIKeyModel.last_used_at.is_(None), APIKeyModel.last_used_at < when))
            .values(last_used_at=when)
        )
        n += result.rowcount or 0
    if n:
        await db.commit()
    return n


@router.post("/report")
async def report(
    body: DataplaneReport,
    x_dataplane_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, int]:
    """What the Rust data plane served and saw: counts, log rows, key use and events."""
    _authorize(x_dataplane_token)
    out = apply_report(body)
    out["tenant_requests"] = await apply_tenant_usage(db, body.tenant_usage)
    out["keys_updated"] = await apply_key_usage(db, body.keys_used)
    out["events"] = await apply_events(db, body.events)
    return out


ANSWER_HEADER = "x-answer-h-"


@router.post("/shape-answer")
async def shape_answer(request: Request, x_dataplane_token: Optional[str] = Header(default=None)) -> Response:
    """What a caller is given for a backend's answer the data plane holds and is not certain of
    changing as this gateway's transformation rules change it: one that is compressed, is not
    known to be short, or has a body it might read or write differently.

    The answer comes as the request: its body as the backend sent it, its status in
    ``X-Answer-Status``, each of its header lines as ``X-Answer-H-<n>-<name>`` (``n`` its place
    among them, in four digits, so that two lines of one name stay two), and the request it
    answers in ``X-Answer-Method`` and ``X-Answer-Path`` (percent-escaped). What goes back is one line of
    JSON, ``{"status", "headers"}``, then the body. Made by the code that answers a request
    served here (``app.main.answer_from``), so the two cannot differ."""
    import httpx
    from urllib.parse import unquote

    from app.main import answer_from

    _authorize(x_dataplane_token)
    try:
        status = int(request.headers["x-answer-status"])
        method = request.headers["x-answer-method"]
        path = unquote(request.headers["x-answer-path"], errors="strict")
    except (KeyError, ValueError):
        raise HTTPException(status_code=400, detail="not an answer")
    prefix = ANSWER_HEADER.encode()
    numbered = sorted((name[len(prefix):], value) for name, value in request.headers.raw if name.startswith(prefix))
    if any(len(name) < 6 or not name[:4].isdigit() or name[4:5] != b"-" for name, _ in numbered):
        raise HTTPException(status_code=400, detail="not an answer")
    headers = [(name[5:], value) for name, value in numbered]
    answer = httpx.Response(status, headers=headers, content=await request.body())
    made = await answer_from(answer, method, path, {}, None)
    lines = [[name.decode("latin-1"), value.decode("latin-1")] for name, value in made.raw_headers]
    return Response(json.dumps({"status": made.status_code, "headers": lines}).encode() + b"\n" + made.body, media_type="application/octet-stream")


@router.get("/snapshot")
async def snapshot(
    since: Optional[str] = None,
    x_dataplane_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """The working set the Rust data plane serves from: key digests, limits and settings."""
    _authorize(x_dataplane_token)
    snap = await current_snapshot(db)
    limited = {t for t, entry in snap["tenants"].items() if entry["minute"] or entry["hour"]}
    usage = await tenant_usage(db, limited)
    try:
        ready = await readiness_note()
    except Exception:  # noqa: BLE001 - without it the data plane asks this gateway each time
        ready = None
    if since and since == snap["revision"]:
        return {"unchanged": True, "revision": snap["revision"], "tenant_usage": usage, "ready": ready}
    return {**snap, "tenant_usage": usage, "ready": ready}
