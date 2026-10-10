"""Admin switches stored in the app_settings table."""
from sqlalchemy.orm import Session

from app.models import AppSetting

OWNER_BUILDING_VIEW = "owner_building_payments_view"
DEFAULTS = {OWNER_BUILDING_VIEW: "1"}


def get_bool(db: Session, key: str) -> bool:
    row = db.get(AppSetting, key)
    value = row.value if row is not None else DEFAULTS.get(key, "0")
    return value == "1"


def set_bool(db: Session, key: str, on: bool, *, updated_by: str | None = None) -> bool:
    row = db.get(AppSetting, key)
    before = get_bool(db, key)
    if row is None:
        row = AppSetting(key=key, value="1" if on else "0", updated_by=updated_by)
        db.add(row)
    else:
        row.value = "1" if on else "0"
        row.updated_by = updated_by
    db.flush()
    return before


def get_value(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.get(AppSetting, key)
    return row.value if row is not None else DEFAULTS.get(key, default)


def set_value(db: Session, key: str, value: str, *, updated_by: str | None = None) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value, updated_by=updated_by))
    else:
        row.value = value
        row.updated_by = updated_by
    db.flush()
