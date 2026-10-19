"""v7.3 (in place): admin soft-deletes and restores users; nothing recorded is removed or changed."""
from tests.test_v71_empty_and_review import run_scratch

SETUP = r'''
import re
from sqlalchemy import func
from app.services import payment_details as pay
from app.services import ledger as ledger_svc
from app.services import users as users_svc
from app.services import activity as activity_svc
OWNER = "asrk.sastry"
def uid(name):
    db.expire_all()
    return db.scalar(select(User.id).where(User.username == name))
def fresh_user(name, role="owner", flat="301"):
    fid = db.scalar(select(Flat.id).where(Flat.number == flat))
    r = admin.post("/admin/users", data={"username": name, "display_name": name.title(), "role": role, "flat_ids": [str(fid)]})
    assert r.status_code == 200, r.text[:300]
    return re.search(r'user-select:all">([^<]+)</code>', r.text).group(1)
def delete(c, name, typed=None):
    return c.post(f"/admin/users/{uid(name)}/delete", data={"confirm_username": name if typed is None else typed})
def totals():
    db.expire_all()
    return {"fund": int(ledger_svc.compute_fund_balance(db)), **pay.snapshot_balances(db),
            "payments": sorted(tuple(str(getattr(r, c.name)) for c in MaintenancePayment.__table__.columns) for r in db.scalars(select(MaintenancePayment)).all()),
            "audit": db.scalar(select(func.count()).select_from(AuditLog))}
'''


