"""v9.1: admin can delete any corpus goal (payments removed after an explicit confirm, backup first, audited)."""
import re
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.models import AuditLog, CancelRequest, CorpusCampaign, CorpusCampaignShare, CorpusLineItem, CorpusPayment, Flat
from app.services import corpus_campaigns as campaigns_svc
from app.services import ledger as ledger_svc


def _flats(db):
    return {f.number: f.id for f in db.scalars(select(Flat)).all()}


def _goal(admin_client, db, name, target=5000, items=""):
    admin_client.post("/corpus/campaigns", data={"name": name, "target_amount": str(target), "line_items": items})
    db.expire_all()
    return db.scalar(select(CorpusCampaign).where(CorpusCampaign.name == name))


def _backups(label):
    return sorted(Path(settings.backup_dir).glob(f"nest5-*-{label}.db")) if Path(settings.backup_dir).exists() else []


def _cleanup(db, goal_id):
    db.expire_all()
    goal = db.get(CorpusCampaign, goal_id)
    if goal is not None:
        for row in list(goal.payments):
            db.delete(row)
        db.flush()
        db.delete(goal)
        db.commit()


def test_admin_deletes_goal_with_payments_after_confirm(seeded_db, admin_client, owner_client):
    flats = _flats(seeded_db)
    goal = _goal(admin_client, seeded_db, "Delete with payments v91", items="Door: 2000\nPanel")
    gid = goal.id
    try:
        item_id = goal.line_items[0].id
        admin_client.post(f"/corpus/campaigns/{gid}/pay", data={"flat_id": flats["102"], "amount": "300", "paid_on": "2026-10-05",
                                                                "line_item_id": item_id})
        admin_client.post(f"/corpus/campaigns/{gid}/pay", data={"flat_id": flats["301"], "amount": "200", "paid_on": "2026-10-06"})
        owner_client.post("/corpus/pay", data={"target": f"{flats['102']}:{gid}", "amount": "100", "paid_on": "2026-10-07",
                                               "payment_mode": "Cash", "payment_note": ""})
        seeded_db.expire_all()
        pays = seeded_db.scalars(select(CorpusPayment).where(CorpusPayment.campaign_id == gid)).all()
        assert len(pays) == 3
        owner_pay = next(p for p in pays if p.submitted_by_user_id)
        owner_pay_id = owner_pay.id
        owner_client.post("/owner/cancel-requests", data={"record_type": "corpus_payment", "record_id": owner_pay_id})
        fund_before = ledger_svc.compute_fund_balance(seeded_db)
        collected_before = campaigns_svc.building_totals(seeded_db)["collected"]

        page = admin_client.get(f"/corpus/goals/{gid}").text
        assert "3 payment(s) totalling ₹600" in page and "Type DELETE" in page and 'name="confirm_payments"' in page
        assert "Delete goal" in page
        assert "Delete goal" not in owner_client.get(f"/corpus/goals/{gid}").text

        # Owners: 403. Admin: DELETE alone, or the checkbox alone, is not enough.
        assert owner_client.post(f"/corpus/goals/{gid}/delete", data={"confirm": "DELETE", "confirm_payments": "yes"},
                                 follow_redirects=False).status_code == 403
        for data in ({"confirm": "DELETE"}, {"confirm_payments": "yes"}, {"confirm": "delete me", "confirm_payments": "yes"}):
            r = admin_client.post(f"/corpus/goals/{gid}/delete", data=data, follow_redirects=False)
            assert "error=" in r.headers["location"], data
        seeded_db.expire_all()
        assert seeded_db.get(CorpusCampaign, gid) is not None and len(
            seeded_db.scalars(select(CorpusPayment).where(CorpusPayment.campaign_id == gid)).all()) == 3

        r = admin_client.post(f"/corpus/goals/{gid}/delete", data={"confirm": "DELETE", "confirm_payments": "yes"},
                              follow_redirects=False)
        assert r.headers["location"].startswith("/corpus?msg=") and "3+payment" in r.headers["location"], r.headers["location"]
        seeded_db.expire_all()
        assert seeded_db.get(CorpusCampaign, gid) is None
        assert not seeded_db.scalars(select(CorpusPayment).where(CorpusPayment.campaign_id == gid)).all()
        assert not seeded_db.scalars(select(CorpusCampaignShare).where(CorpusCampaignShare.campaign_id == gid)).all()
        assert not seeded_db.scalars(select(CorpusLineItem).where(CorpusLineItem.campaign_id == gid)).all()
        assert len(_backups("pre-delete-goal")) >= 1  # automatic backup taken first
        log = seeded_db.scalar(select(AuditLog).where(AuditLog.entity_type == "corpus_campaign", AuditLog.entity_id == gid,
                                                      AuditLog.action == "delete").order_by(AuditLog.id.desc()).limit(1))
        assert log and "Delete with payments v91" in log.before_json and '"amount": 300' in log.before_json
        assert "pre-delete-goal" in log.before_json
        req = seeded_db.scalar(select(CancelRequest).where(CancelRequest.record_type == "corpus_payment",
                                                           CancelRequest.record_id == owner_pay_id))
        assert req.status == "approved"  # pending cancel request closed, not left dangling

        # balances / totals recomputed; pages do not mention the deleted goal
        assert ledger_svc.compute_fund_balance(seeded_db) == fund_before
        assert campaigns_svc.building_totals(seeded_db)["collected"] == collected_before - 600
        for client in (admin_client, owner_client):
            for url in ("/", "/?view=simple", "/corpus", "/payments/corpus", f"/flats/{flats['102']}"):
                r = client.get(url)
                body = re.sub(r'<section class="card dashboard-activity.*?</section>', "", r.text, flags=re.S)
                assert r.status_code == 200 and "Delete with payments v91" not in body, url
        assert admin_client.get(f"/corpus/goals/{gid}").status_code == 404
    finally:
        _cleanup(seeded_db, gid)


