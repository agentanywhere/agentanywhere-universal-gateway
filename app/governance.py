"""Governance for AgentAnywhere Universal Gateway.

Single module covering the four pillars of API-gateway governance:

* **Audit log.** A persistent, tamper-evident log of every admin /
  config / auth-significant action. Each row carries a SHA-256 hash
  chain so that retroactive tampering of any historical row breaks
  the verification of every later row.

* **RBAC.** Admin tokens are mapped to roles (``super_admin`` /
  ``operator`` / ``viewer`` / ``auditor``). A ``require_role()``
  dependency is provided for ``admin/api.py`` routes.

* **Policy engine.** Declarative DENY / ALLOW / RATE_LIMIT policies
  with conditions (path glob, method, IP CIDR, time-of-day, tenant,
  role, header value) and action; evaluated in the auth middleware
  before backends are called. Stored in the DB and cached in-memory.

* **IP allow / deny.** Per-scope (global / tenant / api-key) CIDR
  matching, evaluated alongside policies.

Each surface has a thin, unit-testable Python API plus a corresponding
admin endpoint surface in ``admin/api.py``.
"""
from __future__ import annotations

import asyncio
import weakref
import fnmatch
import hashlib
import ipaddress
import json
import re
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime
from app.time_utils import utcnow
from enum import Enum
from typing import (
    Any,
    Awaitable,
    Callable,
    Deque,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Tuple,
)

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db_models import (
    AdminRoleModel,
    GovernanceAuditLogModel,
    IPRuleModel,
    PolicyModel,
)

logger = structlog.get_logger("governance")


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


class Role(str, Enum):
    """Admin roles, in increasing privilege order."""

    AUDITOR = "auditor"  # read audit log only
    VIEWER = "viewer"  # read all read-only endpoints
    OPERATOR = "operator"  # mutate keys, routes, transformations, plugins, …
    SUPER_ADMIN = "super_admin"  # everything, including governance itself


# Permissions per role. Permissions are coarse-grained; finer checks
# happen at the endpoint level.
_ROLE_PERMS: Dict[Role, set] = {
    Role.AUDITOR: {"audit:read"},
    Role.VIEWER: {"audit:read", "config:read", "metrics:read", "health:read"},
    Role.OPERATOR: {
        "audit:read", "config:read", "config:write",
        "metrics:read", "health:read",
        "keys:write", "routes:write", "plugins:write",
        "transformations:write", "webhooks:write",
        "logging:write",
    },
    Role.SUPER_ADMIN: {"*"},
}


def role_has_permission(role: Role, permission: str) -> bool:
    perms = _ROLE_PERMS.get(role, set())
    if "*" in perms:
        return True
    return permission in perms


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@dataclass
class AuditEntry:
    """In-flight audit entry before persistence."""

    action: str
    actor: Optional[str] = None
    actor_role: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    request_id: Optional[str] = None
    client_ip: Optional[str] = None
    user_agent: Optional[str] = None
    outcome: str = "success"
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

    def fingerprint(self, prev_hash: Optional[str], timestamp: datetime) -> str:
        """SHA-256 over the canonical content + previous hash."""
        payload = {
            "ts": timestamp.isoformat(),
            "action": self.action,
            "actor": self.actor,
            "actor_role": self.actor_role,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "outcome": self.outcome,
            "before": self.before,
            "after": self.after,
            "metadata": self.metadata,
            "request_id": self.request_id,
            "client_ip": self.client_ip,
        }
        canonical = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        h = hashlib.sha256()
        if prev_hash:
            h.update(prev_hash.encode("utf-8"))
        h.update(canonical)
        return h.hexdigest()


