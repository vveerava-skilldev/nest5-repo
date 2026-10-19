"""owner submissions, cancel requests, corpus campaigns

Revision ID: f3a8d1c6b2e9
Revises: e7b3c9d2f4a1
Create Date: 2026-10-09 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3a8d1c6b2e9"
down_revision: Union[str, None] = "e7b3c9d2f4a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Plain nullable columns (no ALTER-time FK) so SQLite can add them in place.
NEW_COLUMNS = {
    "maintenance_payments": [
        sa.Column("payment_mode", sa.String(length=30), nullable=True),
        sa.Column("submitted_by_user_id", sa.Integer(), nullable=True),
    ],
    "expenses": [
        sa.Column("submitted_by_user_id", sa.Integer(), nullable=True),
    ],
    "corpus_payments": [
        sa.Column("campaign_id", sa.Integer(), nullable=True),
        sa.Column("payment_mode", sa.String(length=30), nullable=True),
        sa.Column("submitted_by_user_id", sa.Integer(), nullable=True),
    ],
}


def upgrade() -> None:
    # app.main runs create_all on startup, so new tables may already exist.
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("corpus_campaigns"):
        op.create_table(
            "corpus_campaigns",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("target_amount", sa.Integer(), nullable=False),
            sa.Column("due_date", sa.Date(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("created_by", sa.String(length=80), nullable=False),
            sa.Column("closed_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    if not inspector.has_table("corpus_campaign_shares"):
        op.create_table(
            "corpus_campaign_shares",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("campaign_id", sa.Integer(), nullable=False),
            sa.Column("flat_id", sa.Integer(), nullable=False),
            sa.Column("share_amount", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["campaign_id"], ["corpus_campaigns.id"]),
            sa.ForeignKeyConstraint(["flat_id"], ["flats.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("campaign_id", "flat_id"),
        )
        op.create_index(
            op.f("ix_corpus_campaign_shares_campaign_id"),
            "corpus_campaign_shares",
            ["campaign_id"],
            unique=False,
        )
    if not inspector.has_table("cancel_requests"):
        op.create_table(
            "cancel_requests",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("record_type", sa.String(length=40), nullable=False),
            sa.Column("record_id", sa.Integer(), nullable=False),
            sa.Column("flat_id", sa.Integer(), nullable=True),
            sa.Column("requested_by_user_id", sa.Integer(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("summary", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("decided_at", sa.DateTime(), nullable=True),
            sa.Column("decided_by", sa.String(length=80), nullable=True),
            sa.Column("decision_note", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["flat_id"], ["flats.id"]),
            sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        for col in ("record_type", "record_id", "status"):
            op.create_index(op.f(f"ix_cancel_requests_{col}"), "cancel_requests", [col], unique=False)

    for table, columns in NEW_COLUMNS.items():
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)
    corpus_indexes = {i["name"] for i in inspector.get_indexes("corpus_payments")}
    if "ix_corpus_payments_campaign_id" not in corpus_indexes:
        op.create_index(op.f("ix_corpus_payments_campaign_id"), "corpus_payments", ["campaign_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_corpus_payments_campaign_id"), table_name="corpus_payments")
    for table, columns in NEW_COLUMNS.items():
        with op.batch_alter_table(table) as batch:
            for column in columns:
                batch.drop_column(column.name)
    op.drop_table("cancel_requests")
    op.drop_table("corpus_campaign_shares")
    op.drop_table("corpus_campaigns")
