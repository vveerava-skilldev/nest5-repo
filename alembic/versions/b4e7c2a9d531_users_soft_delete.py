"""v7.3 (in place): soft-deleted users

Two additive nullable columns on users: deleted_at and deleted_by. A "deleted" user keeps its row,
its flat links and every record that points at it; it is also is_active = false, so it cannot log in.
Idempotent: the startup add_missing_columns() safety net may already have added the columns.

Revision ID: b4e7c2a9d531
Revises: a3d9e6c1b852
Create Date: 2026-10-19 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4e7c2a9d531"
down_revision: Union[str, None] = "a3d9e6c1b852"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    ("deleted_at", sa.DateTime()),
    ("deleted_by", sa.String(length=80)),
)


def _have() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}


def upgrade() -> None:
    have = _have()
    for name, type_ in COLUMNS:
        if name not in have:
            op.add_column("users", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    # Soft-deleted users stay disabled (is_active is already false); only the deletion marks go.
    have = _have()
    drop = [name for name, _ in COLUMNS if name in have]
    if drop:
        with op.batch_alter_table("users") as batch:
            for name in drop:
                batch.drop_column(name)
