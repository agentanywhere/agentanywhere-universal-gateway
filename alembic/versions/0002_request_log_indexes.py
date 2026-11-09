"""Request-log indexes: fewer to maintain, and the right ones for the console's queries.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-10 00:00:00.000000

``request_logs`` is written on every request and read by the console every few seconds.
Measured on one million rows, with five single-column indexes:

* inserts ran at 12.7k rows/s; without the unused ``tenant_id`` index and the one on
  ``status_code`` that the new composite replaces, they ran several times faster;
* the dashboard statistics for the last hour took 135 ms (a scan of the time index and then
  the table), and a 4xx/5xx filter took 141 ms (a scan and a sort).

Upgrade drops ``ix_request_logs_tenant_id`` (never populated), ``ix_request_logs_status_code``
and ``ix_request_logs_timestamp`` and creates two composite indexes:

* ``(status_code, timestamp)`` for level filters, newest first;
* ``(timestamp, status_code, response_time_ms)``, which covers the statistics query and also
  serves time-range queries, so the old single-column time index is not needed.

Only indexes change; no row is touched. Each step checks what exists first, so the revision is
safe on a database created by ``init_db()`` and safe to run twice. ``downgrade`` restores the
three single-column indexes and drops the two composites.
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLE = "request_logs"
OLD = {
    "ix_request_logs_tenant_id": ["tenant_id"],
    "ix_request_logs_status_code": ["status_code"],
    "ix_request_logs_timestamp": ["timestamp"],
}
NEW = {
    "ix_request_logs_status_code_timestamp": ["status_code", "timestamp"],
    "ix_request_logs_timestamp_status_code_response_time_ms": ["timestamp", "status_code", "response_time_ms"],
}


def _existing() -> set:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in inspector.get_table_names():
        return set()
    return {ix["name"] for ix in inspector.get_indexes(TABLE)}


def _swap(drop: dict, create: dict) -> None:
    if op.get_context().as_sql:                       # offline mode: emit the statements
        for name in drop:
            op.drop_index(name, table_name=TABLE)
        for name, cols in create.items():
            op.create_index(name, TABLE, cols)
        return
    present = _existing()
    if not present and TABLE not in sa.inspect(op.get_bind()).get_table_names():
        return
    for name in drop:
        if name in present:
            op.drop_index(name, table_name=TABLE)
    present = _existing()
    for name, cols in create.items():
        if name not in present:
            op.create_index(name, TABLE, cols)


def upgrade() -> None:
    _swap(OLD, NEW)


def downgrade() -> None:
    _swap(NEW, OLD)
