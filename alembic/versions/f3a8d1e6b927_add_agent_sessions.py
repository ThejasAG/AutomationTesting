"""add agent_sessions: the AI Agent's Claude Code sessions

Revision ID: f3a8d1e6b927
Revises: e7b2c9d4a815
Create Date: 2026-10-22

New table only; nothing existing changes. Skipped when already present (a
database whose create_all ran first — the backend and the agent runner both call
initialize_database()).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3a8d1e6b927"
down_revision: Union[str, Sequence[str], None] = "e7b2c9d4a815"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    if "agent_sessions" in have:
        return
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("trigger", sa.String(20), nullable=True),
        sa.Column("status", sa.String(20), nullable=True),
        sa.Column("phase", sa.String(300), nullable=True),
        sa.Column("options", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("claude_session_id", sa.String(36), nullable=True),
        sa.Column("base_commit", sa.String(40), nullable=True),
        sa.Column("branch", sa.String(200), nullable=True),
        sa.Column("worktree", sa.String(500), nullable=True),
        sa.Column("files_changed", sa.JSON(), nullable=True),
        sa.Column("gates", sa.JSON(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("stop_requested", sa.Boolean(), nullable=True),
        sa.Column("decision_by", sa.String(120), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("report_sent", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("agent_sessions")
