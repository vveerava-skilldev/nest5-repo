"""v7.3: recurring admin tasks, payment-detail history, payee snapshot on payments, carried-opening flag

Three new tables and additive nullable columns: ledger_periods.opening_carried and the payee snapshot
columns (payee_vpa, payee_mobile, payee_name, payee_settings_id) on maintenance_payments,
corpus_payments, project_contributions and submission_rejections. The two default recurring tasks
(Watchman salary by the 5th, Common EB before the 10th) are inserted only when absent (seed_key).

The initial payment-settings history row is created from the configured seed, and every existing
payment row (paid maintenance/corpus rows, all project contributions, all rejected-submission records)
gets the initial payee written into the four new columns only. Amounts, dates, status and every other
existing column are untouched. Idempotent: the startup create_all() / add_missing_columns() safety net
may already have created the tables and columns, and the backfill runs once (marker in app_settings).

Revision ID: a3d9e6c1b852
Revises: e5c1a7b9d204
Create Date: 2026-10-19 18:00:00.000000

"""
from datetime import datetime, timedelta, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3d9e6c1b852"
down_revision: Union[str, None] = "e5c1a7b9d204"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_IST = timezone(timedelta(hours=5, minutes=30))

DEFAULT_TASKS = (
    ("watchman_salary", "Watchman salary payment", 5, "by"),
    ("common_eb", "Common EB (electricity) payment", 10, "before"),
)


PAYEE_TABLES = ("maintenance_payments", "corpus_payments", "project_contributions", "submission_rejections")
PAYEE_COLUMNS = (
    ("payee_vpa", sa.String(length=120)),
    ("payee_mobile", sa.String(length=20)),
    ("payee_name", sa.String(length=120)),
    ("payee_settings_id", sa.Integer()),
)
APP_SETTING_KEYS = ("pay_upi_vpa", "pay_upi_mobile", "pay_payee_name", "pay_payee_backfill_done")


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _first_open_month(day: int) -> tuple[int, int]:
    """First month whose due day has not passed yet (so a fresh seed is never instantly overdue)."""
    today = datetime.now(timezone.utc).astimezone(_IST).date()
    if today.day <= day:
        return today.year, today.month
    return (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)


