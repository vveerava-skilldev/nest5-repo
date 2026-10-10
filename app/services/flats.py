from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.flats import FLAT_ROSTER, FlatProfile
from app.models import (
    CorpusPayment,
    Expense,
    Flat,
    MaintenancePayment,
    ProjectContribution,
    ProjectPayment,
)


def apply_owner_profile(flat: Flat, profile: FlatProfile) -> None:
    flat.contact_name = profile.owner_name
    flat.owner_notes = profile.owner_notes
    if flat.contact_phone is None:
        flat.contact_phone = None


def _reassign_flat_id(db: Session, from_id: int, to_id: int) -> None:
    for model, attr in (
        (MaintenancePayment, "flat_id"),
        (CorpusPayment, "flat_id"),
        (ProjectPayment, "flat_id"),
        (ProjectContribution, "flat_id"),
        (Expense, "paid_by_flat_id"),
    ):
        rows = db.scalars(select(model).where(getattr(model, attr) == from_id)).all()
        for row in rows:
            setattr(row, attr, to_id)


def migrate_flat_number(db: Session, old_number: str, new_number: str) -> None:
    old = db.scalar(select(Flat).where(Flat.number == old_number))
    if not old:
        return
    existing = db.scalar(select(Flat).where(Flat.number == new_number))
    if existing and existing.id != old.id:
        _reassign_flat_id(db, old.id, existing.id)
        db.delete(old)
        db.flush()
        return
    old.number = new_number
    db.flush()


def sync_flat_roster(db: Session) -> None:
    """Ensure roster numbers, owners, and legacy 201 → 102 migration."""
    migrate_flat_number(db, "201", "102")

    by_number = {f.number: f for f in db.scalars(select(Flat)).all()}
    for profile in FLAT_ROSTER:
        flat = by_number.get(profile.number)
        if not flat:
            flat = Flat(number=profile.number)
            db.add(flat)
            db.flush()
            by_number[profile.number] = flat
        apply_owner_profile(flat, profile)

    allowed = {p.number for p in FLAT_ROSTER}
    for num, flat in list(by_number.items()):
        if num not in allowed and num != "201":
            pass  # keep unexpected flats; do not delete historical data

    db.flush()


def display_owner(flat: Flat) -> str:
    return flat.contact_name or "—"


def display_flat_line(flat: Flat) -> str:
    base = f"Flat {flat.number} — {display_owner(flat)}"
    if flat.owner_notes:
        base += f" ({flat.owner_notes})"
    return base
