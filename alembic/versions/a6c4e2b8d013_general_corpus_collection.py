"""general corpus collection: fold one-off corpus line items into a collection

Before this, corpus lived in two places: one-off line items (corpus_payments with no
campaign_id) and corpus collections. This moves every line item into one open-ended
"General corpus" collection so all corpus views read the same data:

- each flat's General corpus share = sum of its line items (paid + unpaid)
- paid line items become payments against General corpus (same row, campaign_id set)
- unpaid line items are folded into the share (the row is removed; amount kept in the share)

A copy of the SQLite file is written to backups/ first.

Revision ID: a6c4e2b8d013
Revises: f3a8d1c6b2e9
Create Date: 2026-10-10 03:00:00.000000

"""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a6c4e2b8d013"
down_revision: Union[str, None] = "f3a8d1c6b2e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

GENERAL_NAME = "General corpus"
GENERAL_DESCRIPTION = "Open-ended corpus contributions (one-off amounts added per flat)."


def _backup(bind) -> None:
    if bind.dialect.name != "sqlite":
        return
    db_file = bind.engine.url.database
    if not db_file or db_file == ":memory:" or not Path(db_file).exists():
        return
    from app.config import settings

    dest = Path(settings.backup_dir) / f"nest5-{datetime.now():%Y%m%d-%H%M%S}-pre-general-corpus.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src, dst = sqlite3.connect(db_file), sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    print(f"[nest5] Backup before corpus migration: {dest}", flush=True)


def upgrade() -> None:
    bind = op.get_bind()
    legacy = bind.execute(
        sa.text("SELECT id, flat_id, amount, is_paid FROM corpus_payments WHERE campaign_id IS NULL")
    ).fetchall()
    if legacy:
        _backup(bind)

    columns = {c["name"] for c in sa.inspect(bind).get_columns("corpus_campaigns")}
    if "is_general" not in columns:
        op.add_column("corpus_campaigns", sa.Column("is_general", sa.Boolean(), nullable=True))
    if not legacy:
        return

    general_id = bind.execute(
        sa.text("SELECT id FROM corpus_campaigns WHERE is_general = 1 ORDER BY id LIMIT 1")
    ).scalar()
    if general_id is None:
        bind.execute(
            sa.text(
                "INSERT INTO corpus_campaigns (name, description, target_amount, due_date, created_at, created_by, closed_at, is_general) "
                "VALUES (:name, :description, 0, NULL, :now, 'migration', NULL, 1)"
            ),
            {"name": GENERAL_NAME, "description": GENERAL_DESCRIPTION, "now": datetime.utcnow()},
        )
        general_id = bind.execute(
            sa.text("SELECT id FROM corpus_campaigns WHERE is_general = 1 ORDER BY id LIMIT 1")
        ).scalar()

    added: dict[int, int] = {}
    for _id, flat_id, amount, _paid in legacy:
        added[flat_id] = added.get(flat_id, 0) + int(amount or 0)
    flat_ids = [r[0] for r in bind.execute(sa.text("SELECT id FROM flats")).fetchall()]
    for flat_id in sorted(set(flat_ids) | set(added)):
        existing = bind.execute(
            sa.text("SELECT id FROM corpus_campaign_shares WHERE campaign_id = :c AND flat_id = :f"),
            {"c": general_id, "f": flat_id},
        ).scalar()
        if existing is None:
            bind.execute(
                sa.text("INSERT INTO corpus_campaign_shares (campaign_id, flat_id, share_amount) VALUES (:c, :f, :a)"),
                {"c": general_id, "f": flat_id, "a": added.get(flat_id, 0)},
            )
        else:
            bind.execute(
                sa.text("UPDATE corpus_campaign_shares SET share_amount = share_amount + :a WHERE id = :id"),
                {"a": added.get(flat_id, 0), "id": existing},
            )

    bind.execute(
        sa.text("UPDATE corpus_payments SET campaign_id = :c WHERE campaign_id IS NULL AND is_paid = 1"),
        {"c": general_id},
    )
    bind.execute(sa.text("DELETE FROM corpus_payments WHERE campaign_id IS NULL"))
    bind.execute(
        sa.text(
            "UPDATE corpus_campaigns SET target_amount = "
            "(SELECT COALESCE(SUM(share_amount), 0) FROM corpus_campaign_shares WHERE campaign_id = :c) WHERE id = :c"
        ),
        {"c": general_id},
    )


def downgrade() -> None:
    bind = op.get_bind()
    general_id = bind.execute(
        sa.text("SELECT id FROM corpus_campaigns WHERE is_general = 1 ORDER BY id LIMIT 1")
    ).scalar()
    if general_id is not None:
        shares = bind.execute(
            sa.text("SELECT flat_id, share_amount FROM corpus_campaign_shares WHERE campaign_id = :c"),
            {"c": general_id},
        ).fetchall()
        for flat_id, share in shares:
            paid = bind.execute(
                sa.text("SELECT COALESCE(SUM(amount), 0) FROM corpus_payments WHERE campaign_id = :c AND flat_id = :f AND is_paid = 1"),
                {"c": general_id, "f": flat_id},
            ).scalar()
            if share - paid > 0:
                bind.execute(
                    sa.text("INSERT INTO corpus_payments (flat_id, amount, is_paid, description) VALUES (:f, :a, 0, :d)"),
                    {"f": flat_id, "a": share - paid, "d": "General corpus (balance)"},
                )
        bind.execute(sa.text("UPDATE corpus_payments SET campaign_id = NULL WHERE campaign_id = :c"), {"c": general_id})
        bind.execute(sa.text("DELETE FROM corpus_campaign_shares WHERE campaign_id = :c"), {"c": general_id})
        bind.execute(sa.text("DELETE FROM corpus_campaigns WHERE id = :c"), {"c": general_id})
    with op.batch_alter_table("corpus_campaigns") as batch:
        batch.drop_column("is_general")
