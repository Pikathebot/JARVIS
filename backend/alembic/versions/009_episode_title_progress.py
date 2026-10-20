"""Episodes remember how far their title reaches (PLAN 4.18): the open topic is named by the model
after its first reply and renamed as the conversation moves on; ``title_upto_message_id`` is the last
message the current title was written from.

Revision ID: 009
Revises: 008
Create Date: 2026-10-20 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("episodes")}
    if "title_upto_message_id" not in columns:
        op.add_column("episodes", sa.Column("title_upto_message_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("episodes", "title_upto_message_id")
