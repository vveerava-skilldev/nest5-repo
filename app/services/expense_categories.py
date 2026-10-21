"""Expense categories managed by the admin: add, rename, disable, and which ones owners may use.

Expenses store the category name as text, so disabling or renaming a category never changes an
existing expense unless the admin asks for the rename to be applied to them too.
"""
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.domain.expense_categories import EXPENSE_CATEGORY_PRESETS
from app.models import Expense, ExpenseCategory


class CategoryError(ValueError):
    pass


def ensure_defaults(db: Session) -> int:
    """Presets become rows once (a disabled or renamed preset is never re-added)."""
    existing = {n.lower() for n in db.scalars(select(ExpenseCategory.name)).all()}
    added = 0
    for i, name in enumerate(EXPENSE_CATEGORY_PRESETS):
        if name.lower() not in existing:
            db.add(ExpenseCategory(name=name, is_active=True, owner_allowed=True, sort_order=i))
            added += 1
    if added:
        db.flush()
    return added


def _ordered(db: Session):
    ensure_defaults(db)
    return db.scalars(
        select(ExpenseCategory).order_by(ExpenseCategory.sort_order.is_(None), ExpenseCategory.sort_order,
                                         ExpenseCategory.name)
    ).all()


def all_rows(db: Session) -> list[dict]:
    usage = dict(db.execute(
        select(Expense.category, func.count(Expense.id)).where(Expense.counted()).group_by(Expense.category)
    ).all())
    return [{"category": c, "active": c.is_active is not False, "owner_allowed": c.owner_allowed is not False,
             "used": usage.get(c.name, 0)} for c in _ordered(db)]


def active_names(db: Session, *, owner: bool = False) -> list[str]:
    return [c.name for c in _ordered(db)
            if c.is_active is not False and (not owner or c.owner_allowed is not False)]


def by_name(db: Session, name: str) -> ExpenseCategory | None:
    return db.scalar(select(ExpenseCategory).where(func.lower(ExpenseCategory.name) == name.strip().lower()))


def add(db: Session, name: str, *, owner_allowed: bool = True) -> ExpenseCategory:
    name = name.strip()[:100]
    if not name:
        raise CategoryError("Enter a category name.")
    found = by_name(db, name)
    if found is not None:
        if found.is_active is False:
            found.is_active = True
            return found
        raise CategoryError(f"{found.name} already exists.")
    top = db.scalar(select(func.max(ExpenseCategory.sort_order))) or 0
    cat = ExpenseCategory(name=name, is_active=True, owner_allowed=owner_allowed, sort_order=top + 1)
    db.add(cat)
    db.flush()
    return cat


def rename(db: Session, cat: ExpenseCategory, new_name: str, *, update_existing: bool = False) -> int:
    new_name = new_name.strip()[:100]
    if not new_name:
        raise CategoryError("Enter a category name.")
    clash = by_name(db, new_name)
    if clash is not None and clash.id != cat.id:
        raise CategoryError(f"{clash.name} already exists.")
    old = cat.name
    cat.name = new_name
    changed = 0
    if update_existing and old != new_name:
        changed = db.execute(update(Expense).where(Expense.category == old).values(category=new_name)).rowcount or 0
    db.flush()
    return changed
