import re

from sqlalchemy import select

from app.models import AuditLog, Complaint, Flat, LedgerPeriod, MaintenancePayment
from tests.conftest import OWNER_PASSWORD, make_client


def _ids(db):
    flats = {f.number: f.id for f in db.scalars(select(Flat)).all()}
    oct_id = db.scalars(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10)).one().id
    mp_id = db.scalars(select(MaintenancePayment.id)).first()
    return flats, oct_id, mp_id


ADMIN_ONLY_GET = [
    "/settlements",
    "/flats",
    "/audit",
    "/periods/rollover",
    "/admin/maintenance-fee",
    "/admin/users",
    "/admin/export",
    "/admin/export/excel",
    "/admin/export/csv",
    "/admin/export/json",
    "/admin/export/pdf",
    "/admin/export/sqlite",
]


def _admin_only_post(mp_id, oct_id):
    return [
        f"/maintenance/{mp_id}/pay",
        f"/maintenance/{mp_id}/revert",
        "/expenses",
        "/expenses/1/edit",
        "/expenses/1/delete",
        "/settlements/1/settle",
        "/settlements/1/revert",
        "/admin/maintenance-fee",
        "/projects",
        "/projects/payments/1/pay",
        "/corpus",
        "/corpus/1/pay",
        "/schedule",
        "/schedule/1/done",
        "/schedule/1/delete",
        f"/periods/{oct_id}/close",
        "/admin/users",
        "/admin/users/1/update",
        "/admin/users/1/disable",
        "/admin/users/1/reset-password",
        "/admin/users/1/send-reset-link",
        "/admin/export/backup-now",
        "/admin/export/restore",
        "/admin/export/restore/confirm",
    ]


def test_owner_gets_403_on_admin_routes(seeded_db, owner_client):
    flats, oct_id, mp_id = _ids(seeded_db)
    for url in ADMIN_ONLY_GET + [f"/flats/{flats['101']}", f"/flats/{flats['301']}"]:
        r = owner_client.get(url, follow_redirects=False)
        assert r.status_code == 403, url
    for url in _admin_only_post(mp_id, oct_id):
        r = owner_client.post(url, data={}, follow_redirects=False)
        assert r.status_code == 403, url
    paid_before = seeded_db.get(MaintenancePayment, mp_id).is_paid
    seeded_db.expire_all()
    assert seeded_db.get(MaintenancePayment, mp_id).is_paid == paid_before


def test_owner_can_view_ledger_pages(seeded_db, owner_client):
    flats, oct_id, _ = _ids(seeded_db)
    for url in [
        "/",
        f"/?period_id={oct_id}",
        "/payments/maintenance",
        "/payments/corpus",
        "/expenses",
        "/projects",
        "/schedule",
        "/complaints",
        f"/statements/{oct_id}",
        f"/flats/{flats['201']}",
        "/account/password",
    ]:
        r = owner_client.get(url)
        assert r.status_code == 200, url
    page = owner_client.get("/payments/maintenance").text
    assert "Mark paid" not in page and "Revert" not in page
    dash = owner_client.get("/").text
    assert "Audit log" not in dash and "/admin/users" not in dash and "/settlements" not in dash.split("<main>")[0]
    assert "My flat 201" in dash
    assert "Delete" not in owner_client.get("/expenses").text


def test_owner_with_two_flats_sees_both(seeded_db, set_password):
    flats, _, _ = _ids(seeded_db)
    set_password(seeded_db, "manikyamba", OWNER_PASSWORD)
    client = make_client("manikyamba", OWNER_PASSWORD)
    assert client.get(f"/flats/{flats['101']}").status_code == 200
    assert client.get(f"/flats/{flats['401']}").status_code == 200
    assert client.get(f"/flats/{flats['201']}").status_code == 403


