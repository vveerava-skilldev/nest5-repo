from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.flats import FLAT_ROSTER, LEGACY_FLAT_NUMBERS, LEGACY_OWNER_NAMES, FlatProfile
from app.models import Flat


def apply_owner_profile(flat: Flat, profile: FlatProfile) -> None:
    """Fill in what is missing; never overwrite a name or note the admin has edited."""
    if not (flat.contact_name or "").strip():
        flat.contact_name = profile.owner_name
    if flat.owner_notes is None and profile.owner_notes is not None:
        flat.owner_notes = profile.owner_notes


def _renumber_legacy_flats(db: Session) -> None:
    """Old databases numbered a roster flat differently (102, now 201): renumber it once, keeping its id.

    Only when the current number does not exist yet, so a database that is already on the new
    number (or has both) is left alone, and later restarts find nothing to do.
    """
    for old_number, new_number in LEGACY_FLAT_NUMBERS.items():
        old = db.scalar(select(Flat).where(Flat.number == old_number))
        if old is None or db.scalar(select(Flat).where(Flat.number == new_number)) is not None:
            continue
        old.number = new_number
        legacy_name = LEGACY_OWNER_NAMES.get(new_number)
        profile = next((p for p in FLAT_ROSTER if p.number == new_number), None)
        if profile and legacy_name and (old.contact_name or "").strip() == legacy_name:
            old.contact_name = profile.owner_name
        db.flush()


def sync_flat_roster(db: Session) -> None:
    """Make sure every roster flat exists. Idempotent; runs on every start.

    Creates missing flats with the roster owner, renumbers a legacy flat once, and fills in an empty
    owner name or note. It never renames a flat or overwrites a name the admin has edited.
    """
    _renumber_legacy_flats(db)

    by_number = {f.number: f for f in db.scalars(select(Flat)).all()}
    for profile in FLAT_ROSTER:
        flat = by_number.get(profile.number)
        if not flat:
            flat = Flat(number=profile.number)
            db.add(flat)
            db.flush()
            by_number[profile.number] = flat
        apply_owner_profile(flat, profile)

    db.flush()


def display_owner(flat: Flat) -> str:
    return flat.contact_name or "—"


def display_flat_line(flat: Flat) -> str:
    base = f"Flat {flat.number} — {display_owner(flat)}"
    if flat.owner_notes:
        base += f" ({flat.owner_notes})"
    return base
