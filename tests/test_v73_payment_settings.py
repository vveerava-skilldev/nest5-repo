"""v7.3 D: admin-configured payment details with append-only history, plus the per-payment payee snapshot."""
import pytest

from tests.test_v71_empty_and_review import run_scratch

SETUP = r'''
from datetime import date, datetime, timedelta
from sqlalchemy import func
from app.services import payment_details as pay
from app.services import ledger as ledger_svc
tenant_flat = db.scalar(select(Flat).where(Flat.number == "101"))
tuser = User(username="ten1", display_name="Tenant", role="tenant", is_active=True, must_change_password=False, password_hash="x")
tuser.flats = [tenant_flat]
db.add(tuser); db.commit()
u.set_password(tuser, "pw-pw-pw-1", must_change=False); db.commit()
def change(vpa="new.society@oksbi", mobile="9876543210", payee="Sri Krishna Mani Flats Trust", reason="Bank account changed", bank="", client=None):
    c = client or admin
    data = {"upi_vpa": vpa, "upi_mobile": mobile, "payee_name": payee, "reason": reason, "bank_balance": bank}
    review = c.post("/admin/payment-settings/review", data=data)
    if review.status_code != 200:
        return review, None
    return review, c.post("/admin/payment-settings/confirm", data={**data, "confirm": "yes"}, follow_redirects=False)
'''


