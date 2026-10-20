"""Full-text index over messages and a vector per episode record, for recall (PLAN 4.18 phase 3).

Revision ID: 008
Revises: 007
Create Date: 2026-10-20 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

from app.database.message_index import DROP, ensure_message_index

# revision identifiers, used by Alembic.
revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        ensure_message_index(bind)
    columns = [c["name"] for c in sa.inspect(bind).get_columns("episodes")]
    if "embedding" not in columns:
        with op.batch_alter_table("episodes") as batch_op:
            batch_op.add_column(sa.Column("embedding", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("episodes") as batch_op:
        batch_op.drop_column("embedding")
    for sql in DROP:
        op.execute(sql)
