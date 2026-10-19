"""v9.3: tenant role, owner expense edit, cancel-queue rejection, owed total, due hints, empty cards, no insights."""
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import APP_VERSION
from app.models import AuditLog, CancelRequest, Expense, Flat, LedgerPeriod, MaintenancePayment, User, UserRole
from app.services import balances as balances_svc
from app.services import expense_review as review_svc
from app.services import owner_submissions as subs_svc
from app.services import reminders as reminders_svc
from app.services import tenant as tenant_svc
from tests.conftest import ADMIN_PASSWORD, OWNER_PASSWORD, make_client
from tests.test_v91_rejected_expenses import _flats, _owner_submit, cleanup  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
TENANT_PASSWORD = "tenant-pass-123"


@pytest.fixture
def tnt_flat101(seeded_db, admin_client):
    flat = seeded_db.scalar(select(Flat).where(Flat.number == "101"))
    r = admin_client.post(
        "/admin/users",
        data={"username": "tnt.flat101", "display_name": "Tenant 101", "role": "tenant", "flat_ids": [flat.id]},
    )
    assert r.status_code == 200, r.text
    from tests.conftest import _set_password

    _set_password(seeded_db, "tnt.flat101", TENANT_PASSWORD)
    yield make_client("tnt.flat101", TENANT_PASSWORD)
    seeded_db.rollback()
    seeded_db.expire_all()
    user = seeded_db.scalar(select(User).where(User.username == "tnt.flat101"))
    if user is not None:
        seeded_db.delete(user)
        seeded_db.commit()


def _tenant_user(db):
    db.expire_all()
    return db.scalar(select(User).where(User.username == "tnt.flat101"))


def _unpay(db, number, period_month=10):
    flat = db.scalar(select(Flat).where(Flat.number == number))
    period = db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == period_month))
    mp = db.scalar(select(MaintenancePayment).where(MaintenancePayment.flat_id == flat.id, MaintenancePayment.period_id == period.id))
    if mp is not None and mp.is_paid:
        mp.is_paid = False
        mp.paid_on = None
        db.commit()
    return flat, period


# ---- tenant ----------------------------------------------------------------------------


def test_tenant_created_with_default_maintenance_only(seeded_db, tnt_flat101):
    user = _tenant_user(seeded_db)
    assert user.role == UserRole.TENANT.value
    assert list(tenant_svc.sections_for(user)) == ["maintenance"]
    assert [f.number for f in user.flats] == ["101"]


