"""Seed Nest5 ledger from notebook data (Sep–Oct 2026)."""
from datetime import date
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal, engine, Base
from app.models import (
    Complaint,
    Expense,
    Flat,
    FundType,
    LedgerPeriod,
    MaintenancePayment,
    Settlement,
)
from app.services import ledger as ledger_svc
from app.services.flats import sync_flat_roster
from app.services.schema import add_missing_columns

Base.metadata.create_all(bind=engine)
add_missing_columns(engine)


def seed() -> None:
    db = SessionLocal()
    try:
        sync_flat_roster(db)
        if db.scalar(select(LedgerPeriod).limit(1)):
            db.commit()
            print("Flat roster synced; ledger already seeded.")
            return

        flat_map = {
            f.number: f for f in db.scalars(select(Flat).order_by(Flat.number)).all()
        }

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

        sept_expenses = [
            ("Flat 101 advance", "Common", 350, "101", date(2026, 9, 5)),
            ("Common expense 1", "Common", 2143, "301", date(2026, 9, 8)),
            ("Common expense 2", "Common", 2312, "301", date(2026, 9, 12)),
            ("Common expense 3", "Common", 240, "301", date(2026, 9, 18)),
        ]
        for desc, cat, amt, flat_num, ed in sept_expenses:
            exp = Expense(
                description=desc,
                category=cat,
                amount=amt,
                expense_date=ed,
                paid_by_flat_id=flat_map[flat_num].id,
                period_id=sept.id,
            )
            db.add(exp)
            db.flush()
            st = Settlement(
                expense_id=exp.id,
                fund_type=FundType.MAINTENANCE,
                amount=amt,
                settled_on=ed,
                note="Reimbursed from maintenance fund",
            )
            exp.is_settled = True
            db.add(st)

        sept.closing_fund_balance = 4955
        sept.is_closed = True

        oct_p = LedgerPeriod(
            year=2026,
            month=10,
            opening_fund_balance=4955,
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

        salary = Expense(
            description="Watchman salary",
            category="Watchman Salary",
            amount=4000,
            expense_date=date(2026, 10, 1),
            paid_by_flat_id=None,
            period_id=oct_p.id,
        )
        db.add(salary)
        db.flush()
        db.add(
            Settlement(
                expense_id=salary.id,
                fund_type=FundType.MAINTENANCE,
                amount=4000,
                settled_on=date(2026, 10, 1),
                note="Paid from maintenance fund",
            )
        )
        salary.is_settled = True

        db.add(
            Complaint(
                title="Lift doors",
                description="Lift door issue logged in October notebook.",
                reported_on=date(2026, 10, 3),
                status="open",
            )
        )

        from app.services import schedule as schedule_svc

        # v9.1: the register starts with its standard assets but no sample tasks, so a new
        # install has no asset alerts; the admin adds real assets and tasks later.
        schedule_svc.ensure_assets(db)

        from app.services import payment_details as payment_details_svc

        payment_details_svc.ensure_initial(db)
        payment_details_svc.backfill_payees(db)

        db.commit()
        balance = ledger_svc.compute_fund_balance(db)
        print(f"Seeded Nest5 ledger. Fund balance: ₹{balance} (expected ₹10955)")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
