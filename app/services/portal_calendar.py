"""In-portal Calendar: one place for asset tasks, maintenance / corpus / project due dates and
admin-managed building events. Read-only for owners, who only ever receive their own flats'
payment status and events marked visible to owners.
"""
import calendar as pycal
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    AssetMaintenanceLog,
    AssetMaintenanceSchedule,
    CalendarEvent,
    Flat,
    LedgerPeriod,
    MaintenancePayment,
    Project,
    ProjectPayment,
)
from app.services import corpus_campaigns as campaigns_svc
from app.services import ledger as ledger_svc
from app.services import schedule as schedule_svc

EVENT_TYPES = ("Meeting", "AGM", "Vendor visit", "Reminder", "Maintenance reminder", "Other")

# key -> (legend label, colour class). Overdue is a status (red), not a type.
TYPES = {
    "task": "Asset task",
    "done": "Maintenance done",
    "maintenance": "Maintenance due",
    "corpus": "Corpus goal",
    "project": "Project",
    "event": "Event",
}
MAX_OCCURRENCES = 800


class EventError(ValueError):
    pass


@dataclass
class Viewer:
    """Who is looking: the admin sees everything, an owner only their own flats."""

    is_admin: bool
    flat_ids: set[int] = field(default_factory=set)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, pycal.monthrange(year, month)[1])


def parse_month(raw: str | None, today: date | None = None) -> tuple[int, int]:
    """'YYYY-MM' -> (year, month); anything else means the current month."""
    today = today or date.today()
    try:
        year, month = (int(x) for x in (raw or "").split("-", 1))
        if 2000 <= year <= 2100 and 1 <= month <= 12:
            return year, month
    except ValueError:
        pass
    return today.year, today.month


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _item(*, type: str, title: str, day: date, status: str = "open", lines: list[str] | None = None,
          link: str | None = None, link_label: str | None = None, end_day: date | None = None,
          time: str | None = None, projected: bool = False, event_id: int | None = None,
          ident: str) -> dict:
    return {
        "id": ident,
        "type": type,
        "type_label": TYPES[type],
        "title": title,
        "date": day,
        "end_date": end_day if end_day and end_day > day else None,
        "time": time,
        "status": status,
        "lines": lines or [],
        "link": link,
        "link_label": link_label,
        "projected": projected,
        "event_id": event_id,
    }


# ---- asset tasks --------------------------------------------------------------------------


def task_occurrences(task: AssetMaintenanceSchedule, start: date, end: date, today: date) -> list[tuple[date, str, bool]]:
    """(date, status, projected) for each time `task` falls due inside [start, end].

    The stored next-due date is the real occurrence (red when it is already past). Later ones are
    projected by repeating the task's frequency; projections in the past are skipped because the
    next real date only moves when someone marks the task done.
    """
    first = task.next_due_on
    if first is None:
        return []
    out: list[tuple[date, str, bool]] = []
    if start <= first <= end:
        out.append((first, "overdue" if first < today else "open", False))
    current, guard = first, 0
    while guard < MAX_OCCURRENCES:
        guard += 1
        nxt = schedule_svc.add_interval(current, task.frequency, task.custom_interval_days)
        if nxt <= current:
            break
        current = nxt
        if current > end:
            break
        if current >= start and current >= today:
            out.append((current, "open", True))
    return out


def _task_items(db: Session, start: date, end: date, today: date, viewer: Viewer) -> list[dict]:
    items: list[dict] = []
    for task in schedule_svc.list_schedules(db):
        asset_name = task.asset_ref.name if task.asset_ref else task.asset
        link = f"/assets/{task.asset_id}" if task.asset_id else "/schedule"
        for day, status, projected in task_occurrences(task, start, end, today):
            lines = [f"Asset: {asset_name}", f"Repeats: {task.frequency.value}"
                     + (f" every {task.custom_interval_days} days" if task.frequency.value == "custom" and task.custom_interval_days else "")]
            if task.last_done_on:
                lines.append(f"Last done: {task.last_done_on:%d-%m-%Y}")
            if task.vendor:
                lines.append(f"Vendor: {task.vendor}")
            if projected:
                lines.append("Expected date, based on the repeat schedule.")
            if status == "overdue":
                lines.append("Overdue: not marked done yet.")
            items.append(_item(
                type="task", title=f"{asset_name}: {task.task}", day=day, status=status, lines=lines,
                projected=projected, link=link, link_label="Open asset schedule" if viewer.is_admin else "Open asset",
                ident=f"task-{task.id}-{day.isoformat()}"))
    return items


