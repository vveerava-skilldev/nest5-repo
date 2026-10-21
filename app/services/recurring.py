"""Admin-only recurring tasks (Watchman salary, Common EB, lift check ...).

A task repeats every `interval_months` on `day_of_month` (clamped to the month's length). Each month's
occurrence is created lazily and idempotently (see `generate`, called by the monthly job and the lazy
catch-up). Marking an occurrence done records the date, an optional amount and a note; it never creates
an expense. Overdue means the due date has passed and the occurrence is not done.
"""
import calendar as pycal
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.dates import ist_today
from app.models import RecurringTask, RecurringTaskOccurrence
from app.services import audit as audit_svc

DUE_SOON_DAYS = 5
MAX_NOTE = 255
MODES = {"by": "by the", "before": "before the"}

DEFAULTS = (
    # seed_key, title, day, mode
    ("watchman_salary", "Watchman salary payment", 5, "by"),
    ("common_eb", "Common EB (electricity) payment", 10, "before"),
)


class RecurringError(ValueError):
    pass


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def due_label(task: RecurringTask) -> str:
    """'by 5th' / 'before 10th' (the occurrence is due on that day)."""
    return f"{task.due_mode if task.due_mode in MODES else 'by'} {ordinal(task.day_of_month)}"


def repeat_label(task: RecurringTask) -> str:
    n = task.interval_months or 1
    return "Every month" if n == 1 else f"Every {n} months"


def month_index(year: int, month: int) -> int:
    return year * 12 + month - 1


def due_date_for(task: RecurringTask, year: int, month: int) -> date:
    return date(year, month, min(task.day_of_month, pycal.monthrange(year, month)[1]))


def applies_to(task: RecurringTask, year: int, month: int) -> bool:
    """True when `task` falls due in this month (on or after its start, on its interval)."""
    gap = month_index(year, month) - month_index(task.start_year, task.start_month)
    return gap >= 0 and gap % max(task.interval_months or 1, 1) == 0


def first_open_month(day: int, today: date) -> tuple[int, int]:
    """First month whose due day has not passed yet, so a new task is never instantly overdue."""
    if today.day <= day:
        return today.year, today.month
    return (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)


# ---- defaults and generation -----------------------------------------------------------------


def ensure_defaults(db: Session, today: date | None = None) -> int:
    today = today or ist_today()
    added = 0
    for key, title, day, mode in DEFAULTS:
        if db.scalar(select(RecurringTask).where(RecurringTask.seed_key == key)):
            continue
        year, month = first_open_month(day, today)
        db.add(
            RecurringTask(
                title=title, day_of_month=day, due_mode=mode, interval_months=1,
                start_year=year, start_month=month, is_active=True, seed_key=key, created_by="system",
            )
        )
        added += 1
    if added:
        db.flush()
    return added


def generate(db: Session, today: date | None = None) -> int:
    """Create the current month's occurrence of every active task that falls due this month."""
    today = today or ist_today()
    made = 0
    for task in db.scalars(select(RecurringTask).where(RecurringTask.is_active.is_(True))).all():
        if not applies_to(task, today.year, today.month):
            continue
        exists = db.scalar(
            select(RecurringTaskOccurrence.id).where(
                RecurringTaskOccurrence.task_id == task.id,
                RecurringTaskOccurrence.year == today.year,
                RecurringTaskOccurrence.month == today.month,
            )
        )
        if exists:
            continue
        db.add(
            RecurringTaskOccurrence(
                task_id=task.id, year=today.year, month=today.month,
                due_date=due_date_for(task, today.year, today.month),
            )
        )
        made += 1
    if made:
        db.flush()
    return made


# ---- queries ---------------------------------------------------------------------------------


def list_tasks(db: Session, include_inactive: bool = True) -> list[RecurringTask]:
    q = select(RecurringTask).order_by(RecurringTask.is_active.desc(), RecurringTask.day_of_month, RecurringTask.id)
    if not include_inactive:
        q = q.where(RecurringTask.is_active.is_(True))
    return list(db.scalars(q).all())


def status_for(occ: RecurringTaskOccurrence, today: date) -> str:
    if occ.done_on:
        return "done"
    if occ.due_date < today:
        return "overdue"
    if (occ.due_date - today).days <= DUE_SOON_DAYS:
        return "due_soon"
    return "open"


