"""add agent tables: autonomous test agent (AI Agent page)

Revision ID: e7b2c9d4a815
Revises: d5e8a1c3f702
Create Date: 2026-10-06

New tables only; nothing existing changes. Each is skipped when already present
(a database whose create_all ran first).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7b2c9d4a815"
down_revision: Union[str, Sequence[str], None] = "d5e8a1c3f702"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "agent_settings" not in have:
        op.create_table(
            "agent_settings",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("data", sa.JSON(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
    if "agent_batches" not in have:
        op.create_table(
            "agent_batches",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("trigger", sa.String(20), nullable=True),
            sa.Column("env", sa.String(20), nullable=True),
            sa.Column("status", sa.String(20), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("totals", sa.JSON(), nullable=True),
            sa.Column("spend_usd", sa.Float(), nullable=True),
            sa.Column("report_sent", sa.Boolean(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
        )
    if "agent_items" not in have:
        op.create_table(
            "agent_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("batch_id", sa.String(36), sa.ForeignKey("agent_batches.id"), nullable=False),
            sa.Column("position", sa.Integer(), nullable=True),
            sa.Column("flow_id", sa.String(100), nullable=True),
            sa.Column("flow_name", sa.String(255), nullable=True),
            sa.Column("status", sa.String(20), nullable=True),
            sa.Column("run_id", sa.String(36), nullable=True),
            sa.Column("final_run_id", sa.String(36), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=True),
            sa.Column("category", sa.String(20), nullable=True),
            sa.Column("root_cause", sa.Text(), nullable=True),
            sa.Column("verdict", sa.JSON(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
        )
    if "agent_actions" not in have:
        op.create_table(
            "agent_actions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("batch_id", sa.String(36), sa.ForeignKey("agent_batches.id"), nullable=True),
            sa.Column("item_id", sa.String(36), sa.ForeignKey("agent_items.id"), nullable=True),
            sa.Column("run_id", sa.String(36), nullable=True),
            sa.Column("kind", sa.String(30), nullable=False),
            sa.Column("status", sa.String(20), nullable=True),
            sa.Column("title", sa.String(500), nullable=True),
            sa.Column("detail", sa.JSON(), nullable=True),
            sa.Column("cost_usd", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    op.drop_table("agent_actions")
    op.drop_table("agent_items")
    op.drop_table("agent_batches")
    op.drop_table("agent_settings")