def _done_items(db: Session, start: date, end: date, viewer: Viewer) -> list[dict]:
    items: list[dict] = []
    logs = db.scalars(
        select(AssetMaintenanceLog)
        .where(AssetMaintenanceLog.done_on >= start, AssetMaintenanceLog.done_on <= end)
        .options(selectinload(AssetMaintenanceLog.asset), selectinload(AssetMaintenanceLog.task))
        .order_by(AssetMaintenanceLog.done_on, AssetMaintenanceLog.id)
    ).all()
    for log in logs:
        asset_name = log.asset.name if log.asset else "Asset"
        what = log.task.task if log.task else "Maintenance note"
        lines = [f"Asset: {asset_name}"]
        if log.done_by:
            lines.append(f"Done by: {log.done_by}")
        if log.notes:
            lines.append(f"Notes: {log.notes}")
        if viewer.is_admin and log.cost:
            lines.append(f"Cost: ₹{log.cost}")
        items.append(_item(
            type="done", title=f"{asset_name}: {what}", day=log.done_on, status="done", lines=lines,
            link=f"/assets/{log.asset_id}", link_label="Open asset history", ident=f"done-{log.id}"))
    return items


# ---- maintenance, corpus, projects --------------------------------------------------------


def _maintenance_items(db: Session, year: int, month: int, today: date, viewer: Viewer) -> list[dict]:
    """Maintenance is due for the whole month and counts as overdue once the month has passed
    (the same rule as the reminders), so it sits on the month's last day."""
    _, last = month_bounds(year, month)
    period = db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == year, LedgerPeriod.month == month))
    label = f"{month:02d}-{year}"
    link = f"/payments/maintenance?period_id={period.id}" if period else "/payments/maintenance"
    rows: list[MaintenancePayment] = []
    if period:
        rows = list(db.scalars(
            select(MaintenancePayment).where(MaintenancePayment.period_id == period.id)
            .options(selectinload(MaintenancePayment.flat))).all())
    month_over = (year, month) < (today.year, today.month)

    if not viewer.is_admin:
        out = []
        for mp in sorted((r for r in rows if r.flat_id in viewer.flat_ids), key=lambda r: r.flat.number):
            paid = mp.is_paid
            status = "paid" if paid else ("overdue" if month_over else "pending")
            out.append(_item(
                type="maintenance", title=f"Flat {mp.flat.number} maintenance {'paid' if paid else 'pending'}",
                day=last, status=status, link=link, link_label="Open payments", ident=f"maint-{year}{month:02d}-{mp.flat_id}",
                lines=[f"Month: {label}", f"Amount: ₹{mp.amount}",
                       "Paid" + (f" on {mp.paid_on:%d-%m-%Y}" if mp.paid_on else "") if paid
                       else ("Overdue: the month has passed." if month_over else f"Due by the end of the month ({last:%d-%m-%Y}).")]))
        return out

    pending = [r for r in rows if not r.is_paid]
    paid_n = len(rows) - len(pending)
    status = "paid" if rows and not pending else ("overdue" if month_over and pending else "pending")
    lines = [f"Month: {label}"]
    if rows:
        lines.append(f"Paid: {paid_n} of {len(rows)} flats")
        if pending:
            lines.append("Pending: " + ", ".join(f"{r.flat.number} (₹{r.amount})" for r in sorted(pending, key=lambda r: r.flat.number)))
    else:
        lines.append("This month is not opened in the ledger yet.")
    title = f"Maintenance due {label}" + (f": {len(pending)} pending" if pending else (": all paid" if rows else ""))
    return [_item(type="maintenance", title=title, day=last, status=status, lines=lines, link=link,
                  link_label="Open payments", ident=f"maint-{year}{month:02d}")]