def open_occurrences(db: Session, today: date | None = None) -> list[dict]:
    """Not-done occurrences of active tasks that are overdue or due within DUE_SOON_DAYS, most urgent first."""
    today = today or ist_today()
    out = []
    rows = db.scalars(
        select(RecurringTaskOccurrence)
        .join(RecurringTask, RecurringTaskOccurrence.task_id == RecurringTask.id)
        .where(RecurringTask.is_active.is_(True), RecurringTaskOccurrence.done_on.is_(None))
        .options(selectinload(RecurringTaskOccurrence.task))
        .order_by(RecurringTaskOccurrence.due_date, RecurringTaskOccurrence.id)
    ).all()
    for occ in rows:
        status = status_for(occ, today)
        if status in ("overdue", "due_soon"):
            out.append({"occurrence": occ, "task": occ.task, "status": status, "due_date": occ.due_date,
                        "days": (occ.due_date - today).days})
    return out


def occurrences_for_month(db: Session, year: int, month: int, today: date | None = None) -> list[dict]:
    """Real occurrences for a month, plus projected ones for months after the current month."""
    today = today or ist_today()
    real = {
        o.task_id: o
        for o in db.scalars(
            select(RecurringTaskOccurrence)
            .where(RecurringTaskOccurrence.year == year, RecurringTaskOccurrence.month == month)
            .options(selectinload(RecurringTaskOccurrence.task))
        ).all()
    }
    out = []
    for task in list_tasks(db, include_inactive=True):
        occ = real.get(task.id)
        if occ is not None:
            if task.is_active or occ.done_on:
                out.append({"task": task, "occurrence": occ, "status": status_for(occ, today),
                            "due_date": occ.due_date, "projected": False})
            continue
        if task.is_active and month_index(year, month) > month_index(today.year, today.month) and applies_to(task, year, month):
            out.append({"task": task, "occurrence": None, "status": "open",
                        "due_date": due_date_for(task, year, month), "projected": True})
    return out


def recent_done(db: Session, limit: int = 12) -> list[RecurringTaskOccurrence]:
    return list(
        db.scalars(
            select(RecurringTaskOccurrence)
            .where(RecurringTaskOccurrence.done_on.isnot(None))
            .options(selectinload(RecurringTaskOccurrence.task))
            .order_by(RecurringTaskOccurrence.done_at.desc(), RecurringTaskOccurrence.id.desc())
            .limit(limit)
        ).all()
    )


def current_occurrences(db: Session, today: date | None = None) -> list[dict]:
    """The admin page list: every not-done occurrence (any month) plus this month's done ones."""
    today = today or ist_today()
    rows = db.scalars(
        select(RecurringTaskOccurrence)
        .join(RecurringTask, RecurringTaskOccurrence.task_id == RecurringTask.id)
        .where(
            RecurringTask.is_active.is_(True),
            (RecurringTaskOccurrence.done_on.is_(None))
            | ((RecurringTaskOccurrence.year == today.year) & (RecurringTaskOccurrence.month == today.month)),
        )
        .options(selectinload(RecurringTaskOccurrence.task))
        .order_by(RecurringTaskOccurrence.due_date, RecurringTaskOccurrence.id)
    ).all()
    return [{"occurrence": o, "task": o.task, "status": status_for(o, today), "due_date": o.due_date} for o in rows]


# ---- admin actions ---------------------------------------------------------------------------


def _snap(task: RecurringTask) -> dict:
    return {"title": task.title, "day_of_month": task.day_of_month, "due_mode": task.due_mode,
            "interval_months": task.interval_months, "is_active": task.is_active, "notes": task.notes}


def clean_task_fields(*, title: str, day_of_month: str | int, due_mode: str, interval_months: str | int,
                      notes: str = "") -> dict:
    title = " ".join((title or "").split())
    if not title:
        raise RecurringError("Title is required.")
    if len(title) > 200:
        raise RecurringError("Title is too long (200 characters at most).")
    try:
        day = int(day_of_month)
    except (TypeError, ValueError) as exc:
        raise RecurringError("Day of month must be a number from 1 to 31.") from exc
    if not 1 <= day <= 31:
        raise RecurringError("Day of month must be from 1 to 31 (short months use their last day).")
    try:
        interval = int(interval_months or 1)
    except (TypeError, ValueError) as exc:
        raise RecurringError("Repeat interval must be a whole number of months (1 to 12).") from exc
    if not 1 <= interval <= 12:
        raise RecurringError("Repeat interval must be from 1 to 12 months.")
    return {"title": title, "day_of_month": day, "due_mode": due_mode if due_mode in MODES else "by",
            "interval_months": interval, "notes": (notes or "").strip() or None}


