import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import SessionLocal
from app.domain.flats import FLAT_ROSTER
from app.database import Base
from app.models import Flat, MaintenancePayment
from app.services.flats import sync_flat_roster

OWNER = "A. Siva Rama Krishna Sastry"


@pytest.fixture
def scratch(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'f.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _flats(db):
    return {f.number: f for f in db.scalars(select(Flat)).all()}


def _snapshot(db):
    return sorted((f.id, f.number, f.contact_name, f.contact_phone, f.owner_notes) for f in db.scalars(select(Flat)).all())


def test_roster_is_201_with_the_full_owner_name_on_the_shared_db(seeded_db):
    db = SessionLocal()
    sync_flat_roster(db)
    db.commit()
    flats = _flats(db)
    assert "201" in flats and "102" not in flats
    assert flats["201"].contact_name == OWNER
    assert [p.number for p in FLAT_ROSTER] == ["101", "201", "301", "401", "501"]
    for profile in FLAT_ROSTER:
        assert flats[profile.number].contact_name == profile.owner_name
    db.close()


def test_fresh_install_seeds_flat_201_with_the_owner_and_no_102(scratch):
    sync_flat_roster(scratch)
    scratch.commit()
    flats = _flats(scratch)
    assert sorted(flats) == ["101", "201", "301", "401", "501"]
    assert flats["201"].contact_name == OWNER
    assert flats["101"].owner_notes == "Tenant"


def test_existing_db_already_on_201_is_left_untouched(scratch):
    scratch.add_all([Flat(id=1, number="101", contact_name="Manikyamba", owner_notes="Tenant"),
                     Flat(id=2, number="201", contact_name="Name edited by the admin", contact_phone="9000000001"),
                     Flat(id=3, number="301", contact_name="Venkata Giridhar"), Flat(id=4, number="401", contact_name="Manikyamba"),
                     Flat(id=5, number="501", contact_name="Pavan Kumar")])
    scratch.commit()
    before = _snapshot(scratch)
    sync_flat_roster(scratch)
    scratch.commit()
    assert _snapshot(scratch) == before
    assert _flats(scratch)["201"].id == 2 and "102" not in _flats(scratch)


def test_old_db_numbered_102_is_renumbered_once_keeping_id_and_history(scratch):
    scratch.add_all([Flat(id=1, number="101", contact_name="Manikyamba", owner_notes="Tenant"),
                     Flat(id=2, number="102", contact_name="Sastry", contact_phone="9000000002"),
                     Flat(id=3, number="301", contact_name="Venkata Giridhar"), Flat(id=4, number="401", contact_name="Manikyamba"),
                     Flat(id=5, number="501", contact_name="Pavan Kumar")])
    scratch.flush()
    from app.models import LedgerPeriod
    period = LedgerPeriod(year=2026, month=10, opening_fund_balance=0, corpus_opening=0, is_closed=False)
    scratch.add(period)
    scratch.flush()
    scratch.add(MaintenancePayment(flat_id=2, period_id=period.id, amount=2000, is_paid=True))
    scratch.commit()

    sync_flat_roster(scratch)
    scratch.commit()
    flats = _flats(scratch)
    assert "102" not in flats and sorted(flats) == ["101", "201", "301", "401", "501"]
    assert flats["201"].id == 2 and flats["201"].contact_phone == "9000000002"
    assert flats["201"].contact_name == OWNER  # the old default name is replaced when migrating
    assert scratch.scalar(select(MaintenancePayment.flat_id)) == 2  # history stays on the same flat id
    assert scratch.query(Flat).count() == 5


def test_migration_keeps_a_name_the_admin_edited(scratch):
    scratch.add(Flat(id=2, number="102", contact_name="Mrs. Lakshmi (edited)"))
    scratch.commit()
    sync_flat_roster(scratch)
    scratch.commit()
    flat = _flats(scratch)["201"]
    assert flat.id == 2 and flat.contact_name == "Mrs. Lakshmi (edited)"


def test_restarts_are_idempotent_and_never_override_admin_edits(scratch):
    scratch.add(Flat(id=2, number="102", contact_name="Sastry"))
    scratch.commit()
    sync_flat_roster(scratch)
    scratch.commit()
    first = _snapshot(scratch)
    for _ in range(3):
        sync_flat_roster(scratch)
        scratch.commit()
    assert _snapshot(scratch) == first

    flats = _flats(scratch)
    flats["201"].contact_name = "Edited later"
    flats["101"].owner_notes = "Owner-occupied now"
    flats["301"].owner_notes = None
    scratch.commit()
    sync_flat_roster(scratch)
    scratch.commit()
    flats = _flats(scratch)
    assert flats["201"].contact_name == "Edited later" and flats["101"].owner_notes == "Owner-occupied now"
    assert "102" not in flats and len(flats) == 5

    flats["201"].contact_name = ""  # an emptied name is filled from the roster, nothing else
    scratch.commit()
    sync_flat_roster(scratch)
    scratch.commit()
    assert _flats(scratch)["201"].contact_name == OWNER


def test_both_numbers_present_is_left_alone(scratch):
    scratch.add_all([Flat(id=2, number="102", contact_name="Old"), Flat(id=7, number="201", contact_name="Current")])
    scratch.commit()
    sync_flat_roster(scratch)
    scratch.commit()
    flats = _flats(scratch)
    assert flats["102"].id == 2 and flats["201"].id == 7 and flats["201"].contact_name == "Current"


def test_fresh_install_creates_the_owner_login_asrk_sastry_and_derives_the_others(tmp_path):
    from tests.test_v71_empty_and_review import run_scratch

    out = run_scratch(tmp_path, "out['users'] = sorted(u_.username for u_ in db.scalars(select(User)).all())\n"
                                "out['flats'] = [(f.number, f.contact_name) for f in db.scalars(select(Flat).order_by(Flat.number))]\n"
                                "out['owner_flats'] = [f.number for f in db.scalar(select(User).where(User.username == 'asrk.sastry')).flats]\n")
    assert "asrk.sastry" in out["users"] and "sastry" not in out["users"]
    assert "a.siva.rama.krishna.sastry" not in out["users"]
    assert out["users"] == ["admin", "asrk.sastry", "manikyamba", "pavan.kumar", "venkata.giridhar"]
    assert ["201", OWNER] in out["flats"] and out["owner_flats"] == ["201"]
