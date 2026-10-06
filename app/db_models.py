"""SQLAlchemy database models."""
from sqlalchemy import Column, String, Integer, Boolean, DateTime, JSON, Text, ForeignKey, Identity
from app.time_utils import utcnow
import uuid
from app.database import Base


class APIKeyModel(Base):
    """API Key database model."""
    __tablename__ = "api_keys"
    
    id = Column(String, primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    key = Column(String, unique=True, index=True, nullable=True)  # Made nullable since we use key_hash
    key_hash = Column(String, unique=True, index=True, nullable=False)  # Primary lookup field
    name = Column(String, nullable=False)
    rate_limit_per_minute = Column(Integer, default=60)
    rate_limit_per_hour = Column(Integer, default=1000)
    allowed_paths = Column(JSON, default=list)
    key_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=utcnow)
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)  # Added for last used tracking
    is_active = Column(Boolean, default=True)


class RouteModel(Base):
    """Route database model."""
    __tablename__ = "routes"
    
    id = Column(String, primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    path = Column(String, nullable=False, index=True)
    backend_url = Column(String, nullable=False)
    protocol = Column(String, nullable=False)
    timeout = Column(Integer, default=30)
    rate_limit = Column(Integer, nullable=True)
    requires_auth = Column(Boolean, default=True)
    headers = Column(JSON, default=dict)
    rewrite_path = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)  # Added for route activation
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class AgentRegistryModel(Base):
    """AgentAnywhere agent-edge registry — the dynamic source of truth for
    ``/agents/<slug>`` routing.

    Replaces the static ``AGENT_ROUTES_JSON`` / ``AGENT_PUBLIC_PATHS_JSON``
    environment variables: rows here are managed from the gateway admin console
    (``/admin/agents``) and hot-reloaded into the running proxy without a
    redeploy. On first boot an empty table is seeded from the env vars so the
    migration off the static setting is seamless.
    """
    __tablename__ = "agent_registry"

    # kebab-case slug that appears in the browser path (app.<host>/agents/<slug>/…)
    slug = Column(String, primary_key=True, index=True)
    # Upstream origin the edge proxies to (no trailing slash), e.g. http://10.0.0.10:8090
    upstream_base = Column(String, nullable=False)
    # Expected token audience / IDA `agents` entitlement. Defaults to the slug when null.
    audience = Column(String, nullable=True)
    # Path prefixes (relative to the agent root) that bypass SSO (candidate/invite flows).
    public_paths = Column(JSON, default=list, nullable=False)
    # Catalog visibility: "published" (listed) | "hidden" (reachable, not listed). Not a routing gate.
    status = Column(String, default="published", nullable=False)
    display_name = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    runtime_mode = Column(String, default="dedicated", nullable=False)  # dedicated | shared
    data_residency = Column(String, default="in", nullable=False)
    # Master routing gate — a disabled agent is not routable regardless of status.
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    updated_by = Column(String, nullable=True)


class RequestLogModel(Base):
    """Request log database model."""
    __tablename__ = "request_logs"
    
    id = Column(Integer, Identity(), primary_key=True)  # Auto-incrementing integer ID
    request_id = Column(String, index=True, nullable=False)
    method = Column(String, nullable=False)
    path = Column(String, nullable=False, index=True)
    client_ip = Column(String, nullable=False)
    api_key_id = Column(String, ForeignKey("api_keys.id"), nullable=True)
    tenant_id = Column(String, ForeignKey("tenants.tenant_id", ondelete="SET NULL"), nullable=True, index=True)
    status_code = Column(Integer, nullable=False, index=True)
    response_time_ms = Column(Integer, nullable=False)
    timestamp = Column(DateTime, default=utcnow, index=True)
    user_agent = Column(String, nullable=True)
    error = Column(Text, nullable=True)
    protocol = Column(String, nullable=True)