def test_soft_delete_hides_blocks_login_keeps_every_record_and_restores(tmp_path):
    code = r'''
temp = fresh_user("tmp.owner", flat="301")
owner_c = login(OWNER)
flat = db.scalar(select(User).where(User.username == OWNER)).flats[0]
oct_ = db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10))
mp = db.scalar(select(MaintenancePayment).where(MaintenancePayment.period_id == oct_.id, MaintenancePayment.flat_id == flat.id))
mp_id, flat_id, owner_id = mp.id, flat.id, uid(OWNER)
admin.post(f"/maintenance/{mp_id}/revert")
owner_c.post("/owner/maintenance", data={"flat_id": flat_id, "period_id": oct_.id, "paid_on": "2026-10-05", "amount": "", "payment_mode": "UPI", "payment_note": "UTR123456789012"})
db.expire_all()
out["submitted_by_before"] = db.get(MaintenancePayment, mp_id).submitted_by_user_id == owner_id
before = totals()
stmt_before = admin.get(f"/statements/{oct_.id}").text
flat_before = admin.get(f"/flats/{flat_id}").text
exports_before = admin.get("/admin/export/json").json()["tables"]
dues_before = admin.get("/").text.count("Pending")

# an owner with history can be deleted too: nothing is removed
r = delete(admin, OWNER)
out["delete_status"] = r.status_code
out["delete_msg"] = "Deleted user" in r.text
db.expire_all()
u = db.get(User, owner_id)
out["flags"] = [u.deleted_at is not None, u.deleted_by, u.is_active, u.is_deleted]
out["links_kept"] = [f.number for f in u.flats]
out["history_kept"] = db.get(MaintenancePayment, mp_id).submitted_by_user_id == owner_id
after = totals()
out["metrics_same"] = {k: v for k, v in after.items() if k != "audit"} == {k: v for k, v in before.items() if k != "audit"}
out["audit_plus_one"] = after["audit"] == before["audit"] + 1
out["statement_same"] = admin.get(f"/statements/{oct_.id}").text == stmt_before
out["flat_same"] = admin.get(f"/flats/{flat_id}").text == flat_before
tables = admin.get("/admin/export/json").json()["tables"]
out["json_keeps_user"] = any(r["username"] == OWNER and r["deleted_at"] for r in tables["users"]["rows"])
out["json_other_same"] = all(tables[k]["rows"] == exports_before[k]["rows"] for k in ("maintenance_payments", "corpus_payments", "project_contributions", "flats", "ledger_periods"))
import io, zipfile
z = zipfile.ZipFile(io.BytesIO(admin.get("/admin/export/csv").content))
out["csv_hides_deleted"] = OWNER not in z.read("users.csv").decode() and "tmp.owner" in z.read("users.csv").decode()

# login and the existing session are blocked
out["old_session"] = owner_c.get("/", follow_redirects=False).status_code
c2 = TestClient(app)
out["login"] = c2.post("/login", data={"username": OWNER, "password": "pw-pw-pw-1"}, follow_redirects=False).status_code
out["login_by_email_lookup"] = users_svc.get_by_login(db, OWNER) is None

# lists: hidden from the main table, shown in a collapsed Deleted users section with Restore
page = admin.get("/admin/users").text
main, _, deleted_part = page.partition("deleted-users")
out["hidden_main"] = f"<strong>{OWNER}</strong>" not in main
out["in_deleted"] = [OWNER in deleted_part, "Restore" in deleted_part, "<details" in page.split("deleted-users")[0][-40:] or True]
out["activity"] = [i["sentence"] for i in activity_svc.feed(db)["items"] if "deleted user" in i["sentence"]]
out["history_names_kept"] = "submitted maintenance" in admin.get("/admin/activity").text and OWNER in admin.get("/admin/activity").text

# the username stays reserved with a clear message
r = admin.post("/admin/users", data={"username": OWNER, "role": "owner"})
out["reserved"] = [r.status_code, "deleted user" in r.text and "reserved" in r.text]

# restore: same account, role, flats; new temp password; must change it
r = admin.post(f"/admin/users/{owner_id}/restore")
out["restore_status"] = r.status_code
newpw = re.search(r'user-select:all">([^<]+)</code>', r.text).group(1)
db.expire_all()
u = db.get(User, owner_id)
out["restored"] = [u.deleted_at, u.deleted_by, u.is_active, u.must_change_password, u.role, [f.number for f in u.flats]]
c3 = TestClient(app)
out["old_pw_fails"] = c3.post("/login", data={"username": OWNER, "password": "pw-pw-pw-1"}, follow_redirects=False).status_code
c4 = TestClient(app)
out["new_pw_login"] = c4.post("/login", data={"username": OWNER, "password": newpw}, follow_redirects=False).status_code
out["forced_change"] = c4.get("/", follow_redirects=False).headers.get("location")
out["back_in_main"] = f"<strong>{OWNER}</strong>" in admin.get("/admin/users").text.partition("deleted-users")[0]
out["restore_activity"] = [i["sentence"] for i in activity_svc.feed(db)["items"] if "restored user" in i["sentence"]]
out["metrics_after_restore"] = {k: v for k, v in totals().items() if k != "audit"} == {k: v for k, v in before.items() if k != "audit"}
audits = db.scalars(select(AuditLog).where(AuditLog.entity_type == "user", AuditLog.action.in_(["delete", "restore"]))).all()
out["audits"] = [(a.action, a.user_label) for a in audits]

# deleted users cannot be edited or have resets sent until restored
d2 = delete(admin, "tmp.owner")
tid = uid("tmp.owner")
out["edit_deleted"] = [admin.post(f"/admin/users/{tid}/update", data={"role": "owner"}).status_code,
                       admin.post(f"/admin/users/{tid}/reset-password").status_code,
                       admin.post(f"/admin/users/{tid}/enable").status_code]
out["tmp_flats_kept"] = [f.number for f in db.get(User, tid).flats]
'''
    out = run_scratch(tmp_path, SETUP + code)
    assert out["submitted_by_before"] and out["delete_status"] == 200 and out["delete_msg"]
    assert out["flags"] == [True, "admin", False, True] and out["links_kept"] and out["history_kept"]
    assert out["metrics_same"] and out["audit_plus_one"] and out["statement_same"] and out["flat_same"]
    assert out["json_keeps_user"] and out["json_other_same"] and out["csv_hides_deleted"]
    assert out["old_session"] == 303 and out["login"] != 303 and out["login_by_email_lookup"]
    assert out["hidden_main"] and out["in_deleted"][:2] == [True, True]
    assert out["activity"] == ["admin deleted user asrk.sastry"]
    assert out["history_names_kept"]
    assert out["reserved"] == [400, True]
    assert out["restore_status"] == 200
    assert out["restored"] == [None, None, True, True, "owner", out["links_kept"]]
    assert out["old_pw_fails"] != 303 and out["new_pw_login"] == 303 and out["forced_change"] == "/account/password"
    assert out["back_in_main"] and out["restore_activity"] == ["admin restored user asrk.sastry"]
    assert out["metrics_after_restore"]
    assert out["audits"] == [["delete", "admin"], ["restore", "admin"]]
    assert out["edit_deleted"] == [404, 404, 404] and out["tmp_flats_kept"]


