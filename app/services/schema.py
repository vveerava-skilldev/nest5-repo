"""Safety net for databases that were `alembic stamp`ed instead of upgraded.

create_all() adds missing tables but never missing columns; this adds any missing
*nullable* column declared on the models so the app still starts.
"""
import logging

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.database import Base

log = logging.getLogger("nest5.schema")


def add_missing_columns(engine: Engine) -> list[str]:
    added: list[str] = []
    inspector = sa.inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing or not column.nullable or column.primary_key:
                    continue
                ddl_type = column.type.compile(dialect=engine.dialect)
                conn.execute(sa.text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}'))
                added.append(f"{table.name}.{column.name}")
    if added:
        log.warning("Added missing columns: %s", ", ".join(added))
    return added
