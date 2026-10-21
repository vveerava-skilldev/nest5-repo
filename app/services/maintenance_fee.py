"""Monthly maintenance fee revisions (admin). Amount in DB; env default is fallback only."""
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dates import ist_today
from app.config import settings
from app.models import LedgerPeriod, MaintenanceFeeRevision, MaintenancePayment


def period_start(period: LedgerPeriod) -> date:
    return date(period.year, period.month, 1)


def current_maintenance_fee(db: Session, as_of: date | None = None) -> int:
    as_of = as_of or ist_today()
    rev = db.scalar(
        select(MaintenanceFeeRevision)
        .where(MaintenanceFeeRevision.effective_date <= as_of)
        .order_by(
            MaintenanceFeeRevision.effective_date.desc(),
            MaintenanceFeeRevision.id.desc(),
        )
        .limit(1)
    )
    return rev.amount if rev else settings.maintenance_fee


def fee_for_period(db: Session, period: LedgerPeriod) -> int:
    return current_maintenance_fee(db, period_start(period))


def previous_fee(db: Session, rev: MaintenanceFeeRevision) -> int:
    """Fee that applied to rev's effective month just before rev was recorded."""
    prior = db.scalar(
        select(MaintenanceFeeRevision)
        .where(
            MaintenanceFeeRevision.id < rev.id,
            MaintenanceFeeRevision.effective_date <= rev.effective_date,
        )
        .order_by(
            MaintenanceFeeRevision.effective_date.desc(),
            MaintenanceFeeRevision.id.desc(),
        )
        .limit(1)
    )
    return prior.amount if prior else settings.maintenance_fee


def parse_effective_month(raw: str) -> date:
    """Accept YYYY-MM (``type=month``), MM-YYYY (typed fallback, e.g. Safari) or YYYY-MM-DD."""
    raw = raw.strip()
    parts = raw.split("-")
    if len(parts) == 2 and len(parts[0]) == 2:
        return date(int(parts[1]), int(parts[0]), 1)
    if len(parts) >= 2 and len(parts[0]) == 4:
        return date(int(parts[0]), int(parts[1]), 1)
    raise ValueError(f"Unrecognised month: {raw!r}")


def format_month(value: date | None) -> str:
    return value.strftime("%m-%Y") if value else "—"


def _unpaid_rows(db: Session, period: LedgerPeriod) -> list[MaintenancePayment]:
    return list(
        db.scalars(
            select(MaintenancePayment).where(
                MaintenancePayment.period_id == period.id,
                MaintenancePayment.is_paid.is_(False),
            )
        ).all()
    )


def _periods_from(db: Session, effective_month: date) -> list[LedgerPeriod]:
    periods = db.scalars(select(LedgerPeriod).order_by(LedgerPeriod.year, LedgerPeriod.month)).all()
    return [p for p in periods if period_start(p) >= effective_month]


def preview_revision(db: Session, effective_month: date) -> list[dict]:
    """Existing months a new revision at effective_month would reprice (unpaid rows only).

    Months already governed by a later-effective revision keep that revision.
    """
    later = sorted(
        r.effective_date
        for r in db.scalars(
            select(MaintenanceFeeRevision).where(
                MaintenanceFeeRevision.effective_date > effective_month
            )
        ).all()
    )
    affected = []
    for period in _periods_from(db, effective_month):
        if later and later[0] <= period_start(period):
            break
        unpaid = len(_unpaid_rows(db, period))
        if unpaid:
            affected.append({"month": format_month(period_start(period)), "unpaid": unpaid})
    return affected


def reprice_unpaid_from(db: Session, effective_month: date) -> int:
    """Re-resolve unpaid rows on/after effective_month; earlier months and paid rows untouched."""
    updated = 0
    for period in _periods_from(db, effective_month):
        fee = fee_for_period(db, period)
        for mp in _unpaid_rows(db, period):
            if mp.amount != fee:
                mp.amount = fee
                updated += 1
    db.flush()
    return updated
