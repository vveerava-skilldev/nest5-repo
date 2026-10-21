"""Automatic monthly dues and recurring-task occurrences.

`ensure_current` is the single idempotent routine used by the background job (startup + hourly), by
the lazy catch-up on page loads, and by tests with a fake clock. It:

* creates the current IST month's ledger period (never earlier months, never closes a month),
* gives every flat its maintenance row for that month (fee_for_period honours revised fees; a flat
  added mid-month gets its row on the next run),
* seeds/generates the recurring admin tasks for the month.

Fund maths: `compute_fund_balance` takes the LAST period's opening + collections - settlements, so a
new month cannot start at 0. It is created with opening = the previous period's closing (stored when
closed, computed otherwise) and `opening_carried=True`; while the previous month is still open the
opening follows it (ledger.refresh_closed_periods), and closing that month later finalises it. Nothing
is counted twice: the previous month's collections live only in its own rows.

Run a single uvicorn worker: the lock below is process-local.
"""
import asyncio
import logging
import threading
import time
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.dates import ist_today
from app.models import Flat, LedgerPeriod, MaintenancePayment
from app.services import audit as audit_svc
from app.services import ledger as ledger_svc
from app.services import payment_details as payment_details_svc
from app.services import recurring as recurring_svc

log = logging.getLogger("nest5.monthly")

DUE_DAY = 5
LAZY_TTL_SECONDS = 60

_lock = threading.Lock()
_lazy = {"day": None, "at": 0.0}


def due_date_for_month(year: int, month: int) -> date:
    return date(year, month, DUE_DAY)


def _previous_period(db: Session, year: int, month: int) -> LedgerPeriod | None:
    return db.scalar(
        select(LedgerPeriod)
        .where((LedgerPeriod.year < year) | ((LedgerPeriod.year == year) & (LedgerPeriod.month < month)))
        .order_by(LedgerPeriod.year.desc(), LedgerPeriod.month.desc())
        .limit(1)
    )


def _has_later_period(db: Session, year: int, month: int) -> bool:
    return db.scalar(
        select(LedgerPeriod.id)
        .where((LedgerPeriod.year > year) | ((LedgerPeriod.year == year) & (LedgerPeriod.month > month)))
        .limit(1)
    ) is not None


def _create_period(db: Session, year: int, month: int) -> LedgerPeriod:
    prev = _previous_period(db, year, month)
    opening, carried = 0, None
    if prev is not None:
        if prev.is_closed and prev.closing_fund_balance is not None:
            opening, carried = prev.closing_fund_balance, False
        else:
            opening = ledger_svc.compute_fund_balance(db, prev)
            carried = True
    period = LedgerPeriod(
        year=year, month=month, opening_fund_balance=opening, corpus_opening=0, opening_carried=carried
    )
    db.add(period)
    db.flush()
    return period


def ensure_current(db: Session, today: date | None = None) -> dict:
    """Idempotent. `today` is the IST date (injectable for tests). Commits only when something changed."""
    today = today or ist_today()
    result = {"period_created": False, "rows_added": 0, "tasks_seeded": 0, "occurrences_added": 0}
    with _lock:
        year, month = today.year, today.month
        period = db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == year, LedgerPeriod.month == month))
        if period is None and not _has_later_period(db, year, month):
            period = _create_period(db, year, month)
            result["period_created"] = True
        if period is not None:
            before = db.scalar(
                select(func.count()).select_from(MaintenancePayment).where(MaintenancePayment.period_id == period.id)
            ) or 0
            ledger_svc.ensure_maintenance_rows(db, period)
            after = db.scalar(
                select(func.count()).select_from(MaintenancePayment).where(MaintenancePayment.period_id == period.id)
            ) or 0
            result["rows_added"] = after - before
            if result["period_created"]:
                audit_svc.log_audit(
                    db, entity_type="ledger_period", entity_id=period.id, action="auto_create", user_label="system",
                    after={
                        "month": f"{month:02d}-{year}", "opening": period.opening_fund_balance,
                        "opening_carried": bool(period.opening_carried), "flats": after,
                        "due_date": due_date_for_month(year, month),
                    },
                )
        result["tasks_seeded"] = recurring_svc.ensure_defaults(db, today)
        result["occurrences_added"] = recurring_svc.generate(db, today)
        payment_details_svc.ensure_initial(db)
        payment_details_svc.backfill_payees(db)
        db.commit()  # earlier steps flush; committing an unchanged transaction is a no-op
    return result


def lazy_catch_up(db: Session) -> None:
    """Called when pages load: a missed run heals itself. Cheap (cached for LAZY_TTL_SECONDS)."""
    day = ist_today()
    now = time.monotonic()
    if _lazy["day"] == day and now - _lazy["at"] < LAZY_TTL_SECONDS:
        return
    try:
        ensure_current(db, day)
    except Exception:  # noqa: BLE001 - a failed catch-up must never break the page
        db.rollback()
        log.exception("Lazy monthly catch-up failed")
        return
    _lazy["day"], _lazy["at"] = day, now


def reset_cache() -> None:
    _lazy["day"], _lazy["at"] = None, 0.0


def run_job_once() -> dict | None:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        result = ensure_current(db)
        reset_cache()
        if result["period_created"] or result["rows_added"] or result["occurrences_added"]:
            log.info("Monthly job: %s", result)
        return result
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("Monthly job failed")
        return None
    finally:
        db.close()


async def job_loop(interval_seconds: int | None = None) -> None:
    interval = max(int(interval_seconds or settings.monthly_job_interval_seconds), 30)
    while True:
        await asyncio.to_thread(run_job_once)
        await asyncio.sleep(interval)