def test_admin_deletes_goal_without_payments(seeded_db, admin_client):
    goal = _goal(admin_client, seeded_db, "Delete empty v91", items="A\nB")
    gid = goal.id
    try:
        assert 'name="confirm_payments"' not in admin_client.get(f"/corpus/goals/{gid}").text
        r = admin_client.post(f"/corpus/goals/{gid}/delete", data={"confirm": "DELETE"}, follow_redirects=False)
        assert r.headers["location"].startswith("/corpus?msg=")
        seeded_db.expire_all()
        assert seeded_db.get(CorpusCampaign, gid) is None
    finally:
        _cleanup(seeded_db, gid)


def test_general_goal_can_be_deleted_and_is_not_recreated(seeded_db, admin_client, owner_client):
    from app.main import _sync_roster

    flats = _flats(seeded_db)
    existing = campaigns_svc.general_campaign(seeded_db)
    if existing is not None:  # another test left one: remove it so this test controls the state
        admin_client.post(f"/corpus/goals/{existing.id}/delete", data={"confirm": "DELETE", "confirm_payments": "yes"})
        seeded_db.expire_all()
    admin_client.post("/corpus", data={"flat_id": flats["102"], "amount": "1500", "description": "one-off"})
    seeded_db.expire_all()
    general = campaigns_svc.general_campaign(seeded_db)
    assert general is not None
    gid = general.id
    admin_client.post(f"/corpus/campaigns/{gid}/pay", data={"flat_id": flats["102"], "amount": "500", "paid_on": "2026-10-05"})
    page = admin_client.get(f"/corpus/goals/{gid}").text
    assert "Delete goal" in page and "1 payment(s) totalling ₹500" in page
    try:
        r = admin_client.post(f"/corpus/goals/{gid}/delete", data={"confirm": "DELETE", "confirm_payments": "yes"},
                              follow_redirects=False)
        assert r.headers["location"].startswith("/corpus?msg=")
        seeded_db.expire_all()
        assert campaigns_svc.general_campaign(seeded_db) is None
        _sync_roster()  # what every app start does: it must not bring the goal back
        seeded_db.expire_all()
        assert campaigns_svc.general_campaign(seeded_db) is None
        for client in (admin_client, owner_client):
            for url in ("/", "/?view=simple", "/corpus", "/payments/corpus", "/payments/building", "/calendar"):
                assert client.get(url).status_code == 200, url
    finally:
        seeded_db.expire_all()
        g = campaigns_svc.general_campaign(seeded_db)
        if g is not None:
            _cleanup(seeded_db, g.id)


