"""user accounts, flat links, password reset tokens

Revision ID: e7b3c9d2f4a1
Revises: d41e7b2c5a10
Create Date: 2026-10-09 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7b3c9d2f4a1"
down_revision: Union[str, None] = "d41e7b2c5a10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # app.main runs create_all on startup, so the tables may already exist.
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("username", sa.String(length=80), nullable=False),
            sa.Column("display_name", sa.String(length=120), nullable=True),
            sa.Column("email", sa.String(length=255), nullable=True),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("role", sa.String(length=20), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("must_change_password", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("password_changed_at", sa.DateTime(), nullable=True),
            sa.Column("last_login_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_users_username"), "users", ["username"], unique=True)
    if not inspector.has_table("user_flats"):
        op.create_table(
            "user_flats",
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("flat_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["flat_id"], ["flats.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("user_id", "flat_id"),
        )
    if not inspector.has_table("password_reset_tokens"):
        op.create_table(
            "password_reset_tokens",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_password_reset_tokens_token_hash"),
            "password_reset_tokens",
            ["token_hash"],
            unique=True,
        )
        op.create_index(
            op.f("ix_password_reset_tokens_user_id"),
            "password_reset_tokens",
            ["user_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("password_reset_tokens")
    op.drop_table("user_flats")
    op.drop_index(op.f("ix_users_username"), table_name="users")
    op.drop_table("users")