def upgrade() -> None:
    insp = _inspector()
    existing = set(insp.get_table_names())

    if "recurring_tasks" not in existing:
        op.create_table(
            "recurring_tasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("day_of_month", sa.Integer(), nullable=False),
            sa.Column("due_mode", sa.String(length=10), nullable=False),
            sa.Column("interval_months", sa.Integer(), nullable=False),
            sa.Column("start_year", sa.Integer(), nullable=False),
            sa.Column("start_month", sa.Integer(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("seed_key", sa.String(length=40), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("created_by", sa.String(length=80), nullable=False),
            sa.UniqueConstraint("seed_key"),
        )
    if "recurring_task_occurrences" not in existing:
        op.create_table(
            "recurring_task_occurrences",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("task_id", sa.Integer(), sa.ForeignKey("recurring_tasks.id"), nullable=False),
            sa.Column("year", sa.Integer(), nullable=False),
            sa.Column("month", sa.Integer(), nullable=False),
            sa.Column("due_date", sa.Date(), nullable=False),
            sa.Column("done_on", sa.Date(), nullable=True),
            sa.Column("amount", sa.Integer(), nullable=True),
            sa.Column("note", sa.String(length=255), nullable=True),
            sa.Column("done_by", sa.String(length=80), nullable=True),
            sa.Column("done_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("task_id", "year", "month", name="uq_recurring_occurrence_month"),
        )
        op.create_index("ix_recurring_task_occurrences_task_id", "recurring_task_occurrences", ["task_id"])
        op.create_index("ix_recurring_task_occurrences_due_date", "recurring_task_occurrences", ["due_date"])
    if "payment_detail_changes" not in existing:
        op.create_table(
            "payment_detail_changes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("changed_at", sa.DateTime(), nullable=False),
            sa.Column("changed_by", sa.String(length=80), nullable=False),
            sa.Column("is_initial", sa.Boolean(), nullable=False),
            sa.Column("old_upi_vpa", sa.String(length=120), nullable=True),
            sa.Column("new_upi_vpa", sa.String(length=120), nullable=False),
            sa.Column("old_upi_mobile", sa.String(length=20), nullable=True),
            sa.Column("new_upi_mobile", sa.String(length=20), nullable=False),
            sa.Column("old_payee_name", sa.String(length=120), nullable=True),
            sa.Column("new_payee_name", sa.String(length=120), nullable=False),
            sa.Column("reason", sa.String(length=255), nullable=False),
            sa.Column("fund_balance", sa.Integer(), nullable=True),
            sa.Column("corpus_collected", sa.Integer(), nullable=True),
            sa.Column("projects_collected", sa.Integer(), nullable=True),
            sa.Column("owed_back", sa.Integer(), nullable=True),
            sa.Column("total_balance", sa.Integer(), nullable=True),
            sa.Column("bank_balance_entered", sa.String(length=60), nullable=True),
        )
        op.create_index("ix_payment_detail_changes_changed_at", "payment_detail_changes", ["changed_at"])

    insp = _inspector()
    columns = {c["name"] for c in insp.get_columns("ledger_periods")}
    if "opening_carried" not in columns:
        op.add_column("ledger_periods", sa.Column("opening_carried", sa.Boolean(), nullable=True))

    for table in PAYEE_TABLES:
        have = {c["name"] for c in insp.get_columns(table)}
        for name, type_ in PAYEE_COLUMNS:
            if name not in have:
                op.add_column(table, sa.Column(name, type_, nullable=True))

    bind = op.get_bind()
    now = datetime.utcnow()
    for key, title, day, mode in DEFAULT_TASKS:
        found = bind.execute(
            sa.text("SELECT id FROM recurring_tasks WHERE seed_key = :k"), {"k": key}
        ).first()
        if found:
            continue
        year, month = _first_open_month(day)
        bind.execute(
            sa.text(
                "INSERT INTO recurring_tasks (title, day_of_month, due_mode, interval_months, start_year,"
                " start_month, is_active, notes, seed_key, created_at, created_by)"
                " VALUES (:title, :day, :mode, 1, :y, :m, 1, NULL, :k, :now, 'system')"
            ),
            {"title": title, "day": day, "mode": mode, "y": year, "m": month, "k": key, "now": now},
        )

    # The app's own code seeds the initial history row (with the balance snapshot) and stamps the
    # existing payments, so the migration and a fresh start agree exactly.
    from sqlalchemy.orm import Session

    from app.services import payment_details

    session = Session(bind=bind)
    try:
        payment_details.ensure_initial(session)
        payment_details.backfill_payees(session)
        session.flush()
    finally:
        session.close()


def downgrade() -> None:
    # Recurring task definitions/occurrences and the payment-detail change history are lost. The
    # current UPI details stay in app_settings. Ledger figures are untouched: only the carried flag goes.
    insp = _inspector()
    existing = set(insp.get_table_names())
    for table in PAYEE_TABLES:
        have = {c["name"] for c in insp.get_columns(table)}
        drop = [name for name, _ in PAYEE_COLUMNS if name in have]
        if drop:
            with op.batch_alter_table(table) as batch:
                for name in drop:
                    batch.drop_column(name)
    for table in ("recurring_task_occurrences", "recurring_tasks", "payment_detail_changes"):
        if table in existing:
            op.drop_table(table)
    if "opening_carried" in {c["name"] for c in insp.get_columns("ledger_periods")}:
        with op.batch_alter_table("ledger_periods") as batch:
            batch.drop_column("opening_carried")
    if "app_settings" in existing:
        op.get_bind().execute(
            sa.text("DELETE FROM app_settings WHERE key IN (:a, :b, :c, :d)"),
            dict(zip("abcd", APP_SETTING_KEYS)),
        )
