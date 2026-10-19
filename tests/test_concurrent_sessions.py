from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AuditLog, Expense, Flat, LedgerPeriod, MaintenancePayment, Settlement
from tests.conftest import ADMIN_PASSWORD, OWNER_PASSWORD, make_client


def _oct(db):
    return db.scalars(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10)).one()


def _who(client):
    html = client.get("/").text
    assert "Logged in as" in html
    return html.split("Logged in as <strong>", 1)[1].split("</strong>", 1)[0]


def test_admin_and_owner_logged_in_at_the_same_time(seeded_db, set_password):
    set_password(seeded_db, "admin", ADMIN_PASSWORD)
    set_password(seeded_db, "asrk.sastry", OWNER_PASSWORD)
    admin = make_client("admin", ADMIN_PASSWORD)
    owner = make_client("asrk.sastry", OWNER_PASSWORD)  # logs in after admin

    assert _who(admin) == "admin"
    assert _who(owner) == "asrk.sastry"
    assert "(admin)" in admin.get("/").text and "(owner)" in owner.get("/").text
    assert admin.get("/settlements").status_code == 200
    assert owner.get("/settlements", follow_redirects=False).status_code == 403

    # Interleaved writes: owner records a payment, admin settles an advance, owner again.
    flats = {f.number: f.id for f in seeded_db.scalars(select(Flat)).all()}
    period = _oct(seeded_db)
    mp = seeded_db.scalar(
        select(MaintenancePayment).where(
            MaintenancePayment.period_id == period.id, MaintenancePayment.flat_id == flats["201"]
        )
    )
    admin.post(f"/maintenance/{mp.id}/revert")
    exp = Expense(
        description="Two-session advance", category="Common", amount=100,
        expense_date=period_date(period), paid_by_flat_id=flats["301"], period_id=period.id,
    )
    seeded_db.add(exp)
    seeded_db.commit()

    r = owner.post(
        "/owner/maintenance",
        data={"flat_id": flats["201"], "period_id": period.id, "paid_on": "2026-10-10", "payment_note": "UTR123456789", "_as_user": "asrk.sastry"},
        follow_redirects=False,
    )
    assert "owner_msg" in r.headers["location"]
    r = admin.post(
        f"/settlements/{exp.id}/settle", data={"settled_on": "2026-10-05", "_as_user": "admin"}, follow_redirects=False
    )
    assert r.headers["location"] == "/settlements"
    assert _who(owner) == "asrk.sastry" and _who(admin) == "admin"

    seeded_db.expire_all()
    pay_log = seeded_db.scalars(
        select(AuditLog).where(AuditLog.entity_type == "maintenance_payment", AuditLog.entity_id == mp.id)
        .order_by(AuditLog.id.desc())
    ).first()
    assert pay_log.action == "owner_submit" and pay_log.user_label == "asrk.sastry"
    settle_log = seeded_db.scalars(
        select(AuditLog).where(AuditLog.entity_type == "expense", AuditLog.entity_id == exp.id, AuditLog.action == "settle")
    ).first()
    assert settle_log.user_label == "admin"

    st = seeded_db.scalar(select(Settlement).where(Settlement.expense_id == exp.id))
    seeded_db.delete(st)
    seeded_db.delete(exp)
    seeded_db.commit()


def period_date(period):
    from datetime import date

    return date(period.year, period.month, 5)


def test_form_opened_as_other_user_is_rejected(seeded_db, set_password):
    """Same browser (one cookie jar): page opened as admin, then another tab logs in as an owner."""
    set_password(seeded_db, "admin", ADMIN_PASSWORD)
    set_password(seeded_db, "asrk.sastry", OWNER_PASSWORD)
    from app.main import app

    browser = TestClient(app)
    browser.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    page = browser.get("/complaints").text
    assert 'i.name = "_as_user"; i.value = "admin";' in page
    browser.post("/login", data={"username": "asrk.sastry", "password": OWNER_PASSWORD})

    before = seeded_db.scalar(select(AuditLog.id).order_by(AuditLog.id.desc()).limit(1))
    r = browser.post(
        "/complaints",
        data={"title": "Should not save", "description": "x", "reported_on": "2026-10-09", "_as_user": "admin"},
        headers={"referer": "http://testserver/complaints"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/complaints?session_switched=admin"
    page = browser.get(r.headers["location"]).text
    assert "This page was opened as <strong>admin</strong>" in page and "Nothing was saved" in page
    seeded_db.expire_all()
    assert seeded_db.scalar(select(AuditLog.id).order_by(AuditLog.id.desc()).limit(1)) == before

    # Matching user (or no marker, e.g. scripts) still works; logout always works.
    r = browser.post(
        "/complaints", data={"title": "Lift light", "description": "x", "reported_on": "2026-10-09", "_as_user": "asrk.sastry"}, follow_redirects=False
    )
    assert r.status_code == 303 and "session_switched" not in r.headers["location"]
    r = browser.post("/logout", data={"_as_user": "admin"}, follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_login_page_while_logged_in_shows_form_and_warning(admin_client):
    page = admin_client.get("/login")
    assert page.status_code == 200
    assert 'action="/login"' in page.text
    assert "already logged in as <strong>admin</strong>" in page.text
    assert "localhost:8000" in page.text
    assert admin_client.get("/forgot-password").text.count('action="/forgot-password"') >= 1
