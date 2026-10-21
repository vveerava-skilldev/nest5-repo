"""v7.4: the admin can edit a settlement's note. Audited, no effect on amounts or balances, visible in exports."""
import re

from tests.test_v71_empty_and_review import run_scratch

CODE = r'''
import io, zipfile, csv, openpyxl
from app.services import ledger as L

st = db.scalar(select(Settlement).join(Expense).where(Expense.description == "Common expense 1"))
exp = db.get(Expense, st.expense_id)
sid, eid = st.id, st.expense_id
before = {"amount": st.amount, "on": st.settled_on.isoformat(), "rev": st.reverted_at, "settled": exp.is_settled,
          "fund": L.compute_fund_balance(db), "closings": [(p.id, p.opening_fund_balance, p.closing_fund_balance) for p in db.scalars(select(LedgerPeriod).order_by(LedgerPeriod.id)).all()],
          "note": st.note}
out["before_note"] = before["note"]
r = admin.post(f"/settlements/{sid}/note", data={"note": "  Paid by cheque 000123   to Flat 301 "}, follow_redirects=False)
out["post"] = [r.status_code, r.headers["location"]]
db.expire_all()
st = db.get(Settlement, sid); exp = db.get(Expense, eid)
out["after_note"] = st.note
out["unchanged"] = [st.amount == before["amount"], st.settled_on.isoformat() == before["on"], st.reverted_at is None, exp.is_settled,
                    L.compute_fund_balance(db) == before["fund"],
                    [(p.id, p.opening_fund_balance, p.closing_fund_balance) for p in db.scalars(select(LedgerPeriod).order_by(LedgerPeriod.id)).all()] == before["closings"]]
logs = db.scalars(select(AuditLog).where(AuditLog.entity_type == "settlement", AuditLog.action == "edit_note")).all()
out["audit"] = [[a.entity_id, a.user_label, json.loads(a.before_json)["note"], json.loads(a.after_json)["note"]] for a in logs]
out["activity"] = "edited settlement note" in admin.get("/admin/activity").text
out["audit_page"] = "edit_note" in admin.get("/audit").text
page = admin.get("/settlements").text
out["settlements_page"] = ["Paid by cheque 000123 to Flat 301" in page, f'/settlements/{sid}/note' in page, "Settlement note saved." in admin.get(r.headers["location"]).text]
epage = admin.get(f"/expenses?period_id={exp.period_id}").text
out["expenses_page"] = ["Paid by cheque 000123 to Flat 301" in epage, f'/settlements/{sid}/note' in epage]
x = openpyxl.load_workbook(io.BytesIO(admin.get("/admin/export/excel").content))
ws = x["Settlements"]
hdr = [c.value for c in ws[1]]
out["xlsx_note"] = [row[hdr.index("Note")].value for row in ws.iter_rows(min_row=2) if row[hdr.index("Note")].value == "Paid by cheque 000123 to Flat 301"]
z = zipfile.ZipFile(io.BytesIO(admin.get("/admin/export/csv").content))
name = [n for n in z.namelist() if "settlements" in n][0]
out["csv_has_note"] = "Paid by cheque 000123 to Flat 301" in z.read(name).decode("utf-8")
out["json_has_note"] = "Paid by cheque 000123 to Flat 301" in admin.get("/admin/export/json").text

n_logs = len(logs)
admin.post(f"/settlements/{sid}/note", data={"note": "Paid by cheque 000123 to Flat 301"})
db.expire_all()
out["noop_logs"] = [n_logs, len(db.scalars(select(AuditLog).where(AuditLog.action == "edit_note")).all())]
admin.post(f"/settlements/{sid}/note", data={"note": ""})
db.expire_all(); out["cleared"] = db.get(Settlement, sid).note
admin.post(f"/settlements/{sid}/note", data={"note": "x" * 400})
db.expire_all(); out["long"] = len(db.get(Settlement, sid).note)
r = admin.post(f"/settlements/{sid}/note", data={"note": "Back test", "back": "https://evil.example/x"}, follow_redirects=False)
r2 = admin.post(f"/settlements/{sid}/note", data={"note": "Back test 2", "back": "/expenses"}, follow_redirects=False)
out["back"] = [r.headers["location"].startswith("/settlements"), r2.headers["location"].startswith("/expenses?")]

for who in ("asrk.sastry", "manikyamba"):
    out[who] = login(who).post(f"/settlements/{sid}/note", data={"note": "hacked"}, follow_redirects=False).status_code
db.expire_all(); out["hacked"] = db.get(Settlement, sid).note == "hacked"

admin.post(f"/settlements/{sid}/revert")
db.expire_all()
r = admin.post(f"/settlements/{sid}/note", data={"note": "after revert"}, follow_redirects=False)
out["reverted"] = ["error=" in r.headers["location"], db.get(Settlement, sid).note != "after revert"]
out["missing"] = "error=" in admin.post("/settlements/99999/note", data={"note": "x"}, follow_redirects=False).headers["location"]
'''


def test_admin_edits_a_settlement_note(tmp_path):
    out = run_scratch(tmp_path, CODE)
    assert out["before_note"] == "Reimbursed from maintenance fund"
    assert out["post"][0] == 303 and out["post"][1].startswith("/settlements?msg=")
    assert out["after_note"] == "Paid by cheque 000123 to Flat 301"
    assert all(out["unchanged"]), out["unchanged"]
    assert out["audit"] == [[out["audit"][0][0], "admin", "Reimbursed from maintenance fund", "Paid by cheque 000123 to Flat 301"]]
    assert out["activity"] is True and out["audit_page"] is True
    assert out["settlements_page"] == [True, True, True]
    assert out["expenses_page"] == [True, True]
    assert out["xlsx_note"] == ["Paid by cheque 000123 to Flat 301"] and out["csv_has_note"] and out["json_has_note"]
    assert out["noop_logs"][0] == out["noop_logs"][1]
    assert out["cleared"] is None and out["long"] == 255
    assert out["back"] == [True, True]
    assert out["asrk.sastry"] == 403 and out["manikyamba"] == 403 and out["hacked"] is False
    assert out["reverted"] == [True, True] and out["missing"] is True