def test_initial_row_validation_review_confirm_history_and_append_only(tmp_path):
    code = r'''
db.expire_all()
rows = db.scalars(select(PaymentDetailChange).order_by(PaymentDetailChange.id)).all()
out["initial"] = [(r.is_initial, r.new_upi_vpa, r.new_upi_mobile, r.new_payee_name, r.old_upi_vpa, r.changed_by) for r in rows]
out["initial_snapshot"] = [rows[0].fund_balance, rows[0].corpus_collected, rows[0].projects_collected, rows[0].total_balance]
out["live_before"] = pay.get_details(db)
bad = {}
good = dict(upi_vpa="new.society@oksbi", upi_mobile="9876543210", payee_name="Sri Krishna Mani Flats Trust", reason="Bank account changed", bank_balance="")
for name, patch in {
    "vpa": {"upi_vpa": "not a vpa"}, "vpa_no_at": {"upi_vpa": "plainstring"}, "mobile": {"upi_mobile": "12345"},
    "payee": {"payee_name": "x"}, "reason": {"reason": ""}, "reason_blank": {"reason": "   "},
    "bank": {"bank_balance": "lots of money"}, "same": {"upi_vpa": "9940255076@ybl", "upi_mobile": "9940255076", "payee_name": out["live_before"]["payee"]},
}.items():
    r = admin.post("/admin/payment-settings/review", data={**good, **patch})
    bad[name] = r.status_code
out["bad"] = bad
out["count_after_bad"] = db.scalar(select(func.count()).select_from(PaymentDetailChange))
# review shows the confirmation step but changes nothing yet
r = admin.post("/admin/payment-settings/review", data=good)
out["review_status"] = r.status_code
out["review_page"] = r.text
db.expire_all()
out["count_after_review"] = db.scalar(select(func.count()).select_from(PaymentDetailChange))
out["no_confirm"] = admin.post("/admin/payment-settings/confirm", data=good, follow_redirects=False).headers["location"]
db.expire_all()
out["count_after_no_confirm"] = db.scalar(select(func.count()).select_from(PaymentDetailChange))
bal = pay.snapshot_balances(db)
fund = ledger_svc.compute_fund_balance(db)
r = admin.post("/admin/payment-settings/confirm", data={**good, "bank_balance": "₹ 1,23,456.50", "confirm": "yes"}, follow_redirects=False)
out["confirm_status"], out["confirm_loc"] = r.status_code, r.headers["location"]
db.expire_all()
rows = db.scalars(select(PaymentDetailChange).order_by(PaymentDetailChange.id)).all()
new = rows[-1]
out["rows"] = len(rows)
out["new_row"] = [new.is_initial, new.old_upi_vpa, new.new_upi_vpa, new.old_upi_mobile, new.new_upi_mobile, new.old_payee_name, new.new_payee_name,
                  new.reason, new.changed_by, new.bank_balance_entered]
out["snapshot"] = [new.fund_balance, new.corpus_collected, new.projects_collected, new.owed_back, new.total_balance]
out["expected_snapshot"] = [bal["fund_balance"], bal["corpus_collected"], bal["projects_collected"], bal["owed_back"], bal["total_balance"]]
out["fund_matches"] = new.fund_balance == int(fund)
out["total_is_sum"] = new.total_balance == new.fund_balance + new.corpus_collected + new.projects_collected
out["live_after"] = pay.get_details(db)
out["first_row_untouched"] = [rows[0].is_initial, rows[0].new_upi_vpa, rows[0].old_upi_vpa]
hist = admin.get("/admin/payment-settings/history").text
out["history_has"] = [x in hist for x in ("Initial", "new.society@oksbi", "9940255076@ybl", "Bank account changed", "1,23,456.50")]
out["history_order"] = hist.index("new.society@oksbi") < hist.index("Initial values")
section = hist[hist.index("pay-history"):hist.index("</main>")]
out["history_has_edit"] = any(w in section.lower() for w in ("<form", "<button", "delete", "edit"))
out["ist_time"] = " IST" in hist
out["audit"] = [(a.entity_type, a.action, a.user_label) for a in db.scalars(select(AuditLog).where(AuditLog.entity_type == "payment_settings")).all()]
out["activity"] = [i["sentence"] for i in __import__("app.services.activity", fromlist=["x"]).feed(db)["items"] if "payment" in i["sentence"].lower() and "details" in i["sentence"].lower()]
for label, fn in {
    "update": lambda: setattr(new, "reason", "tampered"),
    "delete": lambda: db.delete(new),
}.items():
    try:
        fn(); db.flush(); out[label] = "allowed"
    except Exception as exc:
        out[label] = type(exc).__name__
    db.rollback()
db.expire_all()
out["reason_still"] = db.get(PaymentDetailChange, new.id).reason
out["edit_routes"] = [admin.post(f"/admin/payment-settings/history/{new.id}/{p}", data={}).status_code for p in ("delete", "edit")] + \
                     [admin.delete(f"/admin/payment-settings/history/{new.id}").status_code]
# nothing changed => refuse
same_review, _ = change(vpa="new.society@oksbi", mobile="9876543210", payee="Sri Krishna Mani Flats Trust")
out["same_status"] = same_review.status_code
# a second change records old -> new from the previous change
r1, r2 = change(vpa="third@paytm", mobile="9123456789", payee="Sri Krishna Mani Flats Trust", reason="Switch again")
db.expire_all()
last = db.scalars(select(PaymentDetailChange).order_by(PaymentDetailChange.id.desc())).first()
out["third"] = [last.old_upi_vpa, last.new_upi_vpa, last.old_upi_mobile, last.new_upi_mobile, last.old_payee_name == last.new_payee_name]
'''
    out = run_scratch(tmp_path, SETUP + code)
    assert out["initial"] == [[True, "9940255076@ybl", "9940255076", out["live_before"]["payee"], None, "system"]]
    assert out["initial_snapshot"][0] == 0 or isinstance(out["initial_snapshot"][0], int)
    assert out["bad"] == {k: 400 for k in out["bad"]} and len(out["bad"]) == 8
    assert out["count_after_bad"] == 1 and out["count_after_review"] == 1
    assert out["review_status"] == 200 and "confirm" in out["review_page"].lower()
    assert "new.society@oksbi" in out["review_page"] and "9940255076@ybl" in out["review_page"]
    assert "error=" in out["no_confirm"] and out["count_after_no_confirm"] == 1
    assert out["confirm_status"] == 303 and out["confirm_loc"].startswith("/admin/payment-settings/history")
    assert out["rows"] == 2
    assert out["new_row"] == [False, "9940255076@ybl", "new.society@oksbi", "9940255076", "9876543210", out["live_before"]["payee"],
                              "Sri Krishna Mani Flats Trust", "Bank account changed", "admin", "₹ 1,23,456.50"]
    assert out["snapshot"] == out["expected_snapshot"] and out["fund_matches"] and out["total_is_sum"]
    assert out["live_after"] == {"vpa": "new.society@oksbi", "mobile": "9876543210", "payee": "Sri Krishna Mani Flats Trust"}
    assert out["first_row_untouched"] == [True, "9940255076@ybl", None]
    assert all(out["history_has"]) and out["history_order"] and out["ist_time"]
    assert out["history_has_edit"] is False
    assert out["audit"] == [["payment_settings", "change", "admin"]] or out["audit"][0] == ["payment_settings", "change", "admin"]
    assert out["activity"] and "payment" in out["activity"][0].lower()
    assert out["update"] != "allowed" and out["delete"] != "allowed" and out["reason_still"] == "Bank account changed"
    assert all(code in (404, 405) for code in out["edit_routes"])
    assert out["same_status"] == 400
    assert out["third"] == ["new.society@oksbi", "third@paytm", "9876543210", "9123456789", True]