def create_task(db: Session, *, actor: str = "admin", today: date | None = None, **fields) -> RecurringTask:
    today = today or ist_today()
    clean = clean_task_fields(**fields)
    year, month = first_open_month(clean["day_of_month"], today)
    task = RecurringTask(**clean, start_year=year, start_month=month, is_active=True, created_by=actor)
    db.add(task)
    db.flush()
    generate(db, today)
    audit_svc.log_audit(db, entity_type="recurring_task", entity_id=task.id, action="create", user_label=actor,
                        after=_snap(task))
    return task


def update_task(db: Session, task: RecurringTask, *, actor: str = "admin", today: date | None = None,
                **fields) -> RecurringTask:
    today = today or ist_today()
    clean = clean_task_fields(**fields)
    before = _snap(task)
    for key, value in clean.items():
        setattr(task, key, value)
    # Not-done occurrences from this month on follow the new day; done ones keep what was recorded.
    for occ in db.scalars(
        select(RecurringTaskOccurrence).where(
            RecurringTaskOccurrence.task_id == task.id, RecurringTaskOccurrence.done_on.is_(None)
        )
    ).all():
        if month_index(occ.year, occ.month) >= month_index(today.year, today.month):
            occ.due_date = due_date_for(task, occ.year, occ.month)
    db.flush()
    generate(db, today)
    audit_svc.log_audit(db, entity_type="recurring_task", entity_id=task.id, action="update", user_label=actor,
                        before=before, after=_snap(task))
    return task


def set_active(db: Session, task: RecurringTask, active: bool, *, actor: str = "admin",
               today: date | None = None) -> None:
    if bool(task.is_active) == bool(active):
        return
    before = _snap(task)
    task.is_active = bool(active)
    db.flush()
    if active:
        generate(db, today or ist_today())
    audit_svc.log_audit(db, entity_type="recurring_task", entity_id=task.id,
                        action="enable" if active else "disable", user_label=actor, before=before, after=_snap(task))


def mark_done(db: Session, occ: RecurringTaskOccurrence, *, done_on: str, amount: str = "", note: str = "",
              actor: str = "admin") -> RecurringTaskOccurrence:
    try:
        day = date.fromisoformat((done_on or "")[:10])
    except ValueError as exc:
        raise RecurringError("Done date is not a valid date.") from exc
    value = None
    raw = (amount or "").replace(",", "").replace("₹", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError as exc:
            raise RecurringError("Amount must be a whole number of rupees.") from exc
        if value < 0:
            raise RecurringError("Amount cannot be negative.")
    note = (note or "").strip()
    if len(note) > MAX_NOTE:
        raise RecurringError(f"Note is too long ({MAX_NOTE} characters at most).")
    if occ.done_on:
        raise RecurringError("This occurrence is already marked done.")
    occ.done_on, occ.amount, occ.note = day, value, note or None
    occ.done_by, occ.done_at = actor, datetime.utcnow()
    db.flush()
    audit_svc.log_audit(
        db, entity_type="recurring_task_occurrence", entity_id=occ.id, action="done", user_label=actor,
        after={"task": occ.task.title, "month": f"{occ.month:02d}-{occ.year}", "due_date": occ.due_date,
               "done_on": day, "amount": value, "note": occ.note},
    )
    return occ


def undo_done(db: Session, occ: RecurringTaskOccurrence, *, actor: str = "admin") -> RecurringTaskOccurrence:
    if not occ.done_on:
        raise RecurringError("This occurrence is not marked done.")
    before = {"task": occ.task.title, "month": f"{occ.month:02d}-{occ.year}", "done_on": occ.done_on,
              "amount": occ.amount, "note": occ.note}
    occ.done_on = occ.amount = occ.note = occ.done_by = occ.done_at = None
    db.flush()
    audit_svc.log_audit(db, entity_type="recurring_task_occurrence", entity_id=occ.id, action="undo",
                        user_label=actor, before=before,
                        after={"task": occ.task.title, "month": f"{occ.month:02d}-{occ.year}"})
    return occ
