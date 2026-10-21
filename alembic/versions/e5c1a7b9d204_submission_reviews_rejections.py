"""v7.1: per-submission admin review marks and rejected owner payment records

Two new tables only; no existing row is changed, so every submission listed at upgrade time stays
un-reviewed. Idempotent: the startup create_all() safety net may already have created the tables.

Revision ID: e5c1a7b9d204
Revises: d4b8f1a3c627
Create Date: 2026-10-19 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5c1a7b9d204"
down_revision: Union[str, None] = "d4b8f1a3c627"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _tables()
    if "submission_reviews" not in existing:
        op.create_table(
            "submission_reviews",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("audit_id", sa.Integer(), nullable=False),
            sa.Column("kind", sa.String(length=40), nullable=False),
            sa.Column("record_id", sa.Integer(), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(), nullable=False),
            sa.Column("reviewed_by", sa.String(length=80), nullable=True),
        )
        op.create_index("ix_submission_reviews_audit_id", "submission_reviews", ["audit_id"], unique=True)
    if "submission_rejections" not in existing:
        op.create_table(
            "submission_rejections",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("record_type", sa.String(length=40), nullable=False),
            sa.Column("record_id", sa.Integer(), nullable=True),
            sa.Column("flat_id", sa.Integer(), sa.ForeignKey("flats.id"), nullable=True),
            sa.Column("submitted_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("summary", sa.String(length=255), nullable=True),
            sa.Column("amount", sa.Integer(), nullable=True),
            sa.Column("paid_on", sa.Date(), nullable=True),
            sa.Column("reason", sa.String(length=255), nullable=True),
            sa.Column("rejected_at", sa.DateTime(), nullable=False),
            sa.Column("rejected_by", sa.String(length=80), nullable=True),
        )
        op.create_index("ix_submission_rejections_record_type", "submission_rejections", ["record_type"])
        op.create_index(
            "ix_submission_rejections_submitted_by_user_id", "submission_rejections", ["submitted_by_user_id"]
        )


def downgrade() -> None:
    # Review marks and the owner-visible "Rejected" records are lost; the rejected payments were
    # already removed from the ledger, so no totals change.
    existing = _tables()
    if "submission_rejections" in existing:
        op.drop_table("submission_rejections")
    if "submission_reviews" in existing:
        op.drop_table("submission_reviews")
