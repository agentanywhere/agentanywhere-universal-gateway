"""Initial schema

Revision ID: 001
Revises: 
Create Date: 2024-01-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # API Keys table
    op.create_table(
        'api_keys',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('key_hash', sa.String(), nullable=False),
        sa.Column('rate_limit_per_minute', sa.Integer(), nullable=False),
        sa.Column('rate_limit_per_hour', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('key_metadata', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name')
    )
    op.create_index(op.f('ix_api_keys_name'), 'api_keys', ['name'], unique=True)

    # Request Logs table
    op.create_table(
        'request_logs',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('request_id', sa.String(), nullable=False),
        sa.Column('method', sa.String(), nullable=False),
        sa.Column('path', sa.String(), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=False),
        sa.Column('response_time_ms', sa.Float(), nullable=False),
        sa.Column('client_ip', sa.String(), nullable=True),
        sa.Column('api_key_id', sa.String(), nullable=True),
        sa.Column('protocol', sa.String(), nullable=True),
        sa.Column('backend_url', sa.String(), nullable=True),
        sa.Column('error_message', sa.String(), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_request_logs_request_id'), 'request_logs', ['request_id'], unique=True)
    op.create_index(op.f('ix_request_logs_created_at'), 'request_logs', ['created_at'], unique=False)
    op.create_index(op.f('ix_request_logs_api_key_id'), 'request_logs', ['api_key_id'], unique=False)

    # Settings Profiles table
    op.create_table(
        'settings_profiles',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name')
    )

    # Protocol Settings table
    op.create_table(
        'protocol_settings',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('profile_id', sa.String(), nullable=False),
        sa.Column('protocol', sa.String(), nullable=False),
        sa.Column('backend_url', sa.String(), nullable=True),
        sa.Column('timeout', sa.Integer(), nullable=False),
        sa.Column('rate_limit_per_minute', sa.Integer(), nullable=False),
        sa.Column('rate_limit_per_hour', sa.Integer(), nullable=False),
        sa.Column('circuit_breaker_enabled', sa.Boolean(), nullable=False),
        sa.Column('circuit_breaker_failure_threshold', sa.Integer(), nullable=False),
        sa.Column('circuit_breaker_timeout', sa.Integer(), nullable=False),
        sa.Column('mqtt_qos', sa.Integer(), nullable=True),
        sa.Column('mqtt_keep_alive', sa.Integer(), nullable=True),
        sa.Column('websocket_max_connections', sa.Integer(), nullable=True),
        sa.Column('grpc_compression', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['profile_id'], ['settings_profiles.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_protocol_settings_profile_id'), 'protocol_settings', ['profile_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_protocol_settings_profile_id'), table_name='protocol_settings')
    op.drop_table('protocol_settings')
    op.drop_table('settings_profiles')
    op.drop_index(op.f('ix_request_logs_api_key_id'), table_name='request_logs')
    op.drop_index(op.f('ix_request_logs_created_at'), table_name='request_logs')
    op.drop_index(op.f('ix_request_logs_request_id'), table_name='request_logs')
    op.drop_table('request_logs')
    op.drop_index(op.f('ix_api_keys_name'), table_name='api_keys')
    op.drop_table('api_keys')


