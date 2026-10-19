"""Display and parse helpers for dates (DB stores ISO date).

Everything the app *shows* goes through this module: dates are dd-mm-yyyy (zero padded, hyphens), date-times
are dd-mm-yyyy HH:MM, and the IST clock is 'Mon 19-10-2026, 4:37 PM IST'. ISO yyyy-mm-dd stays only in form
values, URLs, the database and the JSON backup.
"""
import re
from datetime import date, datetime, timedelta, timezone

# India has no daylight saving, so a fixed offset is exact and needs no tz database on the server.
IST = timezone(timedelta(hours=5, minutes=30), "IST")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def ist_now(now: datetime | None = None) -> datetime:
    """Current time in IST. `now` may be naive (taken as UTC) or aware."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(IST)


def format_ist_stamp(now: datetime | None = None) -> str:
    """e.g. '19-10-2026 16:37 IST' (generated-at stamps in files and exports)."""
    return f"{ist_now(now):%d-%m-%Y %H:%M} IST"


def format_ist(now: datetime | None = None) -> str:
    """e.g. 'Mon 19-10-2026, 4:37 PM IST'."""
    t = ist_now(now)
    hour12 = t.hour % 12 or 12
    return f"{_DAYS[t.weekday()]} {t:%d-%m-%Y}, {hour12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'} IST"


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


format_date = format_date_ddmmyyyy
format_datetime = format_datetime_ddmmyyyy


def format_weekday_date(value: date | datetime | str | None) -> str:
    """e.g. 'Mon 05-10-2026'."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, str):
        value = date.fromisoformat(value[:10])
    return f"{_DAYS[value.weekday()]} {value:%d-%m-%Y}"


def format_date_or_blank(value: date | datetime | str | None) -> str:
    """Like format_date but an empty string for None (for exports and hints)."""
    return "" if value is None else format_date_ddmmyyyy(value)


def due_hint(due: date | None) -> str:
    """e.g. 'due 05-10-2026' (empty when there is no due date)."""
    return f"due {format_date_ddmmyyyy(due)}" if due else ""


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


_ISO_DATETIME = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?\b")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def display_dates_in_text(text: str | None) -> str:
    """Rewrite ISO dates / timestamps inside a piece of display text (e.g. a stored JSON payload) as dd-mm-yyyy."""
    if not text:
        return ""
    text = _ISO_DATETIME.sub(lambda m: f"{m[3]}-{m[2]}-{m[1]} {m[4]}:{m[5]}", text)
    return _ISO_DATE.sub(lambda m: f"{m[3]}-{m[2]}-{m[1]}", text)


def ist_today(now: datetime | None = None) -> date:
    return ist_now(now).date()