def test_tenant_lands_on_maintenance_and_sees_only_own_flat(seeded_db, tnt_flat101):
    _unpay(seeded_db, "101")
    _unpay(seeded_db, "401")
    r = tnt_flat101.get("/", follow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["location"] == "/payments/maintenance"
    page = tnt_flat101.get("/payments/maintenance").text
    assert "Flat 101" in page or "101" in page
    assert "Flat 401" not in page
    assert "Your pending payments" in page
    assert "Opening" not in page and "Closing" not in page
    assert "Corpus" not in re.sub(r"<script.*?</script>", "", page, flags=re.S).split("<main", 1)[-1]


def test_tenant_menu_and_tabs(seeded_db, tnt_flat101):
    page = tnt_flat101.get("/payments/maintenance").text
    nav = page.split("<nav", 1)[1].split("</nav>", 1)[0]
    assert "Payments" in nav and "My submissions" in nav
    for hidden in ("Dashboard", "Complaints", "Calendar", "Schedule", "Flats", "Insights"):
        assert hidden not in nav
    tabs = re.findall(r'href="(/payments/[a-z]+)"', page)
    assert "/payments/corpus" not in tabs and "/payments/projects" not in tabs and "/payments/expenses" not in tabs
    assert "role: tenant" in page or "tenant" in page.lower()


@pytest.mark.parametrize(
    "path",
    ["/payments/corpus", "/payments/projects", "/payments/expenses", "/expenses", "/corpus", "/complaints",
     "/schedule", "/calendar", "/payments/building", "/admin/users", "/insights"],
)
def test_tenant_blocked_elsewhere(seeded_db, tnt_flat101, path):
    assert tnt_flat101.get(path, follow_redirects=False).status_code in (403, 404)


def test_tenant_cannot_post_other_things(seeded_db, tnt_flat101):
    flat = seeded_db.scalar(select(Flat).where(Flat.number == "101"))
    r = tnt_flat101.post("/owner/expenses", data={"flat_id": flat.id, "description": "x", "category": "Common",
                                                "amount": "5", "expense_date": "2026-10-12"}, follow_redirects=False)
    assert r.status_code == 403
    r = tnt_flat101.post("/account/dashboard-view", data={"view": "simple"}, follow_redirects=False)
    assert r.status_code == 403
    assert tnt_flat101.get(f"/flats/{flat.id}", follow_redirects=False).status_code == 403


def test_tenant_maintenance_own_flat_only(seeded_db, tnt_flat101):
    flat, period = _unpay(seeded_db, "101")
    other, _ = _unpay(seeded_db, "401")
    bad = tnt_flat101.post("/owner/maintenance", data={"flat_id": other.id, "period_id": period.id, "paid_on": "2026-10-05",
                                                     "payment_mode": "Cash"}, follow_redirects=False)
    assert bad.status_code == 403
    ok = tnt_flat101.post("/owner/maintenance", data={"flat_id": flat.id, "period_id": period.id, "paid_on": "2026-10-05",
                                                    "payment_mode": "Cash"}, follow_redirects=False)
    assert ok.status_code == 303 and "owner_msg" in ok.headers["location"]
    seeded_db.expire_all()
    mp = seeded_db.scalar(select(MaintenancePayment).where(MaintenancePayment.flat_id == flat.id, MaintenancePayment.period_id == period.id))
    assert mp.is_paid and mp.submitted_by_user_id == _tenant_user(seeded_db).id
    mp.is_paid = False
    mp.paid_on = None
    mp.submitted_by_user_id = None
    seeded_db.commit()


def test_admin_enables_and_disables_a_section(seeded_db, admin_client, tnt_flat101):
    user = _tenant_user(seeded_db)
    flat = user.flats[0]
    assert tnt_flat101.get("/payments/projects").status_code == 403
    r = admin_client.post(
        f"/admin/users/{user.id}/update",
        data={"display_name": "Tenant 101", "role": "tenant", "flat_ids": [flat.id],
              "tenant_payments": ["maintenance", "projects"]},
    )
    assert r.status_code == 200
    assert list(tenant_svc.sections_for(_tenant_user(seeded_db))) == ["maintenance", "projects"]
    page = tnt_flat101.get("/payments/projects")
    assert page.status_code == 200 and "Flat 401" not in page.text
    assert tnt_flat101.get("/payments/corpus").status_code == 403
    assert "/payments/projects" in tnt_flat101.get("/payments/maintenance").text
    r = admin_client.post(
        f"/admin/users/{user.id}/update",
        data={"display_name": "Tenant 101", "role": "tenant", "flat_ids": [flat.id], "tenant_payments": ["maintenance"]},
    )
    assert r.status_code == 200
    assert tnt_flat101.get("/payments/projects").status_code == 403


def test_tenant_data_cleared_for_non_tenants(seeded_db, admin_client, tnt_flat101):
    user = _tenant_user(seeded_db)
    flat = user.flats[0]
    admin_client.post(f"/admin/users/{user.id}/update",
                      data={"display_name": "T", "role": "owner", "flat_ids": [flat.id], "tenant_payments": ["corpus"]})
    assert _tenant_user(seeded_db).tenant_payments is None
    admin_client.post(f"/admin/users/{user.id}/update",
                      data={"display_name": "T", "role": "tenant", "flat_ids": [flat.id], "tenant_payments": ["corpus", "bogus"]})
    assert "corpus" in tenant_svc.sections_for(_tenant_user(seeded_db)) and "bogus" not in tenant_svc.sections_for(_tenant_user(seeded_db))


def test_users_page_lists_tenant_toggles(seeded_db, admin_client, tnt_flat101):
    page = admin_client.get("/admin/users").text
    assert 'name="tenant_payments"' in page and "Tenant" in page


def test_two_flat_owner_still_sees_both(seeded_db, set_password):
    set_password(seeded_db, "manikyamba", OWNER_PASSWORD)
    client = make_client("manikyamba", OWNER_PASSWORD)
    page = client.get("/payments/maintenance").text
    assert "101" in page and "401" in page
    assert "/payments/corpus" in page and "/payments/expenses" in page


def test_create_tenant_script(tmp_path):
    db_path = tmp_path / "t.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}", "INSTANCE_DIR": str(tmp_path / "inst"),
           "BACKUP_DIR": str(tmp_path / "bk")}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True, capture_output=True)
    subprocess.run([sys.executable, "-c", "from scripts.seed import seed; seed()"], cwd=ROOT, env=env, check=True, capture_output=True)
    out = subprocess.run([sys.executable, "scripts/create_tenant.py", "--flat", "101", "--sections", "maintenance,corpus"],
                         cwd=ROOT, env=env, check=True, capture_output=True, text=True).stdout
    assert "tenant101" in out and "maintenance, corpus" in out
    con = sqlite3.connect(db_path)
    role, sections, must = con.execute("select role, tenant_payments, must_change_password from users where username='tenant101'").fetchone()
    assert (role, sections, must) == ("tenant", "maintenance,corpus", 1)
    assert con.execute("select count(*) from user_flats").fetchone()[0] >= 1
    con.close()


