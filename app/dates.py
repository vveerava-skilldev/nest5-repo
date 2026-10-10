"""Display and parse helpers for dates (DB stores ISO date)."""
from datetime import date, datetime


def format_date_ddmmyyyy(value: date | datetime | str | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, str):
        value = date.fromisoformat(value[:10])
    return value.strftime("%d-%m-%Y")


def format_datetime_ddmmyyyy(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%d-%m-%Y %H:%M")


def date_to_iso(value: date | datetime | str | None) -> str:
    """Value for HTML5 ``type=\"date\"`` inputs (YYYY-MM-DD)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, str):
        return value[:10]
    return value.isoformat()


def parse_iso_date(raw: str) -> date:
    return date.fromisoformat(raw[:10])
