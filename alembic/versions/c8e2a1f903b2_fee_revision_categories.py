"""maintenance fee revisions + expense category labels

Revision ID: c8e2a1f903b2
Revises: 10551f50ae9d
Create Date: 2026-10-09 10:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8e2a1f903b2"
down_revision: Union[str, None] = "10551f50ae9d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    # app.main runs create_all on startup, so the table may already exist.
    if not sa.inspect(conn).has_table("maintenance_fee_revisions"):
        _create_fee_revisions()
    _remap_legacy_categories(conn)


def _create_fee_revisions() -> None:
    op.create_table(
        "maintenance_fee_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_maintenance_fee_revisions_effective_date"),
        "maintenance_fee_revisions",
        ["effective_date"],
        unique=False,
    )


def _remap_legacy_categories(conn) -> None:
    conn.execute(
        sa.text(
            "UPDATE expenses SET category = 'Common' WHERE category IN ('general', 'misc')"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE expenses SET category = 'Watchman Salary' WHERE category = 'salary'"
        )
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_maintenance_fee_revisions_effective_date"),
        table_name="maintenance_fee_revisions",
    )
    op.drop_table("maintenance_fee_revisions")