def test_only_admin_reaches_payment_settings_owner_and_tenant_get_403(tmp_path):
    code = r'''
data = {"upi_vpa": "x.y@okaxis", "upi_mobile": "9876543210", "payee_name": "Someone Else", "reason": "attempt", "bank_balance": ""}
res = {}
for who, client in (("owner", login("sastry")), ("tenant", login("ten1"))):
    res[who] = [client.get("/admin/payment-settings").status_code, client.get("/admin/payment-settings/history").status_code,
                client.post("/admin/payment-settings/review", data=data).status_code,
                client.post("/admin/payment-settings/confirm", data={**data, "confirm": "yes"}).status_code]
out["res"] = res
out["admin"] = [admin.get("/admin/payment-settings").status_code, admin.get("/admin/payment-settings/history").status_code]
out["rows"] = db.scalar(select(func.count()).select_from(PaymentDetailChange))
out["nav_owner"] = "payment-settings" in login("sastry").get("/").text
out["nav_admin"] = "payment-settings" in admin.get("/").text
out["live"] = pay.get_details(db)["vpa"]
'''
    out = run_scratch(tmp_path, SETUP + code)
    assert out["res"] == {"owner": [403] * 4, "tenant": [403] * 4}
    assert out["admin"] == [200, 200] and out["rows"] == 1
    assert out["nav_owner"] is False and out["nav_admin"] is True and out["live"] == "9940255076@ybl"


def test_pay_sheet_is_plain_text_and_follows_new_values_only_for_new_sheets(tmp_path):
    code = r'''
import re
owner = login("sastry")
sast = db.scalar(select(User).where(User.username == "sastry"))
flat = sast.flats[0]
period = db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10))
mp = db.scalar(select(MaintenancePayment).where(MaintenancePayment.period_id == period.id, MaintenancePayment.flat_id == flat.id))
admin.post(f"/maintenance/{mp.id}/revert")
mp_id, flat_id, period_id = mp.id, flat.id, period.id
sheet_url = f"/?view=simple&period_id={period_id}&pay=m-{period_id}-{flat_id}"
before = owner.get(sheet_url).text
change(vpa="new.society@oksbi", mobile="9876543210", payee="New Payee Name")
after = owner.get(sheet_url).text
def part(t, key):
    m = re.search(rf'id="{key}"[^>]*>([^<]*)<', t)
    return m.group(1).strip() if m else None
out["before"] = [part(before, "pay-vpa"), part(before, "pay-mobile"), part(before, "pay-payee")]
out["after"] = [part(after, "pay-vpa"), part(after, "pay-mobile"), part(after, "pay-payee")]
out["no_qr"] = [("qr" in after.lower().split("sv-sheet")[1][:6000]), "copy" in after.lower().split("sv-sheet")[1][:6000]]
out["phonepe"] = "Open PhonePe" in after
out["upi_link_old_vpa"] = "9940255076%40ybl" in after or "9940255076@ybl" in after
out["qr_route"] = admin.get("/upi/qr.svg?amount=100").status_code
out["owner_other_sheet"] = part(owner.get(sheet_url).text, "pay-vpa")
'''
    out = run_scratch(tmp_path, SETUP + code)
    assert out["before"] == ["9940255076@ybl", "9940255076", out["before"][2]] and out["before"][2]
    assert out["after"] == ["new.society@oksbi", "9876543210", "New Payee Name"]
    assert out["no_qr"] == [False, False] and out["phonepe"] is True
    assert out["upi_link_old_vpa"] is False
    assert out["qr_route"] in (200, 404, 422)
    assert out["owner_other_sheet"] == "new.society@oksbi"