def test_owner_can_raise_complaint_as_open(seeded_db, owner_client):
    r = owner_client.post(
        "/complaints",
        data={"title": "Lobby light", "reported_on": "2026-10-08", "status": "resolved"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    seeded_db.expire_all()
    c = seeded_db.scalars(select(Complaint).where(Complaint.title == "Lobby light")).one()
    assert c.status == "open"
    log = seeded_db.scalars(
        select(AuditLog).where(AuditLog.entity_type == "complaint", AuditLog.entity_id == c.id)
    ).one()
    assert log.user_label == "asrk.sastry"


def test_admin_can_do_everything(seeded_db, admin_client):
    flats, oct_id, _ = _ids(seeded_db)
    for url in ADMIN_ONLY_GET + [
        "/",
        "/payments/maintenance",
        "/payments/corpus",
        "/expenses",
        "/projects",
        "/schedule",
        "/complaints",
        f"/statements/{oct_id}",
        f"/flats/{flats['301']}",
        "/account/password",
    ]:
        r = admin_client.get(url)
        assert r.status_code == 200, url
    dash = admin_client.get("/").text
    for link in ["/audit", "/settlements", "/admin/users", "/admin/export", "/admin/maintenance-fee"]:
        assert link in dash


def test_admin_user_management_and_audit(seeded_db, admin_client):
    flats, _, _ = _ids(seeded_db)
    r = admin_client.post(
        "/admin/users",
        data={"username": "Tenant101", "display_name": "Tenant", "role": "owner", "flat_ids": [str(flats["101"])]},
    )
    assert r.status_code == 200
    temp = re.search(r'user-select:all">([^<]+)</code>', r.text).group(1)
    assert "tenant101" in r.text

    # First login is forced through the password change page.
    client = make_client("tenant101", temp)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account/password"
    r = client.post(
        "/account/password",
        data={"current_password": temp, "new_password": "brand-new-pass", "confirm_password": "brand-new-pass"},
    )
    assert r.status_code == 200 and "Password changed" in r.text
    assert client.get("/").status_code == 200
    assert client.get(f"/flats/{flats['101']}").status_code == 200

    from app.services import users as users_svc

    seeded_db.expire_all()
    user = users_svc.get_by_username(seeded_db, "tenant101")
    r = admin_client.post(f"/admin/users/{user.id}/reset-password")
    temp2 = re.search(r'user-select:all">([^<]+)</code>', r.text).group(1)
    r = admin_client.post(f"/admin/users/{user.id}/disable")
    assert "Disabled" in r.text
    # Disabled: existing session is dropped and login fails.
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    from fastapi.testclient import TestClient

    from app.main import app

    fresh = TestClient(app)
    r = fresh.post("/login", data={"username": "tenant101", "password": temp2}, follow_redirects=False)
    assert r.status_code == 401

    r = admin_client.post(
        f"/admin/users/{user.id}/update",
        data={"display_name": "Tenant", "email": "t@example.com", "role": "owner", "flat_ids": [str(flats["101"]), str(flats["201"])]},
    )
    assert r.status_code == 200

    seeded_db.expire_all()
    logs = seeded_db.scalars(
        select(AuditLog).where(AuditLog.entity_type == "user", AuditLog.entity_id == user.id)
    ).all()
    actions = {log.action for log in logs}
    assert {"create", "password_change", "password_reset", "disable", "update"} <= actions
    blob = " ".join((log.before_json or "") + (log.after_json or "") for log in logs)
    for secret in (temp, temp2, "brand-new-pass", "pbkdf2"):
        assert secret not in blob
    assert all(log.user_label in ("admin", "tenant101") for log in logs)


def test_cannot_disable_last_admin(seeded_db, admin_client):
    from app.services import users as users_svc

    admin = users_svc.get_by_username(seeded_db, "admin")
    r = admin_client.post(f"/admin/users/{admin.id}/disable")
    assert r.status_code == 400
    r = admin_client.post(f"/admin/users/{admin.id}/update", data={"role": "owner"})
    assert r.status_code == 400


def test_blank_username_logs_in_as_admin(seeded_db, admin_client):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post("/login", data={"password": "nest5admin"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/admin/users").status_code == 200


def test_unauthenticated_redirects_to_login(seeded_db):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    assert client.get("/admin/users", follow_redirects=False).headers["location"] == "/login"
    assert client.post("/admin/users", follow_redirects=False).status_code == 401
