from sqlalchemy import select

from app.database import SessionLocal
from app.domain.flats import FLAT_ROSTER
from app.models import Flat
from app.services.flats import sync_flat_roster


def test_roster_has_102_not_201():
    db = SessionLocal()
    sync_flat_roster(db)
    db.commit()
    numbers = [f.number for f in db.scalars(select(Flat)).all()]
    assert "102" in numbers
    assert "201" not in numbers
    sastry = db.scalar(select(Flat).where(Flat.number == "102"))
    assert sastry.contact_name == "Sastry"
    for profile in FLAT_ROSTER:
        flat = db.scalar(select(Flat).where(Flat.number == profile.number))
        assert flat is not None
        assert flat.contact_name == profile.owner_name
    db.close()
