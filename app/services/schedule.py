from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Asset, AssetMaintenanceLog, AssetMaintenanceSchedule, ScheduleFrequency

ASSET_TYPES = ("Lift", "Generator", "Water tank", "Motor/pump", "Fire safety", "CCTV / security",
               "Electrical", "Plumbing", "Other")
DEFAULT_ASSETS = ("Lift", "Generator", "Water tank", "Motor/pump")
_TYPE_KEYWORDS = (("lift", "Lift"), ("elevator", "Lift"), ("generator", "Generator"),
                  ("tank", "Water tank"), ("motor", "Motor/pump"), ("pump", "Motor/pump"),
                  ("fire", "Fire safety"), ("cctv", "CCTV / security"))
UPCOMING_DAYS = 30


def guess_type(name: str | None) -> str:
    low = (name or "").lower()
    return next((kind for word, kind in _TYPE_KEYWORDS if word in low), "Other")


def add_interval(start: date, frequency: ScheduleFrequency, custom_days: int | None) -> date:
    if frequency == ScheduleFrequency.DAILY:
        return start + timedelta(days=1)
    if frequency == ScheduleFrequency.WEEKLY:
        return start + timedelta(days=7)
    if frequency == ScheduleFrequency.MONTHLY:
        month = start.month + 1
        year = start.year
        if month > 12:
            month = 1
            year += 1
        day = min(start.day, 28)
        return date(year, month, day)
    if frequency == ScheduleFrequency.QUARTERLY:
        month = start.month + 3
        year = start.year
        while month > 12:
            month -= 12
            year += 1
        day = min(start.day, 28)
        return date(year, month, day)
    if frequency == ScheduleFrequency.YEARLY:
        return date(start.year + 1, start.month, min(start.day, 28))
    days = custom_days or 30
    return start + timedelta(days=days)


def compute_next_due(
    last_done: date | None,
    frequency: ScheduleFrequency,
    custom_days: int | None,
    explicit_next: date | None = None,
) -> date | None:
    if explicit_next:
        return explicit_next
    if last_done:
        return add_interval(last_done, frequency, custom_days)
    return date.today()


def schedule_status(item: AssetMaintenanceSchedule, today: date | None = None) -> str:
    today = today or date.today()
    if not item.next_due_on:
        return "unknown"
    if item.next_due_on < today:
        return "overdue"
    if item.next_due_on <= today + timedelta(days=7):
        return "due_soon"
    return "ok"


def list_schedules(db: Session) -> list[AssetMaintenanceSchedule]:
    return list(
        db.scalars(
            select(AssetMaintenanceSchedule)
            .where(AssetMaintenanceSchedule.deleted_at.is_(None))
            .order_by(AssetMaintenanceSchedule.next_due_on.nulls_last(), AssetMaintenanceSchedule.asset)
        ).all()
    )


def schedule_buckets(db: Session, within_days: int = 7) -> dict:
    today = date.today()
    horizon = today + timedelta(days=within_days)
    overdue: list[AssetMaintenanceSchedule] = []
    upcoming: list[AssetMaintenanceSchedule] = []
    ok: list[AssetMaintenanceSchedule] = []
    for item in list_schedules(db):
        if not item.next_due_on:
            ok.append(item)
            continue
        if item.next_due_on < today:
            overdue.append(item)
        elif item.next_due_on <= horizon:
            upcoming.append(item)
        else:
            ok.append(item)
    return {"overdue": overdue, "upcoming": upcoming, "ok": ok, "today": today, "within_days": within_days}


def list_assets(db: Session) -> list[Asset]:
    return list(db.scalars(select(Asset).where(Asset.deleted_at.is_(None)).order_by(Asset.name)).all())


def asset_by_name(db: Session, name: str) -> Asset | None:
    return db.scalar(select(Asset).where(Asset.deleted_at.is_(None), func.lower(Asset.name) == name.strip().lower()))


def get_or_create_asset(db: Session, name: str) -> Asset:
    asset = asset_by_name(db, name)
    if asset is None:
        asset = Asset(name=name.strip(), asset_type=guess_type(name))
        db.add(asset)
        db.flush()
    return asset


def ensure_assets(db: Session) -> int:
    """Link schedule rows that only carry an asset name to the register; add the standard assets."""
    linked = 0
    for task in db.scalars(select(AssetMaintenanceSchedule).where(
            AssetMaintenanceSchedule.asset_id.is_(None), AssetMaintenanceSchedule.deleted_at.is_(None))).all():
        task.asset_id = get_or_create_asset(db, task.asset).id
        linked += 1
    for name in DEFAULT_ASSETS:
        if db.scalar(select(Asset.id).where(func.lower(Asset.name) == name.lower())) is None:
            get_or_create_asset(db, name)
            linked += 1
    return linked


