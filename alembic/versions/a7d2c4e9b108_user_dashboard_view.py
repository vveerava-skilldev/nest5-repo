"""per-user default dashboard view (simple / detailed)

Revision ID: a7d2c4e9b108
Revises: f2c6a8d4b301
Create Date: 2026-10-11 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7d2c4e9b108"
down_revision: Union[str, None] = "f2c6a8d4b301"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column() -> bool:
    return any(c["name"] == "dashboard_view" for c in sa.inspect(op.get_bind()).get_columns("users"))


def upgrade() -> None:
    # The app's startup safety net may already have added the column.
    if _has_column():
        return
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("dashboard_view", sa.String(length=10), nullable=True))


def downgrade() -> None:
    if not _has_column():
        return
    with op.batch_alter_table("users") as batch:
        batch.drop_column("dashboard_view")