class AuditLog:
    """Persistent audit log with hash-chain integrity.

    A small in-memory ring buffer is kept for fast reads; the durable
    record lives in ``governance_audit_log``.
    """

    def __init__(self, recent_buffer_size: int = 200) -> None:
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=recent_buffer_size)
        self._last_hash: Optional[str] = None
        # One lock per event loop: the log is a module-level singleton and
        # asyncio locks cannot be shared between loops.
        self._locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
            weakref.WeakKeyDictionary()
        )

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    async def record(self, db: AsyncSession, entry: AuditEntry) -> Dict[str, Any]:
        """Persist an audit entry and return its serialised form.

        Serialised: reading the previous hash, inserting the row, committing
        and remembering the new hash happen under one lock, so concurrent
        writers (for example several background tasks, each with its own
        session) cannot chain from the same previous hash and fork the chain.
        The lock is per process: several workers or instances sharing a
        database would need the chain serialised in the database itself.
        """
        async with self._lock():
            try:
                return await self._record_locked(db, entry)
            except Exception:
                # Re-read the last persisted hash next time rather than chain
                # onto a row that may not have been committed.
                self._last_hash = None
                raise

    async def _record_locked(self, db: AsyncSession, entry: AuditEntry) -> Dict[str, Any]:
        timestamp = utcnow()

        prev_hash = self._last_hash
        if prev_hash is None:
            # Look up the most recent persisted hash so we chain across
            # process restarts.
            stmt = (
                select(GovernanceAuditLogModel.entry_hash)
                .order_by(GovernanceAuditLogModel.id.desc())
                .limit(1)
            )
            result = await db.execute(stmt)
            row = result.first()
            prev_hash = row[0] if row else None

        entry_hash = entry.fingerprint(prev_hash, timestamp)
        row = GovernanceAuditLogModel(
            timestamp=timestamp,
            actor=entry.actor,
            actor_role=entry.actor_role,
            action=entry.action,
            resource_type=entry.resource_type,
            resource_id=entry.resource_id,
            request_id=entry.request_id,
            client_ip=entry.client_ip,
            user_agent=entry.user_agent,
            outcome=entry.outcome,
            before=entry.before,
            after=entry.after,
            audit_metadata=entry.metadata,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
        )
        db.add(row)
        await db.flush()
        as_dict = self._row_to_dict(row)
        await db.commit()
        # Only a committed row may become the link the next entry chains to.
        self._last_hash = entry_hash
        self._recent.append(as_dict)
        logger.info(
            "audit_recorded",
            action=entry.action,
            actor=entry.actor,
            outcome=entry.outcome,
            resource=f"{entry.resource_type}:{entry.resource_id}"
            if entry.resource_type
            else None,
        )
        return as_dict

    async def list(
        self,
        db: AsyncSession,
        *,
        limit: int = 100,
        offset: int = 0,
        action: Optional[str] = None,
        actor: Optional[str] = None,
        resource_type: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        stmt = select(GovernanceAuditLogModel).order_by(
            GovernanceAuditLogModel.id.desc()
        )
        if action:
            stmt = stmt.where(GovernanceAuditLogModel.action == action)
        if actor:
            stmt = stmt.where(GovernanceAuditLogModel.actor == actor)
        if resource_type:
            stmt = stmt.where(GovernanceAuditLogModel.resource_type == resource_type)
        if outcome:
            stmt = stmt.where(GovernanceAuditLogModel.outcome == outcome)
        stmt = stmt.limit(max(1, min(limit, 1000))).offset(max(0, offset))
        result = await db.execute(stmt)
        return [self._row_to_dict(r) for r in result.scalars().all()]

    async def verify_chain(self, db: AsyncSession) -> Dict[str, Any]:
        """Walk the chain forward and confirm hashes match.

        Returns ``{"valid": bool, "checked": n, "broken_at": id_or_None}``.
        """
        stmt = select(GovernanceAuditLogModel).order_by(
            GovernanceAuditLogModel.id.asc()
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        prev_hash: Optional[str] = None
        checked = 0
        for row in rows:
            entry = AuditEntry(
                action=row.action,
                actor=row.actor,
                actor_role=row.actor_role,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                request_id=row.request_id,
                client_ip=row.client_ip,
                user_agent=row.user_agent,
                outcome=row.outcome,
                before=row.before,
                after=row.after,
                metadata=row.audit_metadata,
            )
            expected = entry.fingerprint(prev_hash, row.timestamp)
            if expected != row.entry_hash:
                return {"valid": False, "checked": checked, "broken_at": row.id}
            checked += 1
            prev_hash = row.entry_hash
        return {"valid": True, "checked": checked, "broken_at": None}

    def recent(self) -> List[Dict[str, Any]]:
        return list(self._recent)

    @staticmethod
    def _row_to_dict(row: GovernanceAuditLogModel) -> Dict[str, Any]:
        return {
            "id": row.id,
            "timestamp": row.timestamp.isoformat() + "Z",
            "actor": row.actor,
            "actor_role": row.actor_role,
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "request_id": row.request_id,
            "client_ip": row.client_ip,
            "user_agent": row.user_agent,
            "outcome": row.outcome,
            "before": row.before,
            "after": row.after,
            "metadata": row.audit_metadata,
            "prev_hash": row.prev_hash,
            "entry_hash": row.entry_hash,
        }


audit_log = AuditLog()


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------


class PolicyAction(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    # Not enforced yet: a matching rate_limit policy is evaluated and its
    # limit recorded on the request, but no limit is applied.
    RATE_LIMIT = "rate_limit"


@dataclass
class PolicyConditions:
    """Conditions evaluated against an in-flight request.

    Any subset of fields may be set; missing fields match anything.

    * ``path`` is a glob (``/api/admin/*``) matched against the request
      path.
    * ``methods`` is a list of HTTP methods (any-of).
    * ``cidrs`` is a list of CIDRs; the request matches if the client
      IP is in any of them.
    * ``tenants`` is a list of tenant ids.
    * ``roles`` is a list of admin / OAuth roles.
    * ``time_of_day`` is ``{"start": "HH:MM", "end": "HH:MM"}`` (UTC).
    * ``headers`` is ``{header_name: regex_or_value}`` (any-of, case-insensitive).
    """

    path: Optional[str] = None
    methods: List[str] = field(default_factory=list)
    cidrs: List[str] = field(default_factory=list)
    tenants: List[str] = field(default_factory=list)
    roles: List[str] = field(default_factory=list)
    time_of_day: Optional[Dict[str, str]] = None
    headers: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PolicyConditions":
        return cls(
            path=data.get("path"),
            methods=[m.upper() for m in data.get("methods", [])],
            cidrs=list(data.get("cidrs", [])),
            tenants=list(data.get("tenants", [])),
            roles=list(data.get("roles", [])),
            time_of_day=data.get("time_of_day"),
            headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "methods": list(self.methods),
            "cidrs": list(self.cidrs),
            "tenants": list(self.tenants),
            "roles": list(self.roles),
            "time_of_day": self.time_of_day,
            "headers": dict(self.headers),
        }

    def matches(self, ctx: Mapping[str, Any]) -> bool:
        path = ctx.get("path", "") or ""
        method = (ctx.get("method") or "").upper()
        client_ip = ctx.get("client_ip") or ""
        tenant = ctx.get("tenant") or ""
        role = ctx.get("role") or ""
        headers = ctx.get("headers") or {}

        if self.path and not fnmatch.fnmatch(path, self.path):
            return False
        if self.methods and method not in self.methods:
            return False
        if self.tenants and tenant not in self.tenants:
            return False
        if self.roles and role not in self.roles:
            return False
        if self.cidrs:
            if not _ip_matches_any_cidr(client_ip, self.cidrs):
                return False
        if self.time_of_day:
            if not _time_in_window(self.time_of_day):
                return False
        if self.headers:
            for name, expected in self.headers.items():
                actual = next(
                    (v for k, v in headers.items() if k.lower() == name.lower()),
                    None,
                )
                if actual is None:
                    return False
                # Treat the expected value as a regex; fall back to
                # equality when compilation fails.
                try:
                    if not re.search(expected, actual):
                        return False
                except re.error:
                    if expected != actual:
                        return False
        return True


@dataclass
class Policy:
    """A single declarative policy (DB-backed)."""

    id: str
    name: str
    description: Optional[str] = None
    priority: int = 100
    enabled: bool = True
    conditions: PolicyConditions = field(default_factory=PolicyConditions)
    action: Dict[str, Any] = field(default_factory=lambda: {"type": "allow"})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "priority": self.priority,
            "enabled": self.enabled,
            "conditions": self.conditions.to_dict(),
            "action": dict(self.action),
        }

    @classmethod
    def from_row(cls, row: PolicyModel) -> "Policy":
        return cls(
            id=row.id,
            name=row.name,
            description=row.description,
            priority=row.priority,
            enabled=row.enabled,
            conditions=PolicyConditions.from_dict(row.conditions or {}),
            action=row.action or {"type": "allow"},
        )


def _ip_matches_any_cidr(ip: str, cidrs: Iterable[str]) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def _time_in_window(window: Mapping[str, str]) -> bool:
    start = window.get("start")
    end = window.get("end")
    if not start or not end:
        return True

    def parse(s: str) -> dtime:
        h, m = s.split(":")
        return dtime(int(h), int(m))

    try:
        s = parse(start)
        e = parse(end)
    except (ValueError, AttributeError):
        return True
    now = utcnow().time()
    if s <= e:
        return s <= now <= e
    # Crosses midnight.
    return now >= s or now <= e


@dataclass
class PolicyDecision:
    allowed: bool
    policy_id: Optional[str]
    reason: Optional[str] = None
    rate_limit: Optional[Dict[str, Any]] = None


class PolicyEngine:
    """Evaluates policies against a request context."""

    def __init__(self) -> None:
        self._cache: List[Policy] = []
        self._loaded: bool = False

    async def reload(self, db: AsyncSession) -> None:
        stmt = select(PolicyModel).order_by(
            PolicyModel.priority.asc(), PolicyModel.created_at.asc()
        )
        result = await db.execute(stmt)
        self._cache = [Policy.from_row(r) for r in result.scalars().all() if r.enabled]
        self._loaded = True

    def evaluate(self, ctx: Mapping[str, Any]) -> PolicyDecision:
        """Evaluate the cached policies against ``ctx``.

        Policies are evaluated in priority order (low number first).
        The first matching policy decides.  If no policy matches, the
        default outcome is ``allowed``.
        """
        for policy in self._cache:
            if not policy.enabled:
                continue
            if not policy.conditions.matches(ctx):
                continue
            kind = (policy.action.get("type") or "allow").lower()
            if kind == "deny":
                return PolicyDecision(
                    allowed=False,
                    policy_id=policy.id,
                    reason=policy.action.get("reason") or f"denied by policy '{policy.name}'",
                )
            if kind == "rate_limit":
                return PolicyDecision(
                    allowed=True,
                    policy_id=policy.id,
                    rate_limit=policy.action,
                )
            # allow / unknown -> short-circuit allow
            return PolicyDecision(
                allowed=True,
                policy_id=policy.id,
                reason=f"matched allow policy '{policy.name}'",
            )
        return PolicyDecision(allowed=True, policy_id=None)

    def list_policies(self) -> List[Dict[str, Any]]:
        return [p.to_dict() for p in self._cache]


policy_engine = PolicyEngine()


# ---------------------------------------------------------------------------
# IP rules
# ---------------------------------------------------------------------------


@dataclass
class IPRule:
    id: str
    cidr: str
    action: str
    scope: str  # "global" | "tenant" | "api_key"
    scope_id: Optional[str]
    description: Optional[str]
    enabled: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class _RuleList(list):
    """List that bumps a version counter on every mutation.

    ``IPRuleEngine`` keeps a pre-parsed index of its rules; the version lets
    it notice direct edits (``.clear()``, ``.append()``, ...) and rebuild.
    """

    version = 0

    def _bump(self) -> None:
        self.version += 1

    def _mutator(name):  # type: ignore[no-redef]
        base = getattr(list, name)

        def wrapper(self, *a, **kw):
            self._bump()
            return base(self, *a, **kw)

        wrapper.__name__ = name
        return wrapper

    for _n in (
        "append", "extend", "insert", "remove", "pop", "clear", "sort",
        "reverse", "__setitem__", "__delitem__", "__iadd__", "__imul__",
    ):
        locals()[_n] = _mutator(_n)
    del _n, _mutator


class _Bucket:
    """Pre-parsed rules of one scope (and scope id), in load order."""

    __slots__ = ("denies", "allows", "first_allow")

    def __init__(self) -> None:
        self.denies: List[Tuple[IPRule, Any]] = []
        self.allows: List[Tuple[IPRule, Any]] = []
        self.first_allow: Optional[IPRule] = None


def _parse_network(cidr: str) -> Any:
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except (ValueError, TypeError):
        return None


class IPRuleEngine:
    """Decision engine for IP allow / deny rules."""

    def __init__(self) -> None:
        self._rules_list: _RuleList = _RuleList()
        self._index_key: Optional[Tuple[int, int]] = None
        self._global: Optional[_Bucket] = None
        self._tenant: Dict[str, _Bucket] = {}
        self._api_key: Dict[str, _Bucket] = {}

    @property
    def _rules(self) -> List[IPRule]:
        return self._rules_list

    @_rules.setter
    def _rules(self, value: List[IPRule]) -> None:
        old = self._rules_list
        new = _RuleList(value)
        new.version = old.version + 1
        self._rules_list = new

    async def reload(self, db: AsyncSession) -> None:
        stmt = select(IPRuleModel).order_by(IPRuleModel.created_at.asc())
        result = await db.execute(stmt)
        self._rules = [
            IPRule(
                id=r.id,
                cidr=r.cidr,
                action=r.action,
                scope=r.scope,
                scope_id=r.scope_id,
                description=r.description,
                enabled=r.enabled,
            )
            for r in result.scalars().all()
            if r.enabled
        ]

    def _ensure_index(self) -> None:
        rules = self._rules_list
        key = (id(rules), rules.version)
        if key == self._index_key:
            return
        glob: Optional[_Bucket] = None
        tenant: Dict[str, _Bucket] = {}
        api_key: Dict[str, _Bucket] = {}
        for r in rules:
            if r.scope == "global":
                if glob is None:
                    glob = _Bucket()
                bucket = glob
            elif r.scope in ("tenant", "api_key"):
                if r.scope_id is None:
                    # A scoped rule with no scope id belongs to nobody.
                    continue
                table = tenant if r.scope == "tenant" else api_key
                bucket = table.get(r.scope_id)
                if bucket is None:
                    bucket = table[r.scope_id] = _Bucket()
            else:
                continue
            entry = (r, _parse_network(r.cidr))
            if r.action == "deny":
                bucket.denies.append(entry)
            elif r.action == "allow":
                bucket.allows.append(entry)
                if bucket.first_allow is None:
                    bucket.first_allow = r
        self._global, self._tenant, self._api_key = glob, tenant, api_key
        self._index_key = key

    @staticmethod
    def _check_bucket(
        bucket: Optional[_Bucket], addr: Any
    ) -> Optional[IPRule]:
        """Return the rule that blocks *addr*, or None."""
        if bucket is None:
            return None
        # Deny rules win.
        if addr is not None:
            for rule, net in bucket.denies:
                if net is not None and addr in net:
                    return rule
        if bucket.allows:
            if addr is not None:
                for _rule, net in bucket.allows:
                    if net is not None and addr in net:
                        return None
            return bucket.first_allow
        return None

    def evaluate(
        self,
        ip: str,
        *,
        tenant: Optional[str] = None,
        api_key_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[IPRule]]:
        """Returns ``(allowed, matched_rule_or_None)``.

        Rules are evaluated in this order: global → tenant → api_key.
        If no rule matches, the default outcome is ``allowed``.

        Global rules apply to everyone. A tenant-scoped rule applies only
        to requests of that tenant, and an api_key-scoped rule only to
        that API key id (so a request with no tenant / no key id is never
        subject to a scoped rule).

        If *any* allow rule exists for a scope, that scope becomes
        allowlist-only (i.e. an IP not matching any allow rule is
        denied). Deny rules always win when matched.
        """
        self._ensure_index()
        addr: Any = None
        if ip:
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                addr = None

        blocked = self._check_bucket(self._global, addr)
        if blocked is None and tenant is not None:
            blocked = self._check_bucket(self._tenant.get(tenant), addr)
        if blocked is None and api_key_id is not None:
            blocked = self._check_bucket(self._api_key.get(api_key_id), addr)
        if blocked is not None:
            return False, blocked
        return True, None

    def list_rules(self) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self._rules_list]


ip_rule_engine = IPRuleEngine()


# ---------------------------------------------------------------------------
# Admin role helpers
# ---------------------------------------------------------------------------


async def get_role_for_token(
    db: AsyncSession, token: str
) -> Optional[Role]:
    """Return the configured role for a raw admin token, if any."""
    if not token:
        return None
    h = hash_token(token)
    stmt = select(AdminRoleModel).where(
        AdminRoleModel.token_hash == h,
        AdminRoleModel.is_active == True,  # noqa: E712
    )
    result = await db.execute(stmt)
    row = result.scalars().first()
    if not row:
        return None
    try:
        role = Role(row.role)
    except ValueError:
        return None
    # Update last_used_at (best effort).
    try:
        await db.execute(
            update(AdminRoleModel)
            .where(AdminRoleModel.id == row.id)
            .values(last_used_at=utcnow())
        )
    except Exception:  # noqa: BLE001
        pass
    return role


async def assign_role(
    db: AsyncSession,
    *,
    token: str,
    name: str,
    role: Role,
) -> AdminRoleModel:
    """Map a raw admin token to a role. The token is hashed before storage."""
    row = AdminRoleModel(
        token_hash=hash_token(token),
        name=name,
        role=role.value,
        is_active=True,
    )
    db.add(row)
    await db.flush()
    await db.commit()
    return row


async def revoke_role(db: AsyncSession, role_id: str) -> bool:
    stmt = (
        update(AdminRoleModel)
        .where(AdminRoleModel.id == role_id)
        .values(is_active=False)
    )
    res = await db.execute(stmt)
    await db.commit()
    return res.rowcount > 0


async def list_roles(db: AsyncSession) -> List[Dict[str, Any]]:
    stmt = select(AdminRoleModel).order_by(AdminRoleModel.created_at.desc())
    result = await db.execute(stmt)
    return [
        {
            "id": r.id,
            "name": r.name,
            "role": r.role,
            "is_active": r.is_active,
            "created_at": r.created_at.isoformat() + "Z",
            "last_used_at": (r.last_used_at.isoformat() + "Z") if r.last_used_at else None,
            "token_fingerprint": r.token_hash[:12],
        }
        for r in result.scalars().all()
    ]


# ---------------------------------------------------------------------------
# CRUD helpers for policies / ip rules
# ---------------------------------------------------------------------------


async def create_policy(
    db: AsyncSession,
    *,
    name: str,
    conditions: Mapping[str, Any],
    action: Mapping[str, Any],
    description: Optional[str] = None,
    priority: int = 100,
    enabled: bool = True,
) -> Policy:
    row = PolicyModel(
        name=name,
        description=description,
        priority=priority,
        enabled=enabled,
        conditions=dict(conditions),
        action=dict(action),
    )
    db.add(row)
    await db.flush()
    await db.commit()
    await policy_engine.reload(db)
    return Policy.from_row(row)


async def update_policy(
    db: AsyncSession,
    policy_id: str,
    **changes: Any,
) -> Optional[Policy]:
    stmt = update(PolicyModel).where(PolicyModel.id == policy_id).values(**changes)
    res = await db.execute(stmt)
    if not res.rowcount:
        return None
    await db.commit()
    await policy_engine.reload(db)
    fetch = await db.execute(select(PolicyModel).where(PolicyModel.id == policy_id))
    row = fetch.scalars().first()
    return Policy.from_row(row) if row else None


async def delete_policy(db: AsyncSession, policy_id: str) -> bool:
    fetch = await db.execute(select(PolicyModel).where(PolicyModel.id == policy_id))
    row = fetch.scalars().first()
    if not row:
        return False
    await db.delete(row)
    await db.commit()
    await policy_engine.reload(db)
    return True


async def list_policies(db: AsyncSession) -> List[Dict[str, Any]]:
    stmt = select(PolicyModel).order_by(PolicyModel.priority.asc(), PolicyModel.name.asc())
    result = await db.execute(stmt)
    return [Policy.from_row(r).to_dict() for r in result.scalars().all()]


async def create_ip_rule(
    db: AsyncSession,
    *,
    cidr: str,
    action: str,
    scope: str = "global",
    scope_id: Optional[str] = None,
    description: Optional[str] = None,
    enabled: bool = True,
) -> IPRule:
    if action not in {"allow", "deny"}:
        raise ValueError(f"action must be 'allow' or 'deny', got {action!r}")
    if scope not in {"global", "tenant", "api_key"}:
        raise ValueError(f"scope must be 'global'|'tenant'|'api_key', got {scope!r}")
    # Validate CIDR.
    ipaddress.ip_network(cidr, strict=False)

    row = IPRuleModel(
        cidr=cidr,
        action=action,
        scope=scope,
        scope_id=scope_id,
        description=description,
        enabled=enabled,
    )
    db.add(row)
    await db.flush()
    await db.commit()
    await ip_rule_engine.reload(db)
    return IPRule(
        id=row.id, cidr=row.cidr, action=row.action, scope=row.scope,
        scope_id=row.scope_id, description=row.description, enabled=row.enabled,
    )


async def list_ip_rules(db: AsyncSession) -> List[Dict[str, Any]]:
    stmt = select(IPRuleModel).order_by(IPRuleModel.created_at.asc())
    result = await db.execute(stmt)
    return [
        {
            "id": r.id, "cidr": r.cidr, "action": r.action, "scope": r.scope,
            "scope_id": r.scope_id, "description": r.description, "enabled": r.enabled,
            "created_at": r.created_at.isoformat() + "Z",
        }
        for r in result.scalars().all()
    ]


async def delete_ip_rule(db: AsyncSession, rule_id: str) -> bool:
    fetch = await db.execute(select(IPRuleModel).where(IPRuleModel.id == rule_id))
    row = fetch.scalars().first()
    if not row:
        return False
    await db.delete(row)
    await db.commit()
    await ip_rule_engine.reload(db)
    return True


# ---------------------------------------------------------------------------
# API key lifecycle helpers
# ---------------------------------------------------------------------------


async def expiring_api_keys(
    db: AsyncSession, *, within_days: int = 7
) -> List[Dict[str, Any]]:
    """Return active API keys that expire within ``within_days``."""
    from datetime import timedelta
    from app.db_models import APIKeyModel

    cutoff = utcnow() + timedelta(days=within_days)
    stmt = (
        select(APIKeyModel)
        .where(APIKeyModel.is_active == True)  # noqa: E712
        .where(APIKeyModel.expires_at != None)  # noqa: E711
        .where(APIKeyModel.expires_at <= cutoff)
        .order_by(APIKeyModel.expires_at.asc())
    )
    result = await db.execute(stmt)
    out: List[Dict[str, Any]] = []
    now = utcnow()
    for row in result.scalars().all():
        out.append({
            "id": row.id,
            "name": row.name,
            "expires_at": row.expires_at.isoformat() + "Z",
            "expires_in_seconds": int((row.expires_at - now).total_seconds()),
            "expired": row.expires_at <= now,
        })
    return out


async def auto_deactivate_expired_keys(db: AsyncSession) -> int:
    """Deactivate any API key whose expiry has passed. Returns count."""
    from app.db_models import APIKeyModel

    now = utcnow()
    stmt = (
        update(APIKeyModel)
        .where(APIKeyModel.is_active == True)  # noqa: E712
        .where(APIKeyModel.expires_at != None)  # noqa: E711
        .where(APIKeyModel.expires_at <= now)
        .values(is_active=False)
    )
    res = await db.execute(stmt)
    await db.commit()
    if res.rowcount:
        logger.info("api_keys_auto_deactivated", count=res.rowcount)
    return res.rowcount or 0


# ---------------------------------------------------------------------------
# Convenience for callers without a DB session
# ---------------------------------------------------------------------------


AsyncDBProvider = Callable[[], Awaitable[AsyncSession]]


async def quick_audit(
    db_provider: AsyncDBProvider,
    *,
    action: str,
    actor: Optional[str] = None,
    actor_role: Optional[str] = None,
    outcome: str = "success",
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """Helper for fire-and-forget audit writes without juggling sessions."""
    try:
        db = await db_provider()
        try:
            return await audit_log.record(
                db,
                AuditEntry(
                    action=action,
                    actor=actor,
                    actor_role=actor_role,
                    outcome=outcome,
                    **kwargs,
                ),
            )
        finally:
            await db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit_write_failed", action=action, error=str(exc))
        return None


__all__ = [
    "AuditEntry",
    "AuditLog",
    "IPRule",
    "IPRuleEngine",
    "Policy",
    "PolicyAction",
    "PolicyConditions",
    "PolicyDecision",
    "PolicyEngine",
    "Role",
    "_ip_matches_any_cidr",
    "_time_in_window",
    "assign_role",
    "audit_log",
    "auto_deactivate_expired_keys",
    "create_ip_rule",
    "create_policy",
    "delete_ip_rule",
    "delete_policy",
    "expiring_api_keys",
    "get_role_for_token",
    "hash_token",
    "ip_rule_engine",
    "list_ip_rules",
    "list_policies",
    "list_roles",
    "policy_engine",
    "quick_audit",
    "revoke_role",
    "role_has_permission",
    "update_policy",
]
