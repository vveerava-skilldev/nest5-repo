import re

from sqlalchemy import select

from app.models import AuditLog, Expense, Flat, LedgerPeriod, MaintenancePayment, Settlement
from app.services import ledger as ledger_svc
from tests.conftest import OWNER_PASSWORD, make_client


def _new_advance(db, description, amount):
    flat = db.scalar(select(Flat).where(Flat.number == "101"))
    period = db.scalars(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10)).one()
    exp = Expense(
        description=description, category="Common", amount=amount, expense_date=period_date(period),
        paid_by_flat_id=flat.id, period_id=period.id, is_settled=False,
    )
    db.add(exp)
    db.commit()
    return exp


def period_date(period):
    from datetime import date

    return date(period.year, period.month, 5)


def _settle(client, expense_id):
    return client.post(
        f"/settlements/{expense_id}/settle", data={"settled_on": "2026-10-05", "note": ""}, follow_redirects=False
    )


def test_resettle_after_revert_does_not_500(admin_client, seeded_db):
    exp = _new_advance(seeded_db, "Resettle advance", 350)
    available = ledger_svc.available_for_settlement(seeded_db)

    r = _settle(admin_client, exp.id)
    assert r.status_code == 303 and r.headers["location"] == "/settlements"
    seeded_db.expire_all()
    st = seeded_db.scalar(select(Settlement).where(Settlement.expense_id == exp.id))
    assert ledger_svc.available_for_settlement(seeded_db) == available - 350

    r = admin_client.post(f"/settlements/{st.id}/revert", follow_redirects=False)
    assert r.status_code == 303
    seeded_db.expire_all()
    assert ledger_svc.available_for_settlement(seeded_db) == available

    # Settling the same expense again used to hit UNIQUE(settlements.expense_id) → 500.
    r = _settle(admin_client, exp.id)
    assert r.status_code == 303 and r.headers["location"] == "/settlements"
    seeded_db.expire_all()
    rows = seeded_db.scalars(select(Settlement).where(Settlement.expense_id == exp.id)).all()
    assert len(rows) == 1 and rows[0].reverted_at is None
    assert seeded_db.get(Expense, exp.id).is_settled is True
    assert ledger_svc.available_for_settlement(seeded_db) == available - 350
    assert seeded_db.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "settlement", AuditLog.entity_id == rows[0].id, AuditLog.action == "update"
        )
    )

    # Double settle / double revert are friendly redirects, not errors.
    assert "error=" in _settle(admin_client, exp.id).headers["location"]
    admin_client.post(f"/settlements/{rows[0].id}/revert")
    again = admin_client.post(f"/settlements/{rows[0].id}/revert", follow_redirects=False)
    assert again.status_code == 303 and "error=" in again.headers["location"]
    assert admin_client.get("/settlements").status_code == 200

    seeded_db.expire_all()
    seeded_db.delete(seeded_db.get(Settlement, rows[0].id))
    seeded_db.delete(seeded_db.get(Expense, exp.id))
    seeded_db.commit()
    assert ledger_svc.available_for_settlement(seeded_db) == available


def test_get_on_settle_and_revert_redirects(admin_client, seeded_db):
    for url in ("/settlements/1/settle", "/settlements/1/revert", "/settlements/999/settle"):
        r = admin_client.get(url, follow_redirects=False)
        assert r.status_code == 303, url
        assert r.headers["location"].startswith("/settlements?error=")
    page = admin_client.get("/settlements/1/settle")
    assert page.status_code == 200 and "Use the buttons on this page" in page.text


def test_settle_blocked_when_balance_insufficient(admin_client, seeded_db):
    available = ledger_svc.available_for_settlement(seeded_db)
    exp = _new_advance(seeded_db, "Too big advance", available + 1)
    page = admin_client.get("/settlements").text
    assert f'action="/settlements/{exp.id}/settle"' not in page
    r = _settle(admin_client, exp.id)
    assert r.status_code == 303 and "Insufficient" in admin_client.get(r.headers["location"]).text
    seeded_db.expire_all()
    assert seeded_db.get(Expense, exp.id).is_settled is False
    assert seeded_db.scalar(select(Settlement).where(Settlement.expense_id == exp.id)) is None
    assert ledger_svc.available_for_settlement(seeded_db) == available
    seeded_db.delete(seeded_db.get(Expense, exp.id))
    seeded_db.commit()


def test_other_pay_revert_cycles_repeatable(admin_client, seeded_db):
    period = seeded_db.scalars(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10)).one()
    mp = seeded_db.scalar(select(MaintenancePayment).where(MaintenancePayment.period_id == period.id))
    for _ in range(2):
        assert admin_client.post(f"/maintenance/{mp.id}/revert", follow_redirects=False).status_code == 303
        r = admin_client.post(f"/maintenance/{mp.id}/pay", data={"paid_on": "2026-10-10"}, follow_redirects=False)
        assert r.status_code == 303
    seeded_db.expire_all()
    assert seeded_db.get(MaintenancePayment, mp.id).is_paid is True


def test_owner_maintenance_submit_disabled_when_paid(admin_client, seeded_db, set_password):
    set_password(seeded_db, "manikyamba", OWNER_PASSWORD)
    owner = make_client("manikyamba", OWNER_PASSWORD)
    flats = {f.number: f.id for f in seeded_db.scalars(select(Flat)).all()}
    period = seeded_db.scalars(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10)).one()
    mp_401 = seeded_db.scalar(
        select(MaintenancePayment).where(
            MaintenancePayment.period_id == period.id, MaintenancePayment.flat_id == flats["401"]
        )
    )
    admin_client.post(f"/maintenance/{mp_401.id}/revert")

    html = owner.get(f"/payments/maintenance?period_id={period.id}").text
    # First flat in the selector is 101 (paid on 10-10-2026 in the seed) → disabled with the date.
    assert re.search(r'<button type="submit" class="owner-submit" disabled>', html)
    assert "Already paid on 10-10-2026" in html
    m = re.search(r"const paid = (\{.*?\});", html)
    assert m
    assert f'"{flats["101"]}:{period.id}": "10-10-2026"' in m.group(1)
    assert f'"{flats["401"]}:{period.id}"' not in m.group(1)  # unpaid → Submit enabled for 401
    assert f'"{flats["201"]}:' not in m.group(1)  # other owners' flats are not exposed

    # Server still refuses an already-paid month even if the button is bypassed.
    r = owner.post(
        "/owner/maintenance",
        data={"flat_id": flats["101"], "period_id": period.id, "paid_on": "2026-10-12"},
        follow_redirects=False,
    )
    assert "owner_error" in r.headers["location"]

    r = owner.post(
        "/owner/maintenance",
        data={"flat_id": flats["401"], "period_id": period.id, "paid_on": "2026-10-10", "payment_mode": "UPI", "payment_note": "UTR123456789"},
        follow_redirects=False,
    )
    assert "owner_msg" in r.headers["location"]
    html = owner.get(f"/payments/maintenance?period_id={period.id}").text
    assert f'"{flats["401"]}:{period.id}": "10-10-2026"' in re.search(r"const paid = (\{.*?\});", html).group(1)
