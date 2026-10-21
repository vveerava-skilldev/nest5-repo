"""v7.5: expense payment source

One additive nullable column, expenses.payment_source: 'flat' (an owner advance), 'society' (paid from the
society account) or empty. Every expense that has a paying flat is set to 'flat'; expenses with no flat are
left empty on purpose (the admin confirms them in the app). No amount, settlement or balance is touched.
Idempotent: the startup add_missing_columns() safety net may already have added the column, and the
backfill only writes rows that are still empty.

Revision ID: d6a3f9b1c428
Revises: b4e7c2a9d531
Create Date: 2026-10-20 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6a3f9b1c428"
down_revision: Union[str, None] = "b4e7c2a9d531"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _have() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("expenses")}


def _can_drop_in_place() -> bool:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return True
    version = bind.exec_driver_sql("select sqlite_version()").scalar() or "0"
    return tuple(int(x) for x in version.split(".")[:2]) >= (3, 35)


def upgrade() -> None:
    if "payment_source" not in _have():
        op.add_column("expenses", sa.Column("payment_source", sa.String(length=20), nullable=True))
    op.execute(
        "UPDATE expenses SET payment_source = 'flat' "
        "WHERE paid_by_flat_id IS NOT NULL AND (payment_source IS NULL OR payment_source = '')"
    )


def downgrade() -> None:
    # Only the new column goes; amounts, flats and settlements stay exactly as they are.
    if "payment_source" not in _have():
        return
    if _can_drop_in_place():
        # A plain ALTER keeps the expenses table (and the settlements pointing at it) untouched.
        op.drop_column("expenses", "payment_source")
    else:
        with op.batch_alter_table("expenses") as batch:
            batch.drop_column("payment_source")