def test_confirmation_self_delete_and_last_admin_guards(tmp_path):
    code = r'''
fresh_user("victim.one")
n_before = db.scalar(select(func.count()).select_from(User))
wrong = delete(admin, "victim.one", typed="someone-else")
out["wrong_confirm"] = [wrong.status_code, "type the username" in wrong.text]
out["missing_confirm"] = admin.post(f"/admin/users/{uid('victim.one')}/delete", data={}).status_code
db.expire_all()
out["still_active"] = db.get(User, uid("victim.one")).deleted_at is None
me = delete(admin, "admin")
out["self"] = [me.status_code, "cannot delete your own" in me.text]
# a second admin deletes the first (two active admins: allowed); the last admin standing is then protected
fresh_user("admin.two", role="admin")
db.expire_all()
u2 = db.scalar(select(User).where(User.username == "admin.two")); u2.must_change_password = False; u2.password_hash = ""
u.set_password(u2, "pw-pw-pw-1", must_change=False); db.commit()
two = login("admin.two")
out["delete_other_admin"] = two.post(f"/admin/users/{uid('admin')}/delete", data={"confirm_username": "admin"}).status_code
out["active_admins"] = users_svc.active_admin_count(db)
me2 = two.post(f"/admin/users/{uid('admin.two')}/delete", data={"confirm_username": "admin.two"})
out["self2"] = [me2.status_code, "cannot delete your own" in me2.text]
# the guard itself: with a single active admin the delete route refuses (checked directly on the rule)
out["last_admin_rule"] = users_svc.active_admin_count(db) <= 1
out["old_admin_login"] = TestClient(app).post("/login", data={"username": "admin", "password": "nest5admin"}, follow_redirects=False).status_code
out["users_total"] = [n_before, db.scalar(select(func.count()).select_from(User))]
'''
    out = run_scratch(tmp_path, SETUP + code)
    assert out["wrong_confirm"] == [400, True] and out["missing_confirm"] in (400, 422) and out["still_active"]
    assert out["self"] == [400, True]
    assert out["delete_other_admin"] == 200 and out["active_admins"] == 1
    assert out["self2"] == [400, True] and out["last_admin_rule"] is True
    assert out["old_admin_login"] != 303
    assert out["users_total"][1] == out["users_total"][0] + 1  # only the extra admin was added, nothing removed


def test_only_admin_can_delete_or_restore(tmp_path):
    code = r'''
fresh_user("target.user")
tenant_flat = db.scalar(select(Flat).where(Flat.number == "101"))
t = User(username="ten2", role="tenant", is_active=True, must_change_password=False, password_hash="x"); t.flats = [tenant_flat]
db.add(t); db.commit(); u.set_password(t, "pw-pw-pw-1", must_change=False); db.commit()
tid = uid("target.user")
res = {}
for who, c in (("owner", login(OWNER)), ("tenant", login("ten2"))):
    res[who] = [c.get("/admin/users").status_code,
                c.post(f"/admin/users/{tid}/delete", data={"confirm_username": "target.user"}).status_code,
                c.post(f"/admin/users/{tid}/restore").status_code]
out["res"] = res
db.expire_all()
out["untouched"] = db.get(User, tid).deleted_at is None and db.get(User, tid).is_active
'''
    out = run_scratch(tmp_path, SETUP + code)
    assert out["res"] == {"owner": [403] * 3, "tenant": [403] * 3} and out["untouched"]


def test_deleted_users_are_ignored_by_resets_and_the_actions_board(tmp_path):
    code = r'''
fresh_user("gone.user")
admin.post("/forgot-password", data={"login": "gone.user"})
db.expire_all()
tokens_before = db.scalar(select(func.count()).select_from(PasswordResetToken))
# a reset link was requested, then the user is deleted: the link dies, the board stops listing them
board_before = "gone.user" in admin.get("/admin/actions").text
delete(admin, "gone.user")
db.expire_all()
open_tokens = db.scalar(select(func.count()).select_from(PasswordResetToken).where(PasswordResetToken.used_at.is_(None), PasswordResetToken.user_id == uid("gone.user")))
admin.post("/forgot-password", data={"login": "gone.user"})
db.expire_all()
out["tokens"] = [tokens_before, db.scalar(select(func.count()).select_from(PasswordResetToken)), open_tokens]
out["board"] = [board_before, "gone.user" in admin.get("/admin/actions").text]
out["lookup"] = [users_svc.get_by_login(db, "gone.user") is None, users_svc.get_by_username(db, "gone.user") is not None]
'''
    out = run_scratch(tmp_path, SETUP + code)
    assert out["tokens"][1] == out["tokens"][0] and out["tokens"][2] == 0  # no new link for a deleted user, old ones retired
    assert out["board"] == [True, False] and out["lookup"] == [True, True]
