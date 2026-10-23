"""add mcp_servers: MCP servers connected to the platform (Settings → MCP servers)

Revision ID: d5e8a1c3f702
Revises: c7a4f9e2b118
Create Date: 2026-10-06

A new table only; nothing existing changes. Skipped when the table is already
there (a database whose create_all ran first).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5e8a1c3f702"
down_revision: Union[str, Sequence[str], None] = "c7a4f9e2b118"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "mcp_servers" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("transport", sa.String(10), nullable=False),
        sa.Column("command", sa.String(500), nullable=True),
        sa.Column("args", sa.JSON(), nullable=True),
        sa.Column("env", sa.JSON(), nullable=True),
        sa.Column("url", sa.String(500), nullable=True),
        sa.Column("headers", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column("drives_devices", sa.Boolean(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("mcp_servers")