def test_migration_adds_tenant_column(tmp_path):
    db_path = tmp_path / "m.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}", "INSTANCE_DIR": str(tmp_path / "inst"),
           "BACKUP_DIR": str(tmp_path / "bk")}
    run = lambda *a: subprocess.run([sys.executable, "-m", "alembic", *a], cwd=ROOT, env=env, check=True, capture_output=True, text=True)
    run("upgrade", "c9a2e5f7b140")
    con = sqlite3.connect(db_path)
    assert "tenant_payments" not in [r[1] for r in con.execute("pragma table_info(users)")]
    con.close()
    run("upgrade", "head")
    run("upgrade", "head")
    con = sqlite3.connect(db_path)
    assert "tenant_payments" in [r[1] for r in con.execute("pragma table_info(users)")]
    con.close()


# ---- owner edits own expense ------------------------------------------------------------


def _edit(client, exp, **over):
    data = {"flat_id": exp.paid_by_flat_id, "description": exp.description, "category": "Common",
            "amount": str(exp.amount), "expense_date": "2026-10-12"}
    data.update(over)
    return client.post(f"/owner/expenses/{exp.id}/edit", data=data, follow_redirects=False)


def test_owner_edits_own_expense(seeded_db, owner_client, cleanup):
    flats = _flats(seeded_db)
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Wrong desc 104", 80, cleanup)
    before = balances_svc.overview(seeded_db)["owed"]
    r = _edit(owner_client, exp, description="Right desc 101", amount="95")
    assert r.status_code == 303 and "owner_msg" in r.headers["location"]
    seeded_db.expire_all()
    exp = seeded_db.get(Expense, exp.id)
    assert exp.description == "Right desc 101" and exp.amount == 95
    assert balances_svc.overview(seeded_db)["owed"] == before + 15
    log = seeded_db.scalars(select(AuditLog).where(AuditLog.entity_type == "expense", AuditLog.action == "owner_edit").order_by(AuditLog.id.desc())).first()
    assert log is not None
    page = owner_client.get("/payments/expenses").text
    assert "Right desc 101" in page and f"/owner/expenses/{exp.id}/edit" in page