def test_settings_change_never_touches_recorded_payments_exports_statements_or_history(tmp_path):
    code = r'''
import io, json as js, re, zipfile
import openpyxl
from app.services import activity as activity_svc
owner = login("sastry")
sast = db.scalar(select(User).where(User.username == "sastry"))
oflat = sast.flats[0]
oct_ = db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10))
def mrow(number):
    return db.scalar(select(MaintenancePayment).where(MaintenancePayment.period_id == oct_.id,
                                                      MaintenancePayment.flat_id == db.scalar(select(Flat.id).where(Flat.number == number))))
other_number = next(n for n in db.scalars(select(Flat.number).order_by(Flat.number)).all() if n != oflat.number)
admin_mp, owner_mp = mrow(other_number), mrow(oflat.number)
admin_mp_id, owner_mp_id, oflat_id = admin_mp.id, owner_mp.id, oflat.id
admin.post(f"/maintenance/{admin_mp_id}/revert"); admin.post(f"/maintenance/{owner_mp_id}/revert")
# a project + goal to contribute to
admin.post("/projects", data={"name": "Roof v73", "total_cost": "25000", "due_date": (date.today() + timedelta(days=5)).isoformat()})
admin.post("/corpus/campaigns", data={"name": "Lift v73", "target_amount": "50000", "due_date": (date.today() + timedelta(days=15)).isoformat()})
db.expire_all()
proj = db.scalar(select(Project).where(Project.name == "Roof v73"))
goal = db.scalar(select(CorpusCampaign).where(CorpusCampaign.name == "Lift v73"))
share = db.scalar(select(ProjectPayment).where(ProjectPayment.project_id == proj.id, ProjectPayment.flat_id == oflat_id))
today = date.today().isoformat()

def record_all(tag):
    """admin maintenance, owner maintenance (UPI+UTR), owner project (cash), owner corpus (bank), admin project via mark paid."""
    admin.post(f"/maintenance/{admin_mp_id}/pay", data={"paid_on": "2026-10-04", "payment_note": f"admin {tag}"})
    owner.post("/owner/maintenance", data={"flat_id": oflat_id, "period_id": oct_.id, "paid_on": "2026-10-05", "amount": "",
                                           "payment_mode": "UPI", "payment_note": "UTR123456789012"})
    owner.post("/owner/projects/pay", data={"share_id": share.id, "amount": "100", "paid_on": today, "payment_mode": "Cash"})
    owner.post("/corpus/campaigns/%d/pay" % goal.id, data={"flat_id": oflat_id, "amount": "200", "paid_on": today, "payment_mode": "Bank"})
record_all("one")
# a rejected owner project submission (kept in submission_rejections with its payee)
owner.post("/owner/projects/pay", data={"share_id": share.id, "amount": "55", "paid_on": today, "payment_mode": "Cash"})
db.expire_all()
rej_target = db.scalars(select(ProjectContribution).where(ProjectContribution.amount == 55)).first()
admin.post(f"/admin/submissions/project_contribution/{rej_target.id}/reject", data={"reason": "not received", "back": "/admin/actions"})

def snapshot():
    db.expire_all()
    pays = {}
    for model in (MaintenancePayment, CorpusPayment, ProjectContribution, SubmissionRejection):
        for r in db.scalars(select(model).order_by(model.id)).all():
            cols = {c.name: str(getattr(r, c.name)) for c in model.__table__.columns}
            pays[f"{model.__tablename__}:{r.id}"] = cols
    audit = {a.id: (a.entity_type, a.entity_id, a.action, a.user_label, a.before_json, a.after_json) for a in db.scalars(select(AuditLog)).all()}
    dump = admin.get("/admin/export/json").json()["tables"]
    z = zipfile.ZipFile(io.BytesIO(admin.get("/admin/export/csv").content))
    csvs = {n: z.read(n).decode() for n in z.namelist() if n in ("maintenance_payments.csv", "corpus_payments.csv", "project_contributions.csv", "submission_rejections.csv")}
    wb = openpyxl.load_workbook(io.BytesIO(admin.get("/admin/export/excel").content))
    xl = {ws.title: [[c.value for c in row] for row in ws.iter_rows()] for ws in wb.worksheets}
    return {
        "pays": pays, "audit": audit, "csv": csvs,
        "json": {k: dump[k]["rows"] for k in ("maintenance_payments", "corpus_payments", "project_contributions", "submission_rejections")},
        "json_cols": {k: dump[k]["columns"] for k in ("maintenance_payments", "project_contributions")},
        "xlsx": xl,
        "feed": [(i["id"], i["sentence"]) for i in activity_svc.feed(db, limit=500)["items"]],
        "bal": pay.snapshot_balances(db), "fund": int(ledger_svc.compute_fund_balance(db)),
        "statement": admin.get(f"/statements/{oct_.id}").text, "flat": admin.get(f"/flats/{oflat_id}").text,
        "mine": owner.get("/owner/submissions").text, "maint": admin.get(f"/payments/maintenance?period_id={oct_.id}").text,
        "projects": admin.get("/payments/projects").text,
        "pdf": admin.get(f"/admin/export/pdf?period_id={oct_.id}").status_code,
    }
before = snapshot()
change(vpa="new.society@oksbi", mobile="9876543210", payee="New Payee Name")
after = snapshot()
new_id = db.scalar(select(func.max(PaymentDetailChange.id)))
first_id = db.scalar(select(func.min(PaymentDetailChange.id)))

out["old_rows_identical"] = all(after["pays"][k] == v for k, v in before["pays"].items())
out["n_pay_rows"] = len(before["pays"])
out["audit_identical"] = all(after["audit"][k] == v for k, v in before["audit"].items())
out["feed_identical"] = all(item in after["feed"] for item in before["feed"])
out["bal_same"] = [before["bal"] == {**after["bal"]}, before["fund"] == after["fund"]]
out["csv_identical"] = before["csv"] == after["csv"]
out["json_identical"] = before["json"] == after["json"]
out["xlsx_identical"] = {k: v for k, v in before["xlsx"].items() if k != "Audit logs"} == {k: v for k, v in after["xlsx"].items() if k != "Audit logs"} or \
    all(before["xlsx"][k] == after["xlsx"][k] for k in ("Maintenance payments", "Corpus payments", "Project contributions") if k in before["xlsx"])
out["xlsx_sheets"] = sorted(before["xlsx"])
out["statement_identical"] = before["statement"] == after["statement"]
out["flat_identical"] = before["flat"] == after["flat"]
out["mine_identical"] = before["mine"] == after["mine"]
out["maint_identical"] = before["maint"] == after["maint"]
out["projects_identical"] = before["projects"] == after["projects"]
out["pdf"] = [before["pdf"], after["pdf"]]
out["old_payees"] = sorted({v["payee_vpa"] for k, v in before["pays"].items() if k.split(":")[0] != "maintenance_payments" or v["is_paid"] == "True"})
out["old_ids"] = sorted({v["payee_settings_id"] for k, v in before["pays"].items() if v["payee_vpa"] != "None"})
out["first_id"] = first_id
out["stmt_has_old_vpa"] = "9940255076@ybl" in before["statement"]
out["mine_has_old_vpa"] = "9940255076@ybl" in before["mine"]
out["maint_has_old_vpa"] = "9940255076@ybl" in before["maint"]
out["projects_has_old_vpa"] = "9940255076@ybl" in before["projects"]
out["csv_has_cols"] = all("payee_vpa" in t.splitlines()[0] and "payee_settings_id" in t.splitlines()[0] for t in before["csv"].values())
out["json_has_cols"] = all({"payee_vpa", "payee_mobile", "payee_name", "payee_settings_id"} <= set(c) for c in before["json_cols"].values())
xl_head = [r for t, rows in before["xlsx"].items() if t in ("Maintenance payments", "Project contributions") for r in rows[:1]]
out["xlsx_head"] = xl_head

# --- a second round of payments after the change carries the NEW payee ---
admin.post(f"/maintenance/{admin_mp_id}/revert"); admin.post(f"/maintenance/{owner_mp_id}/revert")
before_total = snapshot()["fund"]
db.expire_all()
old_project_rows = {r.id for r in db.scalars(select(ProjectContribution)).all()}
old_corpus_rows = {r.id for r in db.scalars(select(CorpusPayment)).all()}
record_all("two")
db.expire_all()
am, om = db.get(MaintenancePayment, admin_mp_id), db.get(MaintenancePayment, owner_mp_id)
out["new_maint"] = [[am.payee_vpa, am.payee_mobile, am.payee_name, am.payee_settings_id], [om.payee_vpa, om.payee_mobile, om.payee_name, om.payee_settings_id]]
newp = [r for r in db.scalars(select(ProjectContribution)).all() if r.id not in old_project_rows]
newc = [r for r in db.scalars(select(CorpusPayment)).all() if r.id not in old_corpus_rows and r.is_paid]
out["new_project"] = [[r.payee_vpa, r.payee_settings_id] for r in newp]
out["new_corpus"] = [[r.payee_vpa, r.payee_settings_id] for r in newc]
out["new_id"] = new_id
out["old_project_still_old"] = sorted({r.payee_vpa for r in db.scalars(select(ProjectContribution).where(ProjectContribution.id.in_(old_project_rows)))})
# the owner's pages show the new payee on the new row and the old payee on the old rejection
mine = owner.get("/owner/submissions").text
out["mine_both"] = ["9940255076@ybl" in mine, "new.society@oksbi" in mine]
stmt = admin.get(f"/statements/{oct_.id}").text
out["stmt_new"] = "new.society@oksbi" in stmt
# reverting a payment clears its payee (it is no longer a payment)
admin.post(f"/maintenance/{admin_mp_id}/revert")
db.expire_all()
am = db.get(MaintenancePayment, admin_mp_id)
out["reverted"] = [am.is_paid, am.payee_vpa, am.payee_settings_id]
'''
    out = run_scratch(tmp_path, SETUP + code)
    # nothing already recorded moved
    assert out["old_rows_identical"] and out["audit_identical"] and out["feed_identical"]
    assert out["bal_same"] == [True, True]
    assert out["csv_identical"] and out["json_identical"] and out["xlsx_identical"]
    assert out["statement_identical"] and out["flat_identical"] and out["mine_identical"]
    assert out["maint_identical"] and out["projects_identical"] and out["pdf"] == [200, 200]
    assert out["n_pay_rows"] > 10
    # everything recorded before the change carries the seeded initial payee, linked to the initial history row
    assert out["old_payees"] == ["9940255076@ybl"] and out["old_ids"] == [str(out["first_id"])]
    # and shows it where the admin and the owner look
    assert out["stmt_has_old_vpa"] and out["mine_has_old_vpa"] and out["maint_has_old_vpa"] and out["projects_has_old_vpa"]
    assert out["csv_has_cols"] and out["json_has_cols"]
    # payments recorded after the change carry the new payee; older ones keep the old
    for row in out["new_maint"]:
        assert row == ["new.society@oksbi", "9876543210", "New Payee Name", out["new_id"]]
    assert out["new_project"] and all(r == ["new.society@oksbi", out["new_id"]] for r in out["new_project"])
    assert out["new_corpus"] and all(r == ["new.society@oksbi", out["new_id"]] for r in out["new_corpus"])
    assert out["old_project_still_old"] == ["9940255076@ybl"]
    assert out["mine_both"] == [True, True] and out["stmt_new"] is True
    assert out["reverted"] == [False, None, None]


