"""asset register: assets, per-asset recurring tasks, maintenance log

Existing schedule rows become the recurring tasks of an asset in the new register: one asset per
distinct asset name (Lift, Generator, Water tank, ...) plus the standard Motor/pump. Each row's
"last done" date is kept as the first maintenance-log entry. A copy of the SQLite file is written
to backups/ first. Downgrade removes the register and log; daily/weekly tasks become custom
(1 / 7 days) so the old code can read them.

Revision ID: e1f4b7a2c805
Revises: d3a1c5e7f902
Create Date: 2026-10-10 07:00:00.000000

"""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e1f4b7a2c805"
down_revision: Union[str, None] = "d3a1c5e7f902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_ASSETS = ("Lift", "Generator", "Water tank", "Motor/pump")
TYPE_KEYWORDS = (("lift", "Lift"), ("elevator", "Lift"), ("generator", "Generator"), ("tank", "Water tank"),
                 ("motor", "Motor/pump"), ("pump", "Motor/pump"), ("fire", "Fire safety"), ("cctv", "CCTV / security"))


def guess_type(name: str | None) -> str:
    low = (name or "").lower()
    return next((kind for word, kind in TYPE_KEYWORDS if word in low), "Other")


def _backup(bind) -> None:
    if bind.dialect.name != "sqlite":
        return
    db_file = bind.engine.url.database
    if not db_file or db_file == ":memory:" or not Path(db_file).exists():
        return
    from app.config import settings

    dest = Path(settings.backup_dir) / f"nest5-{datetime.now():%Y%m%d-%H%M%S}-pre-asset-register.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src, dst = sqlite3.connect(db_file), sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    print(f"[nest5] Backup before asset register migration: {dest}", flush=True)


def upgrade() -> None:
    bind = op.get_bind()
    _backup(bind)
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "assets" not in tables:
        op.create_table(
            "assets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("asset_type", sa.String(100), nullable=True),
            sa.Column("location", sa.String(255), nullable=True),
            sa.Column("vendor_contact", sa.String(255), nullable=True),
            sa.Column("install_date", sa.Date(), nullable=True),
            sa.Column("warranty_expiry", sa.Date(), nullable=True),
            sa.Column("cost", sa.Integer(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
        )
    if "asset_id" not in {c["name"] for c in insp.get_columns("asset_maintenance_schedules")}:
        op.add_column("asset_maintenance_schedules", sa.Column("asset_id", sa.Integer(), nullable=True))
    if "ix_asset_maintenance_schedules_asset_id" not in {
            i["name"] for i in insp.get_indexes("asset_maintenance_schedules")}:
        op.create_index("ix_asset_maintenance_schedules_asset_id", "asset_maintenance_schedules", ["asset_id"])
    if "asset_maintenance_logs" not in tables:
        op.create_table(
            "asset_maintenance_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
            sa.Column("schedule_id", sa.Integer(), sa.ForeignKey("asset_maintenance_schedules.id"), nullable=True),
            sa.Column("done_on", sa.Date(), nullable=False),
            sa.Column("done_by", sa.String(255), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("cost", sa.Integer(), nullable=True),
            sa.Column("next_due_on", sa.Date(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("created_by", sa.String(80), nullable=True),
        )
        op.create_index("ix_asset_maintenance_logs_asset_id", "asset_maintenance_logs", ["asset_id"])

    now = datetime.utcnow()
    ids: dict[str, int] = {
        name.lower(): aid for aid, name in bind.execute(sa.text("SELECT id, name FROM assets WHERE deleted_at IS NULL"))
    }

    def asset_id_for(name: str) -> int:
        key = name.strip().lower()
        if key not in ids:
            bind.execute(sa.text("INSERT INTO assets (name, asset_type, created_at) VALUES (:n, :t, :c)"),
                         {"n": name.strip(), "t": guess_type(name), "c": now})
            ids[key] = bind.execute(sa.text("SELECT max(id) FROM assets")).scalar()
        return ids[key]

    rows = bind.execute(sa.text(
        "SELECT id, asset, vendor, last_done_on, next_due_on, deleted_at FROM asset_maintenance_schedules "
        "WHERE asset_id IS NULL ORDER BY id")).all()
    for sid, name, vendor, last_done, next_due, deleted in rows:
        aid = asset_id_for(name or "Other")
        bind.execute(sa.text("UPDATE asset_maintenance_schedules SET asset_id = :a WHERE id = :s"), {"a": aid, "s": sid})
        if vendor:
            bind.execute(sa.text("UPDATE assets SET vendor_contact = :v WHERE id = :a AND vendor_contact IS NULL"),
                         {"v": vendor, "a": aid})
        if last_done and not deleted:
            bind.execute(sa.text(
                "INSERT INTO asset_maintenance_logs (asset_id, schedule_id, done_on, notes, next_due_on, created_at, "
                "created_by) VALUES (:a, :s, :d, :n, :x, :c, 'migration')"),
                {"a": aid, "s": sid, "d": last_done, "n": "Last done date from the earlier schedule.",
                 "x": next_due, "c": now})
    for name in DEFAULT_ASSETS:
        asset_id_for(name)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE asset_maintenance_schedules SET frequency = 'CUSTOM', custom_interval_days = 1 "
                         "WHERE frequency = 'DAILY'"))
    bind.execute(sa.text("UPDATE asset_maintenance_schedules SET frequency = 'CUSTOM', custom_interval_days = 7 "
                         "WHERE frequency = 'WEEKLY'"))
    op.drop_index("ix_asset_maintenance_logs_asset_id", table_name="asset_maintenance_logs")
    op.drop_table("asset_maintenance_logs")
    op.drop_index("ix_asset_maintenance_schedules_asset_id", table_name="asset_maintenance_schedules")
    with op.batch_alter_table("asset_maintenance_schedules") as batch:
        batch.drop_column("asset_id")
    op.drop_table("assets")