def test_owner_edit_blocked_cases(seeded_db, admin_client, owner_client, cleanup, set_password):
    flats = _flats(seeded_db)
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Edit guard", 70, cleanup)
    # a flat that is not theirs
    assert _edit(owner_client, exp, flat_id=flats["401"]).status_code == 403
    # someone else's expense
    set_password(seeded_db, "manikyamba", OWNER_PASSWORD)
    other = make_client("manikyamba", OWNER_PASSWORD)
    assert _edit(other, exp, flat_id=flats["101"]).status_code == 403
    # pending cancel request
    owner_client.post("/owner/cancel-requests", data={"record_type": "expense", "record_id": exp.id, "reason": "x"})
    r = _edit(owner_client, exp, description="nope")
    assert "owner_err" in r.headers["location"] or "error" in r.headers["location"]
    seeded_db.expire_all()
    assert seeded_db.get(Expense, exp.id).description == "Edit guard"
    # rejected by admin
    req = seeded_db.scalar(select(CancelRequest).where(CancelRequest.record_type == "expense", CancelRequest.record_id == exp.id))
    admin_client.post(f"/admin/cancel-requests/{req.id}/reject", data={"note": "wrong flat"})
    r = _edit(owner_client, exp, description="nope")
    assert "owner_err" in r.headers["location"] or "error" in r.headers["location"]


def test_owner_cannot_edit_after_settlement_or_admin_rows(seeded_db, owner_client, cleanup):
    flats = _flats(seeded_db)
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Settled soon", 40, cleanup)
    from app.models import Settlement
    from datetime import date

    st = Settlement(expense_id=exp.id, amount=exp.amount, settled_on=date(2026, 10, 20))
    seeded_db.add(st)
    seeded_db.get(Expense, exp.id).is_settled = True
    seeded_db.commit()
    r = _edit(owner_client, exp, description="late")
    assert "owner_err" in r.headers["location"] or "error" in r.headers["location"]
    seeded_db.expire_all()
    assert seeded_db.get(Expense, exp.id).description == "Settled soon"


# ---- admin rejects a wrongly submitted expense ------------------------------------------


def test_reject_from_cancel_queue_removes_expense_everywhere(seeded_db, admin_client, owner_client, cleanup):
    flats = _flats(seeded_db)
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Wrong flat 104", 55, cleanup)
    before = balances_svc.overview(seeded_db)["owed"]
    owner_client.post("/owner/cancel-requests", data={"record_type": "expense", "record_id": exp.id, "reason": "wrong flat"})
    seeded_db.expire_all()
    req = seeded_db.scalar(select(CancelRequest).where(CancelRequest.record_type == "expense", CancelRequest.record_id == exp.id))
    r = admin_client.post(f"/admin/cancel-requests/{req.id}/reject", data={"note": "wrong"}, follow_redirects=False)
    assert r.status_code == 303
    seeded_db.expire_all()
    assert seeded_db.get(Expense, exp.id).is_rejected
    assert balances_svc.overview(seeded_db)["owed"] == before - 55
    page = owner_client.get("/payments/expenses").text
    assert "Wrong flat 104" not in page and "Pending for reimbursement" not in page.split("Wrong flat 104")[0][-0:]
    assert "Wrong flat 104" in owner_client.get("/owner/submissions").text
    assert "Rejected" in owner_client.get("/owner/submissions").text


def test_keep_entry_keeps_expense(seeded_db, admin_client, owner_client, cleanup):
    flats = _flats(seeded_db)
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Keep me", 33, cleanup)
    owner_client.post("/owner/cancel-requests", data={"record_type": "expense", "record_id": exp.id, "reason": "x"})
    seeded_db.expire_all()
    req = seeded_db.scalar(select(CancelRequest).where(CancelRequest.record_type == "expense", CancelRequest.record_id == exp.id))
    page = admin_client.get("/admin/cancel-requests").text
    assert "Reject expense" in page and "Keep entry" in page and "Approve cancel" in page
    admin_client.post(f"/admin/cancel-requests/{req.id}/keep", data={"note": ""})
    seeded_db.expire_all()
    assert not seeded_db.get(Expense, exp.id).is_rejected
    assert "Keep me" in owner_client.get("/payments/expenses").text