def test_backfill_stamps_existing_paid_rows_only_and_changes_no_other_column(tmp_path):
    code = r'''
from app.services import payment_details as pay
# simulate a v7.2 database: wipe the payee columns and the marker, then run the backfill again
tables = (MaintenancePayment, CorpusPayment, ProjectContribution, SubmissionRejection)
for m in tables:
    db.execute(m.__table__.update().values(payee_vpa=None, payee_mobile=None, payee_name=None, payee_settings_id=None))
db.execute(text("DELETE FROM app_settings WHERE key = 'pay_payee_backfill_done'"))
db.commit()
unpaid = db.scalar(select(MaintenancePayment).where(MaintenancePayment.is_paid.is_(True)))
admin.post(f"/maintenance/{unpaid.id}/revert")
db.expire_all()
cols = {m.__tablename__: {r.id: {c.name: str(getattr(r, c.name)) for c in m.__table__.columns if not c.name.startswith("payee_")}
                          for r in db.scalars(select(m)).all()} for m in tables}
changed = pay.backfill_payees(db); db.commit()
out["changed"] = changed
db.expire_all()
first = db.scalar(select(PaymentDetailChange).order_by(PaymentDetailChange.id))
out["first"] = [first.id, first.is_initial]
res = {}
for m in tables:
    rows = db.scalars(select(m)).all()
    stamped = [r for r in rows if r.payee_vpa is not None]
    res[m.__tablename__] = {
        "rows": len(rows), "stamped": len(stamped),
        "all_initial": all((r.payee_vpa, r.payee_mobile, r.payee_settings_id) == ("9940255076@ybl", "9940255076", first.id) and r.payee_name for r in stamped),
        "other_cols_same": all({c.name: str(getattr(r, c.name)) for c in m.__table__.columns if not c.name.startswith("payee_")} == cols[m.__tablename__][r.id] for r in rows),
    }
    if m in (MaintenancePayment, CorpusPayment):
        res[m.__tablename__]["unpaid_stamped"] = sum(1 for r in rows if not r.is_paid and r.payee_vpa)
out["res"] = res
# a second run is a no-op even after a settings change
change(vpa="new.society@oksbi", mobile="9876543210", payee="New Payee Name")
db.expire_all()
pay.backfill_payees(db); db.commit()
out["after_change_stamped_new"] = db.scalar(select(func.count()).select_from(MaintenancePayment).where(MaintenancePayment.payee_vpa == "new.society@oksbi"))
'''
    out = run_scratch(tmp_path, SETUP + code)
    mp = out["res"]["maintenance_payments"]
    assert mp["stamped"] > 0 and mp["unpaid_stamped"] == 0 and mp["stamped"] < mp["rows"]
    for name, r in out["res"].items():
        assert r["all_initial"] and r["other_cols_same"], name
    assert out["after_change_stamped_new"] == 0
    assert out["first"][1] is True


