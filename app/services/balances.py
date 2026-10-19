"""Account Balance vs what is owed back to owners (unsettled owner expenses)."""
from sqlalchemy import select
from sqlalchemy.orm import Session, defer, selectinload

from app.models import Expense, Flat
from app.services import ledger as ledger_svc


def _unsettled(db: Session, flat_ids: list[int] | None = None) -> list[Expense]:
    q = (
        select(Expense)
        .where(Expense.is_settled.is_(False), Expense.counted(), Expense.paid_by_flat_id.isnot(None))
        # Deferred: the v7.3 migration reaches this query before expenses.payment_source exists.
        .options(defer(Expense.payment_source), selectinload(Expense.paid_by_flat))
        .order_by(Expense.expense_date, Expense.id)
    )
    if flat_ids is not None:
        q = q.where(Expense.paid_by_flat_id.in_(flat_ids))
    return list(db.scalars(q).all())


def overview(db: Session) -> dict:
    """held: money actually in the Account Balance now; owed: unsettled owner advances.

    Settling pays the oldest advances first; `settle_now` is what fits in the balance today.
    """
    held = ledger_svc.compute_fund_balance(db)
    items = _unsettled(db)
    owed = sum(e.amount for e in items)
    room, settle_now, settle_count = max(held, 0), 0, 0
    for e in items:
        if e.amount <= room:
            room -= e.amount
            settle_now += e.amount
            settle_count += 1
    if not items:
        state = "nothing"
    elif settle_count == len(items):
        state = "all"
    elif settle_count:
        state = "some"
    else:
        state = "none"
    by_flat: dict[int, dict] = {}
    for e in items:
        row = by_flat.setdefault(e.paid_by_flat_id, {"flat": e.paid_by_flat, "total": 0, "items": []})
        row["total"] += e.amount
        row["items"].append({"expense": e, "affordable": e.amount <= max(held, 0)})
    return {
        "held": held,
        "owed": owed,
        "effective": held - owed,
        "available": max(held, 0),
        "settle_now": settle_now,
        "settle_count": settle_count,
        "count": len(items),
        "state": state,
        "negative_if_all": held - owed < 0 and owed > 0,
        "by_flat": sorted(by_flat.values(), key=lambda r: r["flat"].number),
    }


def owner_advances(db: Session, flat_ids: list[int]) -> dict:
    """An owner's own expense advances: what they are owed back (cumulative) and each item's status."""
    rows = db.scalars(
        select(Expense)
        .where(Expense.paid_by_flat_id.in_(flat_ids), Expense.counted())
        .options(selectinload(Expense.paid_by_flat), selectinload(Expense.settlement))
        .order_by(Expense.is_settled, Expense.expense_date.desc(), Expense.id.desc())
    ).all()
    flats = db.scalars(select(Flat).where(Flat.id.in_(flat_ids)).order_by(Flat.number)).all()
    unsettled = [e for e in rows if not e.is_settled]
    settled = [e for e in rows if e.is_settled]
    owed = sum(e.amount for e in unsettled)
    settled_total = sum(e.amount for e in settled)

    def _flat_row(f: Flat) -> dict:
        mine = [e for e in rows if e.paid_by_flat_id == f.id]
        return {
            "flat": f,
            "paid": sum(e.amount for e in mine),
            "settled": sum(e.amount for e in mine if e.is_settled),
            "owed": sum(e.amount for e in mine if not e.is_settled),
        }

    return {
        "owed": owed,
        "unsettled": unsettled,
        "settled": settled[:10],
        "settled_total": settled_total,
        # Consolidated metric across the owner's flats (counted expenses only).
        "paid_total": owed + settled_total,
        "count": len(rows),
        "unsettled_count": len(unsettled),
        "settled_count": len(settled),
        "has_any": bool(rows),
        "per_flat": [_flat_row(f) for f in flats],
    }
