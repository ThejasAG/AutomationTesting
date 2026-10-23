"""repair double-encoded saved_scenarios.steps/covers

Revision ID: 9c1f4b2ad70e
Revises: 84af52af343c
Create Date: 2026-09-05

DATA ONLY — no schema change.

scripts/platform_seed.py json.dumps()'d into a JSON column, so SQLAlchemy
encoded an already-encoded string. Affected rows hold '"[\"open app\", …]"'
instead of ["open app", …]. Loaded through the ORM those came back as `str`,
and iterating a string yields characters, so a 2-step scenario became 20
single-character steps.

The decode is exactly ONE level and only commits when the result is a list: a
legitimate list may itself contain a string that looks like JSON, and unwrapping
that would destroy real data. Rows already holding a list are skipped, which
makes the migration idempotent.

NULL `covers` are deliberately left alone — they are not corrupt. to_dict() and
every consumer coerce with `or []`, and StringList now reads them as [].
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9c1f4b2ad70e"
down_revision: Union[str, Sequence[str], None] = "84af52af343c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FIELDS = ("steps", "covers")


def _repairs(conn):
    """[(id, field, old_raw, new_raw)] for every row needing a one-level decode."""
    out = []
    rows = conn.execute(sa.text(
        "select id, steps, covers from saved_scenarios")).fetchall()
    for row in rows:
        for i, field in enumerate(_FIELDS, start=1):
            raw = row[i]
            if raw is None:
                continue
            # Normalise to the DOCUMENT the column holds, whatever the driver
            # handed us. SQLite returns the raw JSON text, so it needs decoding;
            # psycopg2 has already decoded it, so the value IS the document.
            # Getting this wrong makes the migration a silent no-op on one engine.
            if conn.dialect.name == "sqlite" and isinstance(raw, (str, bytes)):
                try:
                    value = json.loads(raw)
                except (ValueError, TypeError):
                    continue      # not JSON at all — leave it for a human
            else:
                value = raw       # already the decoded document

            # Corruption means the document IS a string (a JSON array encoded
            # twice). A document that is already a list is canonical.
            if not isinstance(value, str):
                continue
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                continue          # a genuine string that is not JSON
            if isinstance(decoded, list):
                out.append((row[0], field, raw, json.dumps(decoded)))
    return out


def upgrade() -> None:
    conn = op.get_bind()
    # PostgreSQL will not implicitly coerce a text parameter into a json column,
    # so bind through the JSON type rather than as a bare string. SQLite accepts
    # either, so one statement serves both.
    stmt_type = sa.JSON()
    for row_id, field, _old, new in _repairs(conn):
        conn.execute(
            sa.text(f"update saved_scenarios set {field} = :v where id = :i").bindparams(
                sa.bindparam("v", type_=stmt_type)),
            {"v": json.loads(new), "i": row_id})


def downgrade() -> None:
    """Deliberately not reversible.

    Re-encoding would restore a defect, and the pre-repair value is recoverable
    from the backup taken before this ran. A downgrade that recreates corruption
    is worse than none.
    """
    pass
