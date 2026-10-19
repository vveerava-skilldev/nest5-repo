"""expense status (approved / rejected) with reason, plus repair of rejected rows that still count

Revision ID: c9a2e5f7b140
Revises: b8e3f5a1d726
Create Date: 2026-10-16 07:00:00.000000

"""
from datetime import datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9a2e5f7b140"
down_revision: Union[str, None] = "b8e3f5a1d726"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    ("status", sa.String(20)),
    ("rejection_reason", sa.String(255)),
    ("rejected_at", sa.DateTime()),
    ("rejected_by", sa.String(80)),
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("expenses")}
    for name, coltype in COLUMNS:
        if name not in existing:
            op.add_column("expenses", sa.Column(name, coltype, nullable=True))
    # Idempotent data repair: a rejected expense must not stay settled / carry a live settlement.
    # Rows are never deleted; only their flags are corrected.
    bind.execute(sa.text("UPDATE expenses SET status = 'approved' WHERE status IS NULL"))
    bind.execute(
        sa.text(
            "UPDATE settlements SET reverted_at = :now WHERE reverted_at IS NULL "
            "AND expense_id IN (SELECT id FROM expenses WHERE status = 'rejected')"
        ),
        {"now": datetime.utcnow()},
    )
    bind.execute(
        sa.text("UPDATE expenses SET is_settled = :no WHERE status = 'rejected' AND is_settled = :yes"),
        {"no": False, "yes": True},
    )


def downgrade() -> None:
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("expenses")}
    # A rejected expense would count again once the status column is gone: soft-delete them
    # so a downgrade never inflates the owed-back totals.
    if "status" in existing:
        bind.execute(
            sa.text("UPDATE expenses SET deleted_at = :now WHERE status = 'rejected' AND deleted_at IS NULL"),
            {"now": datetime.utcnow()},
        )
    with op.batch_alter_table("expenses") as batch:
        for name, _ in reversed(COLUMNS):
            if name in existing:
                batch.drop_column(name)
