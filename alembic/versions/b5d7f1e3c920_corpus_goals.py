"""corpus goals: category per goal, admin categories, optional line items

Corpus collections become goals: each gets a category (Building repair, Lift, Painting,
General, or one the admin adds) and may hold line items (sub-goals) that payments can name.
Existing data is kept: every collection gets a category guessed from its name (General corpus
and anything unmatched -> General). A copy of the SQLite file is written to backups/ first.

Revision ID: b5d7f1e3c920
Revises: a6c4e2b8d013
Create Date: 2026-10-10 05:00:00.000000

"""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5d7f1e3c920"
down_revision: Union[str, None] = "a6c4e2b8d013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_CATEGORIES = ("General", "Building repair", "Lift", "Painting")
KEYWORDS = (("lift", "Lift"), ("elevator", "Lift"), ("paint", "Painting"), ("repair", "Building repair"),
            ("leak", "Building repair"), ("roof", "Building repair"))


def guess_category(name: str | None) -> str:
    low = (name or "").lower()
    return next((cat for word, cat in KEYWORDS if word in low), "General")


def _backup(bind) -> None:
    if bind.dialect.name != "sqlite":
        return
    db_file = bind.engine.url.database
    if not db_file or db_file == ":memory:" or not Path(db_file).exists():
        return
    from app.config import settings

    dest = Path(settings.backup_dir) / f"nest5-{datetime.now():%Y%m%d-%H%M%S}-pre-corpus-goals.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src, dst = sqlite3.connect(db_file), sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    print(f"[nest5] Backup before corpus goals migration: {dest}", flush=True)


def upgrade() -> None:
    bind = op.get_bind()
    _backup(bind)
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "corpus_categories" not in tables:
        op.create_table(
            "corpus_categories",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(100), nullable=False, unique=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
    if "corpus_line_items" not in tables:
        op.create_table(
            "corpus_line_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("corpus_campaigns.id"), nullable=False),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("target_amount", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_corpus_line_items_campaign_id", "corpus_line_items", ["campaign_id"])

    if "category" not in {c["name"] for c in insp.get_columns("corpus_campaigns")}:
        op.add_column("corpus_campaigns", sa.Column("category", sa.String(100), nullable=True))
    if "line_item_id" not in {c["name"] for c in insp.get_columns("corpus_payments")}:
        op.add_column("corpus_payments", sa.Column("line_item_id", sa.Integer(), nullable=True))

    now = datetime.utcnow()
    existing = {r[0] for r in bind.execute(sa.text("SELECT name FROM corpus_categories")).fetchall()}
    for name in DEFAULT_CATEGORIES:
        if name not in existing:
            bind.execute(sa.text("INSERT INTO corpus_categories (name, created_at) VALUES (:n, :t)"), {"n": name, "t": now})
    for cid, name, is_general in bind.execute(
        sa.text("SELECT id, name, is_general FROM corpus_campaigns WHERE category IS NULL")
    ).fetchall():
        bind.execute(
            sa.text("UPDATE corpus_campaigns SET category = :c WHERE id = :id"),
            {"c": "General" if is_general else guess_category(name), "id": cid},
        )


def downgrade() -> None:
    with op.batch_alter_table("corpus_payments") as batch:
        batch.drop_column("line_item_id")
    with op.batch_alter_table("corpus_campaigns") as batch:
        batch.drop_column("category")
    op.drop_index("ix_corpus_line_items_campaign_id", table_name="corpus_line_items")
    op.drop_table("corpus_line_items")
    op.drop_table("corpus_categories")
