"""Multi-tenant tables

Revision ID: 002
Revises: 001
Create Date: 2026-05-11 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("quota", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id"),
    )
    op.create_index(op.f("ix_tenants_tenant_id"), "tenants", ["tenant_id"])

    op.create_table(
        "tenant_api_keys",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("api_key_id", sa.String(), nullable=False),
        sa.Column("api_key_hash", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_keys.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_tenant_api_keys_tenant_id"), "tenant_api_keys", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_tenant_api_keys_api_key_id"), "tenant_api_keys", ["api_key_id"]
    )
    op.create_index(
        op.f("ix_tenant_api_keys_api_key_hash"), "tenant_api_keys", ["api_key_hash"]
    )

    op.create_table(
        "tenant_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("window", sa.String(), nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes_out", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_tenant_usage_tenant_id"), "tenant_usage", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_tenant_usage_window"), "tenant_usage", ["window"]
    )
    op.create_index(
        op.f("ix_tenant_usage_window_start"), "tenant_usage", ["window_start"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tenant_usage_window_start"), table_name="tenant_usage")
    op.drop_index(op.f("ix_tenant_usage_window"), table_name="tenant_usage")
    op.drop_index(op.f("ix_tenant_usage_tenant_id"), table_name="tenant_usage")
    op.drop_table("tenant_usage")
    op.drop_index(
        op.f("ix_tenant_api_keys_api_key_hash"), table_name="tenant_api_keys"
    )
    op.drop_index(
        op.f("ix_tenant_api_keys_api_key_id"), table_name="tenant_api_keys"
    )
    op.drop_index(
        op.f("ix_tenant_api_keys_tenant_id"), table_name="tenant_api_keys"
    )
    op.drop_table("tenant_api_keys")
    op.drop_index(op.f("ix_tenants_tenant_id"), table_name="tenants")
    op.drop_table("tenants")
