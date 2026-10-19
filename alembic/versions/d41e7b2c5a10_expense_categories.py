"""user-added expense categories

Revision ID: d41e7b2c5a10
Revises: c8e2a1f903b2
Create Date: 2026-10-09 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d41e7b2c5a10"
down_revision: Union[str, None] = "c8e2a1f903b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # app.main runs create_all on startup, so the table may already exist.
    if sa.inspect(op.get_bind()).has_table("expense_categories"):
        return
    op.create_table(
        "expense_categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("expense_categories")
