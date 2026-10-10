from datetime import date

import pytest
from sqlalchemy import delete, select

from app.models import LedgerPeriod, MaintenanceFeeRevision, MaintenancePayment
from app.services import ledger as ledger_svc
from app.services import maintenance_fee as fee_svc

# Far-future months so these tests never collide with the seeded 2026 ledger.
TEST_YEAR_FROM = 2031


@pytest.fixture
def db(seeded_db):
    yield seeded_db
    seeded_db.rollback()
    period_ids = select(LedgerPeriod.id).where(LedgerPeriod.year >= TEST_YEAR_FROM)
    seeded_db.execute(delete(MaintenancePayment).where(MaintenancePayment.period_id.in_(period_ids)))
    seeded_db.execute(delete(LedgerPeriod).where(LedgerPeriod.year >= TEST_YEAR_FROM))
    seeded_db.execute(
        delete(MaintenanceFeeRevision).where(
            MaintenanceFeeRevision.effective_date >= date(TEST_YEAR_FROM, 1, 1)
        )
    )
    seeded_db.commit()


def _period(db, year, month):
    period = ledger_svc.get_or_create_period(db, year, month)
    db.commit()
    return period


def _amounts(db, period):
    db.expire_all()
    return {
        mp.flat_id: (mp.amount, mp.is_paid)
        for mp in db.scalars(
            select(MaintenancePayment).where(MaintenancePayment.period_id == period.id)
        ).all()
    }


def _revise(client, month, amount, note="test"):
    resp = client.post(
        "/admin/maintenance-fee",
        data={"effective_date": month, "amount": str(amount), "note": note},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def test_earlier_months_unchanged_and_paid_rows_untouched(db, admin_client):
    jan, feb, mar = (_period(db, 2031, m) for m in (1, 2, 3))
    base = fee_svc.fee_for_period(db, jan)
    paid = db.scalars(select(MaintenancePayment).where(MaintenancePayment.period_id == feb.id)).first()
    paid.is_paid = True
    paid.paid_on = date(2031, 2, 3)
    db.commit()

    _revise(admin_client, "2031-02", base + 700)

    assert all(amt == base for amt, _ in _amounts(db, jan).values())
    for flat_id, (amt, is_paid) in _amounts(db, feb).items():
        assert amt == (base if flat_id == paid.flat_id else base + 700)
        assert is_paid == (flat_id == paid.flat_id)
    assert all(amt == base + 700 for amt, _ in _amounts(db, mar).values())


def test_future_revision_changes_nothing_now_but_applies_when_month_created(db, admin_client):
    may = _period(db, 2032, 5)
    base = fee_svc.fee_for_period(db, may)
    all_rows_before = {
        mp.id: mp.amount for mp in db.scalars(select(MaintenancePayment)).all()
    }

    preview = admin_client.get("/admin/maintenance-fee/preview?month=06-2032").json()
    assert preview == {"effective_month": "06-2032", "affected": []}

    _revise(admin_client, "06-2032", base + 400)

    db.expire_all()
    assert {mp.id: mp.amount for mp in db.scalars(select(MaintenancePayment)).all()} == all_rows_before
    jun = _period(db, 2032, 6)
    assert all(amt == base + 400 for amt, _ in _amounts(db, jun).values())
    assert all(amt == base for amt, _ in _amounts(db, may).values())


def test_overlapping_revisions_use_latest_effective_on_or_before_month(db, admin_client):
    jul, aug, sep, oct_ = (_period(db, 2033, m) for m in (7, 8, 9, 10))

    _revise(admin_client, "09-2033", 3000)
    preview = admin_client.get("/admin/maintenance-fee/preview?month=2033-07").json()
    assert [a["month"] for a in preview["affected"]] == ["07-2033", "08-2033"]
    assert all(a["unpaid"] == 5 for a in preview["affected"])

    _revise(admin_client, "07-2033", 2600)

    assert all(amt == 2600 for amt, _ in _amounts(db, jul).values())
    assert all(amt == 2600 for amt, _ in _amounts(db, aug).values())
    assert all(amt == 3000 for amt, _ in _amounts(db, sep).values())
    assert all(amt == 3000 for amt, _ in _amounts(db, oct_).values())

    _revise(admin_client, "09-2033", 3100, note="same month, recorded later")
    assert all(amt == 3100 for amt, _ in _amounts(db, sep).values())
    assert all(amt == 2600 for amt, _ in _amounts(db, aug).values())
    nov = _period(db, 2033, 11)
    assert all(amt == 3100 for amt, _ in _amounts(db, nov).values())


def test_admin_page_month_picker_and_history(db, admin_client):
    _revise(admin_client, "12-2031", 2750, note="AGM 2031")
    page = admin_client.get("/admin/maintenance-fee").text
    assert 'type="month"' in page
    assert 'id="fee-preview"' in page
    assert "12-2031" in page and "AGM 2031" in page and "₹2750" in page
    assert admin_client.get("/admin/maintenance-fee/preview?month=bad").status_code == 400


def test_parse_effective_month_formats():
    assert fee_svc.parse_effective_month("2026-11") == date(2026, 11, 1)
    assert fee_svc.parse_effective_month("11-2026") == date(2026, 11, 1)
    assert fee_svc.parse_effective_month("2026-11-15") == date(2026, 11, 1)
