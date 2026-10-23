"""add the six indexes the models declare but production never got

Revision ID: b3e21c07f4d9
Revises: 9c1f4b2ad70e
Create Date: 2026-09-05

INDEX-ONLY. No columns, no constraints, no data.

Production's tables were built by Base.metadata.create_all() and then evolved by
_add_missing_columns(), which adds columns and nothing else — so every index
declared on a model after a table already existed was never created. Phase 4G.5.0
confirmed none of these six exists under another name.

Engine-portable: op.create_index()/op.drop_index() emit the right DDL for SQLite
and PostgreSQL alike, and index creation is additive on both — no table rewrite.
The reconciliation that DOES rewrite test_runs (foreign keys, nullability, types)
is deliberately held back for 4G.5B.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b3e21c07f4d9"
down_revision: Union[str, Sequence[str], None] = "9c1f4b2ad70e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: (index name, table, [columns]) — exactly what the models declare.
INDEXES = (
    ("ix_devices_reserved_by", "devices", ["reserved_by"]),
    ("ix_execution_agents_agent_credential_hash", "execution_agents",
     ["agent_credential_hash"]),
    ("ix_test_runs_id", "test_runs", ["id"]),
    ("ix_test_runs_machine_id", "test_runs", ["machine_id"]),
    ("ix_test_runs_test_name", "test_runs", ["test_name"]),
    ("ix_test_runs_test_suite", "test_runs", ["test_suite"]),
)


def _existing(conn, table):
    from sqlalchemy import inspect
    return {i["name"] for i in inspect(conn).get_indexes(table)}


def upgrade() -> None:
    conn = op.get_bind()
    for name, table, cols in INDEXES:
        # Idempotent: a database that already has one is left alone, so this
        # revision is safe to re-run and safe on a fresh create_all() database.
        if name not in _existing(conn, table):
            op.create_index(name, table, cols, unique=False)


def downgrade() -> None:
    conn = op.get_bind()
    for name, table, _cols in reversed(INDEXES):
        if name in _existing(conn, table):
            op.drop_index(name, table_name=table)