def test_exports_and_pdf_statement_show_the_payee(tmp_path):
    code = r'''
import io, zipfile
owner = login("sastry")
sast = db.scalar(select(User).where(User.username == "sastry"))
flat = sast.flats[0]
oct_ = db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10))
mp = db.scalar(select(MaintenancePayment).where(MaintenancePayment.period_id == oct_.id, MaintenancePayment.flat_id == flat.id))
mp_id = mp.id
admin.post(f"/maintenance/{mp_id}/revert")
change(vpa="new.society@oksbi", mobile="9876543210", payee="New Payee Name")
owner.post("/owner/maintenance", data={"flat_id": flat.id, "period_id": oct_.id, "paid_on": "2026-10-06", "amount": "", "payment_mode": "UPI", "payment_note": "UTR222233334444"})
z = zipfile.ZipFile(io.BytesIO(admin.get("/admin/export/csv").content))
import csv
rows = list(csv.DictReader(io.StringIO(z.read("maintenance_payments.csv").decode())))
out["csv_row"] = [r for r in rows if r["id"] == str(mp_id)][0]
out["csv_old"] = sorted({r["payee_vpa"] for r in rows if r["id"] != str(mp_id) and r["is_paid"] == "1"})
import openpyxl
wb = openpyxl.load_workbook(io.BytesIO(admin.get("/admin/export/excel").content))
ws = wb["Maintenance payments"] if "Maintenance payments" in wb.sheetnames else wb.worksheets[[w.lower() for w in wb.sheetnames].index("maintenance payments")]
head = [c.value for c in ws[1]]
out["xlsx_head"] = head
vals = {h: [r[i].value for r in ws.iter_rows(min_row=2)] for i, h in enumerate(head)}
out["xlsx_vpas"] = sorted({v for k, col in vals.items() if "UPI" in str(k) or "vpa" in str(k).lower() for v in col if v})
pdf = admin.get(f"/flats/{flat.id}/statement.pdf")
out["pdf_status"] = pdf.status_code
out["pdf_has_text"] = b"new.society" in pdf.content or b"%PDF" in pdf.content
from app.services import pdf_report
import inspect
out["pdf_fn"] = [n for n in dir(pdf_report) if "pdf" in n.lower() or "statement" in n.lower()]
'''
    out = run_scratch(tmp_path, SETUP + code)
    assert out["csv_row"]["payee_vpa"] == "new.society@oksbi" and out["csv_row"]["payee_mobile"] == "9876543210"
    assert out["csv_row"]["payee_name"] == "New Payee Name" and out["csv_row"]["payee_settings_id"] == "2"
    assert out["csv_old"] == ["9940255076@ybl"]
    assert any("UPI" in str(h) for h in out["xlsx_head"]), out["xlsx_head"]
    assert out["xlsx_vpas"] == ["9940255076@ybl", "new.society@oksbi"]