def _corpus_items(db: Session, start: date, end: date, today: date, viewer: Viewer) -> list[dict]:
    items: list[dict] = []
    for campaign in campaigns_svc.active_campaigns(db):
        if not campaign.due_date or not (start <= campaign.due_date <= end):
            continue
        summary = campaigns_svc.campaign_summary(db, campaign, today)
        rows = summary["rows"] if viewer.is_admin else [r for r in summary["rows"] if r["flat"].id in viewer.flat_ids and r["share"] > 0]
        if not rows:
            continue
        pending = [r for r in rows if r["remaining"] > 0]
        status = "paid" if not pending else ("overdue" if campaign.due_date < today else "pending")
        lines = []
        if viewer.is_admin:
            lines.append(f"Collected ₹{summary['paid_total']} of ₹{summary['share_total']}")
            if pending:
                lines.append("Pending: " + ", ".join(f"{r['flat'].number} (₹{r['remaining']})" for r in pending))
            title = f"Corpus due: {campaign.name}"
        else:
            for r in rows:
                lines.append(f"Flat {r['flat'].number}: " + (f"₹{r['remaining']} remaining of ₹{r['share']}" if r["remaining"] > 0 else "paid in full"))
            title = f"Corpus due: {campaign.name}"
        if status == "overdue":
            lines.append("Overdue: the due date has passed.")
        items.append(_item(type="corpus", title=title, day=campaign.due_date, status=status, lines=lines,
                           link=f"/corpus/goals/{campaign.id}", link_label="Open goal", ident=f"corpus-{campaign.id}"))
    return items


def _project_items(db: Session, start: date, end: date, today: date, viewer: Viewer) -> list[dict]:
    items: list[dict] = []
    projects = db.scalars(
        select(Project).where(Project.deleted_at.is_(None), Project.due_date >= start, Project.due_date <= end)
        .order_by(Project.due_date, Project.id)).all()
    flats = {f.id: f for f in db.scalars(select(Flat)).all()}
    for project in projects:
        shares = list(db.scalars(select(ProjectPayment).where(ProjectPayment.project_id == project.id)).all())
        if not viewer.is_admin:
            shares = [s for s in shares if s.flat_id in viewer.flat_ids]
        if not shares:
            continue
        pending = [s for s in shares if s.remaining > 0]
        status = "paid" if not pending else ("overdue" if project.due_date < today else "pending")
        if viewer.is_admin:
            lines = [f"Collected ₹{sum(s.paid for s in shares)} of ₹{sum(s.amount for s in shares)}"]
            if pending:
                lines.append("Pending: " + ", ".join(
                    f"{flats[s.flat_id].number} (₹{s.remaining})" for s in sorted(pending, key=lambda s: flats[s.flat_id].number)))
            link = f"/projects/{project.id}"
        else:
            lines = [f"Flat {flats[s.flat_id].number}: " + (f"₹{s.remaining} remaining of ₹{s.amount}" if s.remaining > 0 else "paid in full")
                     for s in sorted(shares, key=lambda s: flats[s.flat_id].number)]
            link = "/payments/projects"
        if status == "overdue":
            lines.append("Overdue: the due date has passed.")
        items.append(_item(type="project", title=f"Project due: {project.name}", day=project.due_date, status=status,
                           lines=lines, link=link, link_label="Open project" if viewer.is_admin else "Open payments",
                           ident=f"project-{project.id}"))
    return items


# ---- custom events ------------------------------------------------------------------------


def _events_in_range(db: Session, start: date, end: date, viewer: Viewer) -> list[CalendarEvent]:
    q = select(CalendarEvent).where(
        CalendarEvent.event_date <= end,
        (CalendarEvent.end_date >= start) | ((CalendarEvent.end_date.is_(None)) & (CalendarEvent.event_date >= start)),
    ).order_by(CalendarEvent.event_date, CalendarEvent.start_time, CalendarEvent.id)
    if not viewer.is_admin:
        q = q.where(CalendarEvent.visible_to_owners.is_(True))
    return list(db.scalars(q).all())


def time_label(ev: CalendarEvent) -> str | None:
    if ev.start_time and ev.end_time:
        return f"{ev.start_time}–{ev.end_time}"
    return ev.start_time or None


def _event_items(db: Session, start: date, end: date, viewer: Viewer) -> list[dict]:
    items: list[dict] = []
    for ev in _events_in_range(db, start, end, viewer):
        lines = [f"Type: {ev.event_type}"]
        if ev.end_date and ev.end_date > ev.event_date:
            lines.append(f"Until: {ev.end_date:%d-%m-%Y}")
        if time_label(ev):
            lines.append(f"Time: {time_label(ev)}")
        if ev.notes:
            lines.append(ev.notes)
        if viewer.is_admin and not ev.visible_to_owners:
            lines.append("Hidden from owners.")
        items.append(_item(type="event", title=ev.title, day=ev.event_date, end_day=ev.end_date, time=time_label(ev),
                           status="hidden" if viewer.is_admin and not ev.visible_to_owners else "open", lines=lines, event_id=ev.id if viewer.is_admin else None, ident=f"event-{ev.id}",
                           link=f"/calendar/events/{ev.id}/edit" if viewer.is_admin else None,
                           link_label="Edit event" if viewer.is_admin else None))
    return items


