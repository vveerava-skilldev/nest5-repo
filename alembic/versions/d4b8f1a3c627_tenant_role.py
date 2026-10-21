"""tenant role: per-user list of payment sections a tenant may see

Revision ID: d4b8f1a3c627
Revises: c9a2e5f7b140
Create Date: 2026-10-19 08:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4b8f1a3c627"
down_revision: Union[str, None] = "c9a2e5f7b140"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column() -> bool:
    return any(c["name"] == "tenant_payments" for c in sa.inspect(op.get_bind()).get_columns("users"))


def upgrade() -> None:
    # The startup safety net may already have added the column. No data is changed.
    if _has_column():
        return
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("tenant_payments", sa.String(length=80), nullable=True))


def downgrade() -> None:
    if not _has_column():
        return
    with op.batch_alter_table("users") as batch:
        batch.drop_column("tenant_payments")
