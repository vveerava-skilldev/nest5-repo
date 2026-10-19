"""app settings: admin switches (owner building-wide payments view)

Revision ID: d3a1c5e7f902
Revises: b5d7f1e3c920
Create Date: 2026-10-10 06:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d3a1c5e7f902"
down_revision: Union[str, None] = "b5d7f1e3c920"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "app_settings" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sa.String(80), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