# ---- month view ---------------------------------------------------------------------------


def month_items(db: Session, year: int, month: int, viewer: Viewer, today: date | None = None) -> list[dict]:
    today = today or date.today()
    start, end = month_bounds(year, month)
    items = (
        _event_items(db, start, end, viewer)
        + _task_items(db, start, end, today, viewer)
        + _done_items(db, start, end, viewer)
        + _maintenance_items(db, year, month, today, viewer)
        + _corpus_items(db, start, end, today, viewer)
        + _project_items(db, start, end, today, viewer)
    )
    order = {"event": 0, "maintenance": 1, "corpus": 2, "project": 3, "task": 4, "done": 5}
    items.sort(key=lambda i: (i["date"], i["time"] or "", order[i["type"]], i["title"]))
    return items


def summary(items: list[dict]) -> dict:
    return {
        "total": len(items),
        "events": sum(1 for i in items if i["type"] == "event"),
        "overdue": sum(1 for i in items if i["status"] == "overdue"),
        "tasks": sum(1 for i in items if i["type"] == "task"),
    }


def expand_days(items: list[dict], start: date, end: date) -> dict[date, list[dict]]:
    """Items per day; a multi-day event appears on each of its days inside the month."""
    days: dict[date, list[dict]] = {}
    for item in items:
        last = item["end_date"] or item["date"]
        day = max(item["date"], start)
        while day <= min(last, end):
            days.setdefault(day, []).append(item)
            day += timedelta(days=1)
    return days


def month_grid(year: int, month: int, days: dict[date, list[dict]]) -> list[list[dict]]:
    weeks = []
    for week in pycal.Calendar(firstweekday=0).monthdatescalendar(year, month):
        weeks.append([{"date": d, "in_month": d.month == month, "items": days.get(d, []) if d.month == month else []} for d in week])
    return weeks


def agenda(days: dict[date, list[dict]]) -> list[dict]:
    return [{"date": d, "items": days[d]} for d in sorted(days)]


def month_options(year: int, month: int, today: date | None = None) -> list[tuple[str, str]]:
    today = today or date.today()
    keys = {month_key(*shift_month(today.year, today.month, d)) for d in range(-12, 25)}
    keys.add(month_key(year, month))
    return [(k, f"{k[5:]}-{k[:4]}") for k in sorted(keys)]


def json_item(item: dict) -> dict:
    """What the details panel receives: dates already in dd-mm-yyyy."""
    return {
        "id": item["id"], "type": item["type"], "type_label": item["type_label"], "title": item["title"],
        "date": item["date"].strftime("%d-%m-%Y"),
        "end_date": item["end_date"].strftime("%d-%m-%Y") if item["end_date"] else None,
        "time": item["time"], "status": item["status"], "lines": item["lines"], "link": item["link"],
        "link_label": item["link_label"], "projected": item["projected"], "event_id": item["event_id"],
    }


# ---- admin CRUD ---------------------------------------------------------------------------


def _clean_time(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%H:%M").strftime("%H:%M")
    except ValueError as exc:
        raise EventError("Time must look like 14:30.") from exc


def _clean_date(raw: str, what: str, *, required: bool) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        if required:
            raise EventError(f"{what} is required.")
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise EventError(f"{what} is not a valid date.") from exc


def clean_event_fields(*, title: str, event_date: str, end_date: str, start_time: str, end_time: str,
                       event_type: str, notes: str, visible_to_owners: bool) -> dict:
    title = (title or "").strip()
    if not title:
        raise EventError("Title is required.")
    if len(title) > 200:
        raise EventError("Title is too long (200 characters at most).")
    day = _clean_date(event_date, "Date", required=True)
    end = _clean_date(end_date, "End date", required=False)
    if end and end < day:
        raise EventError("End date cannot be before the start date.")
    if end == day:
        end = None
    start_t, end_t = _clean_time(start_time), _clean_time(end_time)
    if end_t and not start_t:
        raise EventError("Add a start time before an end time.")
    if start_t and end_t and end_t <= start_t and not end:
        raise EventError("End time must be after the start time.")
    if event_type not in EVENT_TYPES:
        event_type = "Other"
    return {"title": title, "event_date": day, "end_date": end, "start_time": start_t, "end_time": end_t,
            "event_type": event_type, "notes": (notes or "").strip() or None,
            "visible_to_owners": bool(visible_to_owners)}
