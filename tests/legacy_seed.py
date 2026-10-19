"""Test-only fixture: the sample expenses and settlements that scripts/seed.py used to create.

scripts/seed.py no longer ships any expenses or settlements. Older tests were written against that data
(Sept closing 4,955, Watchman salary, fund 10,955), so they build it here, on top of `seed(sample=True)`.
"""
from datetime import date

from sqlalchemy import select

from app.models import Expense, Flat, FundType, LedgerPeriod, Settlement

SEPT_EXPENSES = [
    ("Flat 101 advance", "Common", 350, "101", date(2026, 9, 5)),
    ("Common expense 1", "Common", 2143, "301", date(2026, 9, 8)),
    ("Common expense 2", "Common", 2312, "301", date(2026, 9, 12)),
    ("Common expense 3", "Common", 240, "301", date(2026, 9, 18)),
]


def add_legacy_expenses(db) -> None:
    flats = {f.number: f for f in db.scalars(select(Flat)).all()}
    sept = db.scalars(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 9)).one()
    oct_p = db.scalars(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10)).one()
    for desc, cat, amt, flat_num, ed in SEPT_EXPENSES:
        exp = Expense(description=desc, category=cat, amount=amt, expense_date=ed,
                      paid_by_flat_id=flats[flat_num].id, period_id=sept.id, is_settled=True)
        db.add(exp)
        db.flush()
        db.add(Settlement(expense_id=exp.id, fund_type=FundType.MAINTENANCE, amount=amt, settled_on=ed,
                          note="Reimbursed from maintenance fund"))
    sept.closing_fund_balance = 4955
    sept.is_closed = True
    oct_p.opening_fund_balance = 4955
    salary = Expense(description="Watchman salary", category="Watchman Salary", amount=4000,
                     expense_date=date(2026, 10, 1), paid_by_flat_id=None, period_id=oct_p.id, is_settled=True)
    db.add(salary)
    db.flush()
    db.add(Settlement(expense_id=salary.id, fund_type=FundType.MAINTENANCE, amount=4000,
                      settled_on=date(2026, 10, 1), note="Paid from maintenance fund"))
    db.commit()


def seed_legacy() -> None:
    """Sample seed + the legacy expenses, for the shared test database and for scratch subprocesses."""
    from app.database import SessionLocal
    from scripts.seed import seed

    seed(sample=True)
    db = SessionLocal()
    try:
        if not db.scalar(select(Expense).limit(1)):
            add_legacy_expenses(db)
    finally:
        db.close()



def add_live_like(db) -> None:
    """On top of the legacy seed: October expenses so the numbers match the live society
    (Account Balance 8,256; expenses 11,944 = 11,744 settled + 200 owed to flat 301)."""
    flats = {f.number: f for f in db.scalars(select(Flat)).all()}
    oct_p = db.scalars(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10)).one()
    for desc, amt, num, day, settled in (("Lift repair", 1799, "101", 6, True), ("Garden tools", 900, "201", 9, True),
                                         ("Pump fuse", 200, "301", 12, False)):
        exp = Expense(description=desc, category="Common", amount=amt, expense_date=date(2026, 10, day),
                      paid_by_flat_id=flats[num].id, period_id=oct_p.id, is_settled=settled)
        db.add(exp)
        db.flush()
        if settled:
            db.add(Settlement(expense_id=exp.id, fund_type=FundType.MAINTENANCE, amount=amt,
                              settled_on=date(2026, 10, day), note="Paid back to owner"))
    db.commit()
