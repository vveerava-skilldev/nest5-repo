"""Display and parse helpers for dates (DB stores ISO date)."""
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


def format_ist(now: datetime | None = None) -> str:
    """e.g. 'Mon 19 Oct 2026, 4:37 PM IST'."""
    t = ist_now(now)
    hour12 = t.hour % 12 or 12
    return f"{_DAYS[t.weekday()]} {t.day} {_MONTHS[t.month - 1]} {t.year}, {hour12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'} IST"


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


def ist_today(now: datetime | None = None) -> date:
    return ist_now(now).date()
