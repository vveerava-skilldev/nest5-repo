"""Prepare a Nest5 ledger database: flat roster, assets and payment settings; demo data only with --sample."""
from datetime import date
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal, engine, Base
from app.models import (
    Complaint,
    Flat,
    LedgerPeriod,
    MaintenancePayment,
)
from app.services import ledger as ledger_svc
from app.services.flats import sync_flat_roster
from app.services.schema import add_missing_columns

Base.metadata.create_all(bind=engine)
add_missing_columns(engine)


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def seed(sample: bool | None = None) -> None:
    """Prepare a new database.

    Always: the flat roster, the standard asset register and the initial payment settings.
    Optional (`--sample` or SEED_SAMPLE_DATA=1): demo Sep/Oct 2026 months with every flat's maintenance
    marked paid and one open complaint. No sample expenses or settlements are ever created; a real
    install starts with an empty ledger and the app creates the current month by itself.
    """
    sample = _truthy(os.environ.get("SEED_SAMPLE_DATA")) if sample is None else sample
    db = SessionLocal()
    try:
        sync_flat_roster(db)
        if db.scalar(select(LedgerPeriod).limit(1)):
            db.commit()
            print("Flat roster synced; ledger already has data, nothing else seeded.")
            return
        if not sample:
            _finish(db)
            print("Seeded the flat roster and payment settings (no sample ledger data; use --sample for demo data).")
            return

        sept = LedgerPeriod(year=2026, month=9, opening_fund_balance=0, corpus_opening=0)
        db.add(sept)
        db.flush()
        ledger_svc.ensure_maintenance_rows(db, sept)

        for mp in db.scalars(
            select(MaintenancePayment).where(MaintenancePayment.period_id == sept.id)
        ).all():
            mp.is_paid = True
            mp.paid_on = date(2026, 9, 15)
            mp.payment_note = "Monthly maintenance"
        db.flush()

        sept_closing = ledger_svc.maintenance_collected(db, sept.id)
        sept.closing_fund_balance = sept_closing
        sept.is_closed = True

        oct_p = LedgerPeriod(
            year=2026,
            month=10,
            opening_fund_balance=sept_closing,
            corpus_opening=0,
        )
        db.add(oct_p)
        db.flush()
        ledger_svc.ensure_maintenance_rows(db, oct_p)
        for mp in db.scalars(
            select(MaintenancePayment).where(MaintenancePayment.period_id == oct_p.id)
        ).all():
            mp.is_paid = True
            mp.paid_on = date(2026, 10, 10)
            mp.payment_note = "Monthly maintenance"

        db.add(
            Complaint(
                title="Lift doors",
                description="Lift door issue logged in October notebook.",
                reported_on=date(2026, 10, 3),
                status="open",
            )
        )
        _finish(db)
        balance = ledger_svc.compute_fund_balance(db)
        print(f"Seeded sample ledger (Sep-Oct 2026, maintenance only). Fund balance: ₹{balance}")
    finally:
        db.close()


def _finish(db) -> None:
    from app.services import payment_details as payment_details_svc
    from app.services import schedule as schedule_svc

    # v9.1: the register starts with its standard assets but no sample tasks, so a new
    # install has no asset alerts; the admin adds real assets and tasks later.
    schedule_svc.ensure_assets(db)
    payment_details_svc.ensure_initial(db)
    payment_details_svc.backfill_payees(db)
    db.commit()


if __name__ == "__main__":
    seed(sample=True if "--sample" in sys.argv[1:] else None)
