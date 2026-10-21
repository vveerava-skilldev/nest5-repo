"""calendar events (admin-managed building events for the in-portal Calendar)

Revision ID: b8e3f5a1d726
Revises: a7d2c4e9b108
Create Date: 2026-10-12 06:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8e3f5a1d726"
down_revision: Union[str, None] = "a7d2c4e9b108"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector() -> sa.engine.reflection.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    # The app's startup (create_all) may already have created the table on a stamped database.
    if "calendar_events" in _inspector().get_table_names():
        return
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("start_time", sa.String(5), nullable=True),
        sa.Column("end_time", sa.String(5), nullable=True),
        sa.Column("event_type", sa.String(40), nullable=False, server_default="Other"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("visible_to_owners", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(80), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by", sa.String(80), nullable=True),
    )
    op.create_index("ix_calendar_events_event_date", "calendar_events", ["event_date"])


def downgrade() -> None:
    inspector = _inspector()
    if "calendar_events" not in inspector.get_table_names():
        return
    if any(ix["name"] == "ix_calendar_events_event_date" for ix in inspector.get_indexes("calendar_events")):
        op.drop_index("ix_calendar_events_event_date", table_name="calendar_events")
    op.drop_table("calendar_events")