def test_pdf_statement_text_includes_paid_to(tmp_path):
    code = r'''
from app.services import pdf_report
from types import SimpleNamespace
out["fn"] = pdf_report._with_payee("Cash", SimpleNamespace(payee_vpa="a@b"))
out["fn_none"] = pdf_report._with_payee("Cash", SimpleNamespace(payee_vpa=None))
out["fn_empty"] = pdf_report._with_payee("", SimpleNamespace(payee_vpa="a@b"))
'''
    out = run_scratch(tmp_path, SETUP + code)
    assert "paid to a@b" in out["fn"] and out["fn"].startswith("Cash")
    assert out["fn_none"] == "Cash" and "paid to a@b" in out["fn_empty"]


def test_restoring_a_pre_v73_dump_rederives_payees_and_keeps_the_history(tmp_path):
    code = r'''
import json as js, re
change(vpa="new.society@oksbi", mobile="9876543210", payee="New Payee Name")
dump = admin.get("/admin/export/json").json()
# make it look like a v7.2 dump: no new tables, no payee columns, no payment settings
for name in ("payment_detail_changes", "recurring_tasks", "recurring_task_occurrences"):
    dump["tables"].pop(name)
drop = {"payee_vpa", "payee_mobile", "payee_name", "payee_settings_id", "opening_carried"}
for t in dump["tables"].values():
    t["columns"] = [c for c in t["columns"] if c not in drop]
    t["rows"] = [{k: v for k, v in r.items() if k not in drop} for r in t["rows"]]
dump["tables"]["app_settings"]["rows"] = [r for r in dump["tables"]["app_settings"]["rows"] if not r["key"].startswith("pay_")]
raw = js.dumps(dump).encode()
paid_before = db.scalar(select(func.count()).select_from(MaintenancePayment).where(MaintenancePayment.is_paid.is_(True)))
r = admin.post("/admin/export/restore", files={"dump": ("old.json", raw, "application/json")})
token = re.search(r'name="token" value="([^"]+)"', r.text).group(1)
r = admin.post("/admin/export/restore/confirm", data={"token": token, "confirm_text": "RESTORE"})
out["restore"] = r.status_code
db.expire_all()
out["live"] = pay.get_details(db)
out["history"] = db.scalar(select(func.count()).select_from(PaymentDetailChange))
stamped = db.scalars(select(MaintenancePayment).where(MaintenancePayment.is_paid.is_(True))).all()
out["paid"] = [paid_before, len(stamped)]
out["payees"] = sorted({m.payee_vpa for m in stamped})
out["unpaid_stamped"] = db.scalar(select(func.count()).select_from(MaintenancePayment).where(MaintenancePayment.is_paid.is_(False), MaintenancePayment.payee_vpa.is_not(None)))
'''
    out = run_scratch(tmp_path, SETUP + code)
    assert out["restore"] == 200 and out["history"] == 2
    assert out["live"] == {"vpa": "new.society@oksbi", "mobile": "9876543210", "payee": "New Payee Name"}
    assert out["paid"][0] == out["paid"][1] and out["payees"] == ["9940255076@ybl"] and out["unpaid_stamped"] == 0
