"""project part payments, project due date, admin-managed expense categories

- project_contributions: each payment (full or part) towards a flat's project share; a share that
  was already marked paid becomes one contribution of its full amount (same date and note).
- project_payments.paid_amount (sum of its contributions), projects.due_date.
- expense_categories.is_active / owner_allowed / sort_order; the built-in presets become rows.
A copy of the SQLite file is written to backups/ first.

Revision ID: f2c6a8d4b301
Revises: e1f4b7a2c805
Create Date: 2026-10-10 08:00:00.000000

"""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2c6a8d4b301"
down_revision: Union[str, None] = "e1f4b7a2c805"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PRESETS = ("Common", "Watchman Salary", "Common Electricity", "Others")


def _backup(bind) -> None:
    if bind.dialect.name != "sqlite":
        return
    db_file = bind.engine.url.database
    if not db_file or db_file == ":memory:" or not Path(db_file).exists():
        return
    from app.config import settings

    dest = Path(settings.backup_dir) / f"nest5-{datetime.now():%Y%m%d-%H%M%S}-pre-v6.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src, dst = sqlite3.connect(db_file), sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    print(f"[nest5] Backup before v6 migration: {dest}", flush=True)


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    _backup(bind)
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    now = datetime.utcnow()

    if "due_date" not in _columns(insp, "projects"):
        op.add_column("projects", sa.Column("due_date", sa.Date(), nullable=True))
    if "paid_amount" not in _columns(insp, "project_payments"):
        op.add_column("project_payments", sa.Column("paid_amount", sa.Integer(), nullable=True))
    if "project_contributions" not in tables:
        op.create_table(
            "project_contributions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_payment_id", sa.Integer(), sa.ForeignKey("project_payments.id"), nullable=False),
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("flat_id", sa.Integer(), sa.ForeignKey("flats.id"), nullable=False),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("paid_on", sa.Date(), nullable=False),
            sa.Column("payment_mode", sa.String(30), nullable=True),
            sa.Column("payment_note", sa.String(255), nullable=True),
            sa.Column("submitted_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_project_contributions_project_payment_id", "project_contributions",
                        ["project_payment_id"])

    has_contrib = {r[0] for r in bind.execute(sa.text("SELECT DISTINCT project_payment_id FROM project_contributions"))}
    rows = bind.execute(sa.text(
        "SELECT id, project_id, flat_id, amount, is_paid, paid_on, payment_note FROM project_payments "
        "WHERE paid_amount IS NULL")).all()
    for pid, project_id, flat_id, amount, is_paid, paid_on, note in rows:
        if is_paid and pid not in has_contrib:
            bind.execute(sa.text(
                "INSERT INTO project_contributions (project_payment_id, project_id, flat_id, amount, paid_on, "
                "payment_note, created_at) VALUES (:p, :pr, :f, :a, :d, :n, :c)"),
                {"p": pid, "pr": project_id, "f": flat_id, "a": amount, "d": paid_on or now.date(), "n": note, "c": now})
        bind.execute(sa.text("UPDATE project_payments SET paid_amount = :v WHERE id = :p"),
                     {"v": amount if is_paid else 0, "p": pid})

    cols = _columns(insp, "expense_categories")
    for name, col in (("is_active", sa.Boolean()), ("owner_allowed", sa.Boolean()), ("sort_order", sa.Integer())):
        if name not in cols:
            op.add_column("expense_categories", sa.Column(name, col, nullable=True))
    bind.execute(sa.text("UPDATE expense_categories SET is_active = 1 WHERE is_active IS NULL"))
    bind.execute(sa.text("UPDATE expense_categories SET owner_allowed = 1 WHERE owner_allowed IS NULL"))
    existing = {r[0].lower() for r in bind.execute(sa.text("SELECT name FROM expense_categories"))}
    for i, name in enumerate(PRESETS):
        if name.lower() in existing:
            bind.execute(sa.text("UPDATE expense_categories SET sort_order = :o WHERE lower(name) = :n "
                                 "AND sort_order IS NULL"), {"o": i, "n": name.lower()})
        else:
            bind.execute(sa.text("INSERT INTO expense_categories (name, created_at, is_active, owner_allowed, "
                                 "sort_order) VALUES (:n, :c, 1, 1, :o)"), {"n": name, "c": now, "o": i})


def downgrade() -> None:
    bind = op.get_bind()
    # Part-paid shares cannot be represented by the old paid/unpaid flag: they go back to unpaid.
    bind.execute(sa.text(
        "UPDATE project_payments SET is_paid = CASE WHEN COALESCE(paid_amount, 0) >= amount AND amount > 0 "
        "THEN 1 ELSE 0 END"))
    bind.execute(sa.text("UPDATE project_payments SET paid_on = NULL WHERE is_paid = 0"))
    op.drop_index("ix_project_contributions_project_payment_id", table_name="project_contributions")
    op.drop_table("project_contributions")
    with op.batch_alter_table("project_payments") as batch:
        batch.drop_column("paid_amount")
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("due_date")
    with op.batch_alter_table("expense_categories") as batch:
        batch.drop_column("sort_order")
        batch.drop_column("owner_allowed")
        batch.drop_column("is_active")
