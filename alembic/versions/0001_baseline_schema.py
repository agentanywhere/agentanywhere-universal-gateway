"""Baseline: the full gateway schema.

Revision ID: 0001
Revises:
Create Date: 2026-10-20 00:00:00.000000

This one revision describes every table in ``app/db_models.py`` as of this
release. It replaces the two earlier migrations (``001`` and ``002``), which
described a different, older schema and never matched the models; see
docs/UPGRADING.md.

The tables below are a frozen copy of the models, not an import of them, so
that this revision keeps meaning the same thing when the models change. A
later schema change needs a new revision; ``tests/test_migrations.py`` fails
when the models and the migrations disagree.

The upgrade is safe to run on a database the gateway has already created with
``init_db()`` (``Base.metadata.create_all``):

* a table that does not exist is created, with its indexes;
* a table that exists is left in place; only columns and indexes that are
  missing from it are added. A missing NOT NULL column without a server
  default is added as nullable, because existing rows have no value for it.

Nothing is dropped or altered on upgrade. ``downgrade`` drops every table this
revision manages, data included.
"""
import logging

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.schema import CreateIndex, CreateTable

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")


def _schema() -> sa.MetaData:
    m = sa.MetaData()

    sa.Table(
        "api_keys", m,
        sa.Column("id", sa.String(), primary_key=True, index=True),
        sa.Column("key", sa.String(), unique=True, index=True, nullable=True),
        sa.Column("key_hash", sa.String(), unique=True, index=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer()),
        sa.Column("rate_limit_per_hour", sa.Integer()),
        sa.Column("allowed_paths", sa.JSON()),
        sa.Column("key_metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean()),
    )

    sa.Table(
        "routes", m,
        sa.Column("id", sa.String(), primary_key=True, index=True),
        sa.Column("path", sa.String(), nullable=False, index=True),
        sa.Column("backend_url", sa.String(), nullable=False),
        sa.Column("protocol", sa.String(), nullable=False),
        sa.Column("timeout", sa.Integer()),
        sa.Column("rate_limit", sa.Integer(), nullable=True),
        sa.Column("requires_auth", sa.Boolean()),
        sa.Column("headers", sa.JSON()),
        sa.Column("rewrite_path", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean()),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
    )

    sa.Table(
        "agent_registry", m,
        sa.Column("slug", sa.String(), primary_key=True, index=True),
        sa.Column("upstream_base", sa.String(), nullable=False),
        sa.Column("audience", sa.String(), nullable=True),
        sa.Column("public_paths", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("runtime_mode", sa.String(), nullable=False),
        sa.Column("data_residency", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=True),
    )

    sa.Table(
        "tenants", m,
        sa.Column("tenant_id", sa.String(), primary_key=True, index=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("config", sa.JSON()),
        sa.Column("quota", sa.JSON()),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime()),
    )

    sa.Table(
        "request_logs", m,
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("request_id", sa.String(), index=True, nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False, index=True),
        sa.Column("client_ip", sa.String(), nullable=False),
        sa.Column("api_key_id", sa.String(), sa.ForeignKey("api_keys.id"), nullable=True),
        sa.Column(
            "tenant_id", sa.String(),
            sa.ForeignKey("tenants.tenant_id", ondelete="SET NULL"),
            nullable=True, index=True,
        ),
        sa.Column("status_code", sa.Integer(), nullable=False, index=True),
        sa.Column("response_time_ms", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), index=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("protocol", sa.String(), nullable=True),
    )

    sa.Table(
        "settings_profiles", m,
        sa.Column("id", sa.String(), primary_key=True, index=True),
        sa.Column("name", sa.String(), nullable=False, unique=True, index=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("protocol_settings", sa.JSON()),
        sa.Column("global_settings", sa.JSON()),
        sa.Column("is_default", sa.Boolean()),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("created_by", sa.String(), nullable=True),
    )

    sa.Table(
        "tenant_api_keys", m,
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column(
            "tenant_id", sa.String(),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column(
            "api_key_id", sa.String(),
            sa.ForeignKey("api_keys.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("api_key_hash", sa.String(), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    sa.Table(
        "tenant_usage", m,
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column(
            "tenant_id", sa.String(),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("window", sa.String(), nullable=False, index=True),
        sa.Column("window_start", sa.DateTime(), nullable=False, index=True),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("bytes_in", sa.Integer(), nullable=False),
        sa.Column("bytes_out", sa.Integer(), nullable=False),
    )

    sa.Table(
        "governance_audit_log", m,
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False, index=True),
        sa.Column("actor", sa.String(), nullable=True, index=True),
        sa.Column("actor_role", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False, index=True),
        sa.Column("resource_type", sa.String(), nullable=True, index=True),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("request_id", sa.String(), nullable=True, index=True),
        sa.Column("client_ip", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False, index=True),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("prev_hash", sa.String(), nullable=True),
        sa.Column("entry_hash", sa.String(), nullable=False, index=True),
    )

    sa.Table(
        "admin_roles", m,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("token_hash", sa.String(), unique=True, index=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
    )

    sa.Table(
        "governance_policies", m,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, index=True, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, index=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("action", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime()),
    )

    sa.Table(
        "governance_ip_rules", m,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("cidr", sa.String(), nullable=False, index=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False, index=True),
        sa.Column("scope_id", sa.String(), nullable=True, index=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    sa.Table(
        "openapi_registrations", m,
        sa.Column("id", sa.String(), primary_key=True, index=True),
        sa.Column("name", sa.String(), unique=True, nullable=False, index=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("openapi_document", sa.JSON(), nullable=False),
        sa.Column("upstream_base_url", sa.String(), nullable=False),
        sa.Column("path_prefix", sa.String(), nullable=True),
        sa.Column("expose_mcp_tools", sa.Boolean(), nullable=False),
        sa.Column("expose_a2a_skills", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime()),
    )

    return m


def _add_missing_column(table: sa.Table, column: sa.Column, dialect: str) -> None:
    nullable = column.nullable
    if not nullable and column.server_default is None:
        # Existing rows would have no value: add it as nullable and say so.
        nullable = True
        log.warning(
            "Column %s.%s is NOT NULL in the models but was missing; added as "
            "nullable because the table already has rows. Fill it in, then "
            "make it NOT NULL in a later revision if you need the constraint.",
            table.name, column.name,
        )
    op.add_column(table.name, sa.Column(column.name, column.type, nullable=nullable))
    if dialect != "sqlite":
        # SQLite cannot add a foreign key to an existing table; the column
        # works without it there.
        for fk in column.foreign_keys:
            op.create_foreign_key(
                None, table.name, fk.column.table.name,
                [column.name], [fk.column.name], ondelete=fk.ondelete,
            )


def upgrade() -> None:
    schema = _schema()

    if context.is_offline_mode():
        # No database to inspect: emit the full schema.
        for table in schema.sorted_tables:
            op.execute(CreateTable(table))
            for index in sorted(table.indexes, key=lambda i: i.name):
                op.execute(CreateIndex(index))
        return

    bind = op.get_bind()
    dialect = bind.dialect.name
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    for table in schema.sorted_tables:
        if table.name not in existing_tables:
            table.create(bind)  # also creates its indexes
            continue

        existing_columns = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in existing_columns:
                _add_missing_column(table, column, dialect)

        existing_indexes = {i["name"] for i in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name not in existing_indexes:
                op.create_index(
                    index.name, table.name,
                    [c.name for c in index.columns], unique=index.unique,
                )


def downgrade() -> None:
    schema = _schema()
    if context.is_offline_mode():
        for table in reversed(schema.sorted_tables):
            op.drop_table(table.name)
        return
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table in reversed(schema.sorted_tables):
        if table.name in existing:
            op.drop_table(table.name)