class SettingsProfileModel(Base):
    """Settings profile database model."""
    __tablename__ = "settings_profiles"
    
    id = Column(String, primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    protocol_settings = Column(JSON, default=dict)  # Dict[str, ProtocolSettings]
    global_settings = Column(JSON, default=dict)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    created_by = Column(String, nullable=True)


class TenantModel(Base):
    """Tenant database model for multi-tenant isolation."""
    __tablename__ = "tenants"

    tenant_id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    config = Column(JSON, default=dict)
    quota = Column(JSON, default=dict)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class TenantAPIKeyModel(Base):
    """Mapping between tenants and API keys."""
    __tablename__ = "tenant_api_keys"

    id = Column(Integer, Identity(), primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False, index=True)
    api_key_id = Column(String, ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False, index=True)
    api_key_hash = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class TenantUsageModel(Base):
    """Per-tenant rolling usage counters used for quota enforcement."""
    __tablename__ = "tenant_usage"

    id = Column(Integer, Identity(), primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False, index=True)
    window = Column(String, nullable=False, index=True)  # e.g. "minute", "hour", "day"
    window_start = Column(DateTime, nullable=False, index=True)
    request_count = Column(Integer, default=0, nullable=False)
    error_count = Column(Integer, default=0, nullable=False)
    bytes_in = Column(Integer, default=0, nullable=False)
    bytes_out = Column(Integer, default=0, nullable=False)


# ---------------------------------------------------------------------------
# Governance models
# ---------------------------------------------------------------------------


class GovernanceAuditLogModel(Base):
    """Tamper-evident audit log of governance / admin actions.

    Each row carries a SHA-256 hash chain (``prev_hash`` + this row) so
    that any tampering with historical entries breaks subsequent
    verifications.
    """
    __tablename__ = "governance_audit_log"

    id = Column(Integer, Identity(), primary_key=True)
    timestamp = Column(DateTime, default=utcnow, nullable=False, index=True)
    actor = Column(String, nullable=True, index=True)  # admin token fingerprint or api key id
    actor_role = Column(String, nullable=True)
    action = Column(String, nullable=False, index=True)  # e.g. "config.update", "policy.create"
    resource_type = Column(String, nullable=True, index=True)
    resource_id = Column(String, nullable=True)
    request_id = Column(String, nullable=True, index=True)
    client_ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
    outcome = Column(String, nullable=False, index=True)  # "success" | "failure"
    before = Column(JSON, nullable=True)
    after = Column(JSON, nullable=True)
    audit_metadata = Column("metadata", JSON, nullable=True)
    prev_hash = Column(String, nullable=True)
    entry_hash = Column(String, nullable=False, index=True)


class AdminRoleModel(Base):
    """Map admin tokens to roles for RBAC.

    Tokens are stored as SHA-256 hashes — never raw. The `name` is a
    human label.
    """
    __tablename__ = "admin_roles"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    token_hash = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    role = Column(String, nullable=False, index=True)  # super_admin|operator|viewer|auditor
    created_at = Column(DateTime, default=utcnow, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)


class PolicyModel(Base):
    """Declarative policies enforced before forwarding requests.

    The policy ``conditions`` and ``action`` are JSON blobs; see
    ``app.governance`` for the schema and evaluator.
    """
    __tablename__ = "governance_policies"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False, index=True, unique=True)
    description = Column(Text, nullable=True)
    priority = Column(Integer, default=100, nullable=False, index=True)
    enabled = Column(Boolean, default=True, nullable=False)
    conditions = Column(JSON, default=dict, nullable=False)
    action = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class IPRuleModel(Base):
    """Per-scope IP allow/deny rules."""
    __tablename__ = "governance_ip_rules"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    cidr = Column(String, nullable=False, index=True)  # e.g. "10.0.0.0/8" or "1.2.3.4"
    action = Column(String, nullable=False)  # "allow" | "deny"
    scope = Column(String, nullable=False, index=True)  # "global" | "tenant" | "api_key"
    scope_id = Column(String, nullable=True, index=True)
    description = Column(Text, nullable=True)
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class OpenAPIRegistrationModel(Base):
    """Registered OpenAPI 3.x document for MCP tool / A2A skill synthesis."""

    __tablename__ = "openapi_registrations"

    id = Column(String, primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    openapi_document = Column(JSON, nullable=False)
    upstream_base_url = Column(String, nullable=False)
    path_prefix = Column(String, nullable=True, default="")
    expose_mcp_tools = Column(Boolean, default=True, nullable=False)
    expose_a2a_skills = Column(Boolean, default=True, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
