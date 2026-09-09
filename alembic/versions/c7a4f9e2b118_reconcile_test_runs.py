"""reconcile test_runs with the models: foreign keys, nullability, types

Revision ID: c7a4f9e2b118
Revises: b3e21c07f4d9
Create Date: 2026-09-05

Closes the last of the drift left by create_all() + _add_missing_columns(),
which adds columns but never constraints or type changes.

  * three foreign keys, named so SQLite batch mode can create and drop them
  * three columns LOOSENED from NOT NULL to nullable (the models' definition)
  * timeline and error_message become String, matching the models

`flaky_score` is deliberately RETAINED. It is dead (0 non-null values in 158
rows, no reference anywhere in the platform) but dropping it is the only
irreversible act available here, and it costs nothing to keep.

timeline matters more than it looks. jobs.py does json.loads(job.timeline) and
json.dumps() on the way in — the application marshals JSON by hand. As a `json`
column on PostgreSQL psycopg2 would decode it first, and json.loads(list) raises
TypeError. On SQLite the two are indistinguishable, so only String is correct on
both engines.

SQLite cannot ALTER a constraint, a column's nullability or its type, so those
run inside batch_alter_table, which rewrites the table: new table, copy every
row, drop the original, rename. PostgreSQL supports all of it natively and is
given plain ALTERs instead.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7a4f9e2b118"
down_revision: Union[str, Sequence[str], None] = "b3e21c07f4d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: (constraint name, table, [local cols], referred table, [remote cols])
#: No ondelete/onupdate: the models declare none, and inventing cascade
#: behaviour here would silently change what a delete does.
FOREIGN_KEYS = (
    ("fk_test_runs_project_id_test_projects", "test_runs",
     ["project_id"], "test_projects", ["id"]),
    ("fk_test_runs_agent_id_execution_agents", "test_runs",
     ["agent_id"], "execution_agents", ["id"]),
    ("fk_test_projects_group_id_application_groups", "test_projects",
     ["group_id"], "application_groups", ["id"]),
)

#: column -> (existing type, model type). All become unbounded VARCHAR.
NULLABLE_NOW = (
    ("test_suite", sa.VARCHAR(length=255)),
    ("test_name", sa.VARCHAR(length=255)),
    ("status", sa.VARCHAR(length=50)),
)


def _existing_fks(conn, table):
    from sqlalchemy import inspect
    return {f["name"] for f in inspect(conn).get_foreign_keys(table) if f.get("name")}


def _nullable(conn, table, column):
    from sqlalchemy import inspect
    for c in inspect(conn).get_columns(table):
        if c["name"] == column:
            return c["nullable"]
    return None


def upgrade() -> None:
    """Reconcile only what is actually still drifted.

    Every step is conditional. A database built fresh from this history already
    got the foreign keys and the model types from 84af52af343c, so re-applying
    them would fail with "constraint already exists"; only a database that grew
    via create_all() needs this work. That makes the revision correct on both a
    fresh build and the drifted production database, and idempotent on either.
    """
    conn = op.get_bind()
    have = _existing_fks(conn, "test_runs")
    needs_fk = [f for f in FOREIGN_KEYS if f[1] == "test_runs" and f[0] not in have]
    needs_null = [(n, t) for n, t in NULLABLE_NOW if _nullable(conn, "test_runs", n) is False]

    if needs_fk or needs_null:
        with op.batch_alter_table("test_runs", schema=None) as batch_op:
            for name, existing in needs_null:
                batch_op.alter_column(name, existing_type=existing,
                                      type_=sa.String(), nullable=True)
            if needs_null:
                # Only meaningful on the drifted database; a fresh one already
                # carries the model types from the baseline revision.
                batch_op.alter_column("timeline", existing_type=sa.JSON(),
                                      type_=sa.String(), existing_nullable=True)
                batch_op.alter_column("error_message", existing_type=sa.TEXT(),
                                      type_=sa.String(), existing_nullable=True)
            for name, _t, local, ref, remote in needs_fk:
                batch_op.create_foreign_key(name, ref, local, remote)

    have_p = _existing_fks(conn, "test_projects")
    needs_p = [f for f in FOREIGN_KEYS if f[1] == "test_projects" and f[0] not in have_p]
    if needs_p:
        with op.batch_alter_table("test_projects", schema=None) as batch_op:
            for name, _t, local, ref, remote in needs_p:
                batch_op.create_foreign_key(name, ref, local, remote)


def downgrade() -> None:
    """Restore the pre-migration shape as closely as it can be restored.

    LIMITATION, stated rather than papered over: the original columns were
    VARCHAR(255)/VARCHAR(50)/TEXT/JSON, and this puts those declarations back —
    but a value written while the column was unbounded could exceed the restored
    length. SQLite does not enforce VARCHAR length, so no data is lost on SQLite;
    on PostgreSQL an over-long value would make the downgrade fail rather than
    truncate, which is the safer failure.

    `flaky_score` is untouched in both directions because upgrade() never drops
    it, so there is no historical state for downgrade to recreate.
    """
    conn = op.get_bind()
    have_p = _existing_fks(conn, "test_projects")
    with op.batch_alter_table("test_projects", schema=None) as batch_op:
        for name, table, _l, _r, _rc in FOREIGN_KEYS:
            if table == "test_projects" and name in have_p:
                batch_op.drop_constraint(name, type_="foreignkey")

    have = _existing_fks(conn, "test_runs")
    with op.batch_alter_table("test_runs", schema=None) as batch_op:
        for name, table, _l, _r, _rc in FOREIGN_KEYS:
            if table == "test_runs" and name in have:
                batch_op.drop_constraint(name, type_="foreignkey")
        batch_op.alter_column("error_message", existing_type=sa.String(),
                              type_=sa.TEXT(), existing_nullable=True)
        batch_op.alter_column("timeline", existing_type=sa.String(),
                              type_=sa.JSON(), existing_nullable=True)
        for name, original in reversed(NULLABLE_NOW):
            batch_op.alter_column(name, existing_type=sa.String(),
                                  type_=original, nullable=False)