def test_legacy_rejected_cancel_row_can_reject_expense_now(seeded_db, admin_client, owner_client, cleanup):
    flats = _flats(seeded_db)
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Legacy reject", 22, cleanup)
    owner_client.post("/owner/cancel-requests", data={"record_type": "expense", "record_id": exp.id, "reason": "x"})
    seeded_db.expire_all()
    req = seeded_db.scalar(select(CancelRequest).where(CancelRequest.record_type == "expense", CancelRequest.record_id == exp.id))
    admin_client.post(f"/admin/cancel-requests/{req.id}/keep", data={"note": ""})
    page = admin_client.get("/admin/cancel-requests").text
    assert "Reject expense now" in page
    r = admin_client.post(f"/admin/cancel-requests/{req.id}/reject", data={"note": "later"}, follow_redirects=False)
    assert r.status_code == 303
    seeded_db.expire_all()
    assert seeded_db.get(Expense, exp.id).is_rejected
    assert "Reject expense now" not in admin_client.get("/admin/cancel-requests").text


# ---- simple view: owed total, due hints --------------------------------------------------


def test_simple_view_shows_owed_back_total(seeded_db, owner_client, cleanup):
    flats = _flats(seeded_db)
    already = balances_svc.owner_advances(seeded_db, [flats["201"]])["owed"]
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Owed to me", 123, cleanup)
    page = owner_client.get("/?view=simple").text
    assert "Expenses you paid" in page and "Owed to me" in page
    assert "Unsettled" in page
    total = balances_svc.owner_advances(seeded_db, [flats["201"]])["owed"]
    assert f"₹{total}" in page and total >= 123
    review_svc.reject(seeded_db, seeded_db.get(Expense, exp.id), admin_label="admin", reason="x")
    seeded_db.commit()
    page = owner_client.get("/?view=simple").text
    assert "Owed to me" not in page
    if not already:
        assert "Your unsettled expenses" not in page


def test_due_hints(seeded_db, owner_client):
    flat, period = _unpay(seeded_db, "201")
    assert reminders_svc.maintenance_due_date(period).isoformat() == "2026-10-05"
    assert reminders_svc.due_hint(reminders_svc.maintenance_due_date(period)) == "due 05-10-2026"
    simple = owner_client.get(f"/?view=simple&more=1&period_id={period.id}").text
    assert re.search(r"due 05-10-2026", simple, re.I)
    maint = owner_client.get(f"/payments/maintenance?period_id={period.id}").text
    assert "due 05-10-2026" in maint
    pending = subs_svc.describe(seeded_db, "maintenance_payment", seeded_db.scalar(
        select(MaintenancePayment).where(MaintenancePayment.flat_id == flat.id, MaintenancePayment.period_id == period.id)))
    assert "due 05-10-2026" in str(pending)


# ---- empty owner sections / insights / version -----------------------------------------


def test_empty_cards_hidden_for_owner_with_no_data(seeded_db, admin_client, set_password):
    flat = Flat(number="999")
    seeded_db.add(flat)
    seeded_db.commit()
    try:
        admin_client.post("/admin/users", data={"username": "empty999", "role": "owner", "flat_ids": [flat.id]})
        set_password(seeded_db, "empty999", OWNER_PASSWORD)
        client = make_client("empty999", OWNER_PASSWORD)
        for path in ("/payments/corpus", "/payments/projects", "/payments/expenses", "/corpus"):
            page = client.get(path)
            assert page.status_code == 200
            assert "Your corpus payments" not in page.text
            assert "Your corpus status" not in page.text
            assert "Your project payments" not in page.text
            assert "Your expenses" not in page.text
        admin_page = admin_client.get("/payments/corpus").text
        assert admin_page
    finally:
        seeded_db.rollback()
        user = seeded_db.scalar(select(User).where(User.username == "empty999"))
        if user is not None:
            user.flats = []
            seeded_db.flush()
            seeded_db.delete(user)
        seeded_db.delete(seeded_db.get(Flat, flat.id))
        seeded_db.commit()


def test_no_insights_anywhere(seeded_db, admin_client, owner_client):
    flat = seeded_db.scalar(select(Flat).where(Flat.number == "201"))
    for client in (admin_client, owner_client):
        page = client.get(f"/flats/{flat.id}").text
        assert "Insights" not in page and "insight" not in page.lower().replace("insights_", "")


def test_version_label(seeded_db, owner_client):
    assert APP_VERSION == "nest5-v7.3"
    assert "nest5-v7.3" in owner_client.get("/payments/maintenance").text
