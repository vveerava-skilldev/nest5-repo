from fastapi.testclient import TestClient

from app.database import SessionLocal, engine, Base
from tests.legacy_seed import seed_legacy

Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)
seed_legacy()

client = TestClient(__import__("app.main", fromlist=["app"]).app)


def login():
    return client.post("/login", data={"password": "nest5admin"}, follow_redirects=False)


def test_login_and_dashboard():
    r = login()
    assert r.status_code == 303
    dash = client.get("/", cookies=r.cookies)
    assert dash.status_code == 200
    assert b"10955" in dash.content


def test_maintenance_page():
    r = login()
    page = client.get("/maintenance", cookies=r.cookies)
    assert page.status_code == 200
    assert b"101" in page.content


def test_dashboard_period_nav():
    r = login()
    dash = client.get("/", cookies=r.cookies)
    assert dash.status_code == 200
    assert b"period-nav" in dash.content
    assert b"period_id=" in dash.content


def test_expenses_columns_and_date_format():
    r = login()
    page = client.get("/expenses", cookies=r.cookies)
    assert page.status_code == 200
    assert b"Payment date" in page.content
    assert b"Account Balance" in page.content
    assert b"01-10-2026" in page.content or b"Watchman" in page.content


def test_add_category_persists_and_edit():
    from app.models import Expense, LedgerPeriod

    db = SessionLocal()
    oct_id = db.query(LedgerPeriod).filter_by(year=2026, month=10).one().id
    db.close()
    r = login()
    data = {
        "period_id": str(oct_id),
        "description": "Pump repair",
        "category": "__add__",
        "category_other": "Plumbing",
        "amount": "750",
        "expense_date": "2026-10-05",
        "payment_source": "society",
        "payment_note": "Paid in cash from the office account",
    }
    resp = client.post("/expenses", data=data, cookies=r.cookies, follow_redirects=False)
    assert resp.status_code == 303
    page = client.get(f"/expenses?period_id={oct_id}", cookies=r.cookies)
    assert b'<option value="Plumbing"' in page.content
    assert b"05-10-2026" in page.content

    db = SessionLocal()
    exp = db.query(Expense).filter_by(description="Pump repair").one()
    db.close()
    resp = client.post(
        f"/expenses/{exp.id}/edit",
        data={**data, "category": "Plumbing", "amount": "800"},
        cookies=r.cookies,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db = SessionLocal()
    assert db.get(Expense, exp.id).amount == 800
    db.close()
    resp = client.post(f"/expenses/{exp.id}/delete", cookies=r.cookies, follow_redirects=False)
    assert resp.status_code == 303


def test_maintenance_fee_revision_history():
    r = login()
    resp = client.post(
        "/admin/maintenance-fee",
        data={"effective_date": "2026-12-15", "amount": "2500", "note": "AGM decision"},
        cookies=r.cookies,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = client.get("/admin/maintenance-fee", cookies=r.cookies)
    assert b"12-2026" in page.content
    assert b"\xe2\x82\xb92000" in page.content and b"\xe2\x82\xb92500" in page.content
    assert b"AGM decision" in page.content


def test_admin_maintenance_fee_page():
    r = login()
    page = client.get("/admin/maintenance-fee", cookies=r.cookies)
    assert page.status_code == 200
    assert b"Maintenance fee" in page.content


def test_settlement_blocked_when_insufficient(monkeypatch):
    from app.services import ledger as ledger_svc

    monkeypatch.setattr(ledger_svc, "available_for_settlement", lambda db: 0)
    r = login()
    page = client.get("/settlements", cookies=r.cookies)
    assert b"blocked" in page.content.lower() or b"Blocked" in page.content