def test_fresh_install_and_zero_goals_do_not_crash_anything(tmp_path):
    """A brand-new database has no corpus goals at all; every page and export must still work, and
    deleting the General goal later must leave the app in the same working state."""
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'zero.db'}", "INSTANCE_DIR": str(tmp_path / "i"),
           "BACKUP_DIR": str(tmp_path / "b")}
    assert subprocess.run([sys.executable, "scripts/seed.py"], cwd=root, env=env, capture_output=True).returncode == 0
    code = r'''
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app, _sync_roster
from app.database import SessionLocal
from app.models import CorpusCampaign, Flat
from app.services import users as u, corpus_campaigns as cc

db = SessionLocal(); u.ensure_bootstrap_users(db); db.commit()
for name in ("admin", "sastry"):
    u.set_password(u.get_by_username(db, name), "pw-pw-pw-1", must_change=False)
db.commit()
assert db.scalar(select(CorpusCampaign.id)) is None, "a new install must not seed any corpus goal"
flat = {f.number: f.id for f in db.scalars(select(Flat)).all()}

def client(user):
    c = TestClient(app)
    assert c.post("/login", data={"username": user, "password": "pw-pw-pw-1"}, follow_redirects=False).status_code == 303
    return c

admin, owner = client("admin"), client("sastry")
URLS = ["/", "/?view=simple", "/payments/maintenance", "/payments/corpus", "/payments/projects", "/payments/expenses",
        "/payments/building", "/expenses", "/corpus", "/projects", "/schedule", "/calendar", "/complaints",
        "/owner/submissions", f"/flats/{flat['102']}", f"/flats/{flat['102']}/statement.pdf?period_id=", "/owner/statement.pdf?period_id="]
ADMIN_ONLY = ["/admin/actions", "/settlements", "/flats", "/admin/export", "/admin/export/pdf?period_id=",
              "/admin/export/excel", "/admin/export/csv", "/admin/export/json", "/audit"]

def sweep(tag):
    for c, urls in ((admin, URLS + ADMIN_ONLY), (owner, URLS)):
        for url in urls:
            r = c.get(url)
            assert r.status_code == 200, (tag, url, r.status_code)

sweep("no goals")
r = owner.post("/corpus/pay", data={"target": f"{flat['102']}:1", "amount": "10", "paid_on": "2026-10-05"}, follow_redirects=False)
assert r.status_code in (303, 400, 404, 422), r.status_code   # no goal to pay: refused, not a crash

admin.post("/corpus", data={"flat_id": flat["102"], "amount": "1000", "description": "one-off"})
general = cc.general_campaign(db); assert general is not None
admin.post(f"/corpus/campaigns/{general.id}/pay", data={"flat_id": flat["102"], "amount": "400", "paid_on": "2026-10-05"})
sweep("general with payment")
r = admin.post(f"/corpus/goals/{general.id}/delete", data={"confirm": "DELETE", "confirm_payments": "yes"}, follow_redirects=False)
assert r.headers["location"].startswith("/corpus?msg="), r.headers["location"]
db.expire_all()
assert cc.general_campaign(db) is None
_sync_roster(); db.expire_all()
assert cc.general_campaign(db) is None, "startup must not recreate a deleted General goal"
sweep("general deleted")
assert owner.get("/corpus").status_code == 200
print("OK")
'''
    ran = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True)
    assert "OK" in ran.stdout, (ran.stdout + ran.stderr)[-2500:]
    assert list((tmp_path / "b").glob("nest5-*-pre-delete-goal.db"))


def test_confirm_dialogs_are_not_shadowed_by_a_field_called_confirm():
    """Inside a form, a control named `confirm` hides window.confirm() in inline handlers, so the browser
    dialog silently never appeared. Forms with such a field must call window.confirm()."""
    import re

    templates = Path(__file__).resolve().parents[1] / "app" / "templates"
    checked = 0
    for path in templates.glob("*.html"):
        for form in re.findall(r"<form\b.*?</form>", path.read_text(encoding="utf-8"), flags=re.S):
            if 'name="confirm"' in form and "onsubmit" in form:
                checked += 1
                assert "return window.confirm(" in form, path.name
                assert not re.search(r"return confirm\(", form), path.name
    assert checked >= 3
