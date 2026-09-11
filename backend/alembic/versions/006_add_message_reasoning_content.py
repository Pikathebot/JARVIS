"""Add reasoning_content column to messages, for structurally separated thinking-block storage.

Revision ID: 006
Revises: 005
Create Date: 2026-09-07 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "messages" in inspector.get_table_names():
        columns = [col["name"] for col in inspector.get_columns("messages")]
        with op.batch_alter_table("messages") as batch_op:
            if "reasoning_content" not in columns:
                batch_op.add_column(sa.Column("reasoning_content", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_column("reasoning_content")
