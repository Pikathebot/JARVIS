"""Add the episodes table for the one continuing Freeform thread (PLAN 4.18).

Revision ID: 007
Revises: 006
Create Date: 2026-10-20 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "episodes" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "episodes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.String(), sa.ForeignKey("sessions.session_id"), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("ended_at", sa.Float(), nullable=True),
        sa.Column("start_after_message_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fold_upto_message_id", sa.Integer(), nullable=True),
        sa.Column("fold_note", sa.Text(), nullable=True),
        sa.Column("digest", sa.Text(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("decisions", sa.Text(), nullable=True),
        sa.Column("open_items", sa.Text(), nullable=True),
        sa.Column("record_status", sa.String(), nullable=False, server_default="open"),
    )
    op.create_index("ix_episodes_session_id", "episodes", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_episodes_session_id", table_name="episodes")
    op.drop_table("episodes")
