from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LedgerPeriod
from app.services import ledger as ledger_svc


def list_periods_chronological(db: Session) -> list[LedgerPeriod]:
    return list(
        db.scalars(select(LedgerPeriod).order_by(LedgerPeriod.year, LedgerPeriod.month)).all()
    )


def resolve_period(db: Session, period_id: int | None = None) -> LedgerPeriod | None:
    if period_id:
        return db.get(LedgerPeriod, period_id)
    today = date.today()
    current = db.scalar(
        select(LedgerPeriod).where(
            LedgerPeriod.year == today.year, LedgerPeriod.month == today.month
        )
    )
    if current:
        return current
    return db.scalar(
        select(LedgerPeriod).order_by(LedgerPeriod.year.desc(), LedgerPeriod.month.desc())
    )


def period_nav(db: Session, period: LedgerPeriod | None) -> dict:
    periods = list_periods_chronological(db)
    if not period or not periods:
        return {"prev": None, "next": None, "periods": periods}
    idx = next((i for i, p in enumerate(periods) if p.id == period.id), None)
    prev_p = periods[idx - 1] if idx is not None and idx > 0 else None
    next_p = periods[idx + 1] if idx is not None and idx < len(periods) - 1 else None
    return {"prev": prev_p, "next": next_p, "periods": periods}


def period_context(db: Session, period: LedgerPeriod | None) -> dict:
    if not period:
        return {
            "period": None,
            "summary": None,
            "nav": period_nav(db, None),
            "label": "",
        }
    summary = ledger_svc.period_fund_summary(db, period)
    return {
        "period": period,
        "summary": summary,
        "nav": period_nav(db, period),
        "label": ledger_svc.month_label(period),
    }