def tasks_for(db: Session, asset: Asset) -> list[AssetMaintenanceSchedule]:
    return list(db.scalars(
        select(AssetMaintenanceSchedule)
        .where(AssetMaintenanceSchedule.asset_id == asset.id, AssetMaintenanceSchedule.deleted_at.is_(None))
        .order_by(AssetMaintenanceSchedule.next_due_on.nulls_last(), AssetMaintenanceSchedule.task)
    ).all())


def history(db: Session, asset: Asset) -> list[AssetMaintenanceLog]:
    return list(db.scalars(
        select(AssetMaintenanceLog).where(AssetMaintenanceLog.asset_id == asset.id)
        .order_by(AssetMaintenanceLog.done_on.desc(), AssetMaintenanceLog.id.desc())
    ).all())


def record_log(db: Session, asset: Asset, task: AssetMaintenanceSchedule | None, done_on: date, *,
               done_by: str | None = None, notes: str | None = None, cost: int | None = None,
               created_by: str | None = None) -> AssetMaintenanceLog:
    """Save a check/service; when it is for a task, move that task's last done / next due forward."""
    next_due = None
    if task is not None:
        if task.last_done_on is None or done_on >= task.last_done_on:
            task.last_done_on = done_on
            task.next_due_on = add_interval(done_on, task.frequency, task.custom_interval_days)
        next_due = add_interval(done_on, task.frequency, task.custom_interval_days)
    log = AssetMaintenanceLog(asset_id=asset.id, schedule_id=task.id if task else None, done_on=done_on,
                              done_by=done_by or None, notes=notes or None, cost=cost, next_due_on=next_due,
                              created_by=created_by)
    db.add(log)
    db.flush()
    return log


def remove_log(db: Session, log: AssetMaintenanceLog) -> None:
    """Delete a log entry; its task falls back to the latest remaining entry for that task."""
    task = log.task
    db.delete(log)
    db.flush()
    if task is None or task.last_done_on != log.done_on:
        return
    latest = db.scalar(select(func.max(AssetMaintenanceLog.done_on)).where(AssetMaintenanceLog.schedule_id == task.id))
    if latest is not None:
        task.last_done_on = latest
        task.next_due_on = add_interval(latest, task.frequency, task.custom_interval_days)


def board(db: Session, today: date | None = None) -> dict:
    """Asset schedule page: overdue / due this week / upcoming, then every asset with its tasks."""
    today = today or date.today()
    week, horizon = today + timedelta(days=7), today + timedelta(days=UPCOMING_DAYS)
    overdue, this_week, upcoming = [], [], []
    for task in list_schedules(db):
        if not task.next_due_on:
            continue
        if task.next_due_on < today:
            overdue.append((task, "overdue"))
        elif task.next_due_on <= week:
            this_week.append((task, "due_soon"))
        elif task.next_due_on <= horizon:
            upcoming.append((task, "ok"))
    last_logs = {}
    for log in db.scalars(select(AssetMaintenanceLog).order_by(AssetMaintenanceLog.done_on, AssetMaintenanceLog.id)).all():
        last_logs[log.asset_id] = log
    assets = [
        {"asset": a, "tasks": [(t, schedule_status(t, today)) for t in tasks_for(db, a)], "last_log": last_logs.get(a.id)}
        for a in list_assets(db)
    ]
    return {"overdue": overdue, "this_week": this_week, "upcoming": upcoming, "assets": assets,
            "today": today, "upcoming_days": UPCOMING_DAYS}


def soft_delete_asset(db: Session, asset: Asset) -> None:
    now = datetime.utcnow()
    asset.deleted_at = now
    for task in tasks_for(db, asset):
        task.deleted_at = now


def clear_all_tasks(db: Session) -> dict:
    """Delete every asset task and the maintenance-log entries that belong to a task.

    Assets and general (task-less) log notes are kept. Returns what was removed.
    """
    logs = list(db.scalars(select(AssetMaintenanceLog).where(AssetMaintenanceLog.schedule_id.isnot(None))).all())
    tasks = list(db.scalars(select(AssetMaintenanceSchedule)).all())
    live = sum(1 for t in tasks if t.deleted_at is None)
    for log in logs:
        db.delete(log)
    db.flush()
    for task in tasks:
        db.delete(task)
    db.flush()
    return {"tasks": len(tasks), "live_tasks": live, "logs": len(logs)}


def task_counts(db: Session) -> dict:
    return {
        "tasks": db.scalar(select(func.count(AssetMaintenanceSchedule.id))) or 0,
        "logs": db.scalar(select(func.count(AssetMaintenanceLog.id)).where(AssetMaintenanceLog.schedule_id.isnot(None))) or 0,
    }
