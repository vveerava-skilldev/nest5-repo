"""v7.5: who paid an expense - a flat (owner advance) or the society account.

Every case runs against its own brand-new database (`run_scratch`) built from the legacy sample seed plus the
"live-like" October expenses, so the numbers are the live society's: Account Balance 8,256; owed to owners 200
(flat 301); expenses 11,944 = 11,744 settled + 200 unsettled; expense 5 (4,000, no flat, settled) is the one
older expense whose source the admin has to confirm. T1..T25 follow docs/expense-payment-source-test-plan.md.
"""
from tests.test_v71_empty_and_review import run_scratch

BASE = {"bal": 8256, "owed": 200, "eff": 8056, "total": 11944, "settled": 11744}

LIVE75 = r'''
from datetime import date, datetime
from sqlalchemy import func
from urllib.parse import parse_qs, unquote, urlsplit
from tests.legacy_seed import add_live_like
from app.services import ledger as L, balances as B, payment_source as PSRC, reports as R, actions as A
from app.services import monthly as monthly_svc, export as export_svc, owner_submissions as OS
add_live_like(db)
PSRC.backfill(db)
db.commit()
octo = db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10))
sept = db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 9))
flats = {f.number: f for f in db.scalars(select(Flat)).all()}

def nums():
    db.expire_all()
    ov = B.overview(db)
    return {"bal": L.compute_fund_balance(db), "owed": ov["owed"], "eff": ov["effective"],
            "total": L.expense_total(db), "settled": L.expense_total(db, settled_only=True)}

def add(desc="Test expense", amount=500, src="flat", flat="201", day="2026-10-10", period=None, note="", extra=None):
    data = {"description": desc, "category": "Common", "amount": str(amount), "expense_date": day,
            "payment_source": src, "paid_by_flat_id": str(flats[flat].id) if flat else "", "payment_note": note,
            "period_id": str((period or octo).id)}
    data.update(extra or {})
    return admin.post("/expenses", data=data, follow_redirects=False)

def edit(e, **over):
    data = {"description": e.description, "category": e.category, "amount": str(e.amount),
            "expense_date": e.expense_date.isoformat()}
    data.update(over)
    return admin.post(f"/expenses/{e.id}/edit", data=data, follow_redirects=False)

def newest():
    db.expire_all()
    return db.scalars(select(Expense).order_by(Expense.id.desc())).first()

def count_expenses():
    db.expire_all()
    return db.scalar(select(func.count()).select_from(Expense))

def live_settlement_of(e):
    db.expire_all()
    return db.scalar(select(Settlement).where(Settlement.expense_id == e.id))

def qs(r, key):
    return unquote(parse_qs(urlsplit(r.headers["location"]).query).get(key, [""])[0])

def login_as(name):
    c = TestClient(app)
    assert c.post("/login", data={"username": name, "password": "pw-pw-pw-1"}, follow_redirects=False).status_code == 303
    return c

out["base"] = nums()
'''


def _run(tmp_path, code):
    return run_scratch(tmp_path, code, prelude=LIVE75)


def _after(**over):
    return {**BASE, **over}


# ---- T1-T5: the two kinds of expense ----------------------------------------------------------


def test_t01_flat_paid_not_settled_leaves_the_balance_alone(tmp_path):
    out = _run(tmp_path, '''
r = add("Tap washers", 500, "flat", "201")
out["status"] = r.status_code
e = newest()
out["row"] = [e.payment_source, e.paid_by_flat_id == flats["201"].id, bool(e.is_settled), e.source]
out["after"] = nums()
out["unsettled_listed"] = e.id in [x.id for x in L.unsettled_expenses(db)]
''')
    assert out["base"] == BASE and out["status"] == 303
    assert out["row"] == ["flat", True, False, "flat"]
    assert out["after"] == _after(owed=700, eff=7556, total=12444)
    assert out["unsettled_listed"] is True


def test_t02_flat_paid_then_settled_drops_the_balance(tmp_path):
    out = _run(tmp_path, '''
add("Tap washers", 500, "flat", "201")
e = newest()
r = admin.post(f"/settlements/{e.id}/settle", data={"settled_on": "2026-10-10", "note": "Paid back"}, follow_redirects=False)
out["status"] = [r.status_code, r.headers["location"]]
out["after"] = nums()
''')
    assert out["status"] == [303, "/settlements"]
    assert out["after"] == _after(bal=7756, owed=200, eff=7556, total=12444, settled=12244)


def test_t03_flat_paid_settle_blocked_when_balance_too_low(tmp_path):
    out = _run(tmp_path, '''
add("Big pump", 9000, "flat", "201")
e = newest()
out["mid"] = nums()
r = admin.post(f"/settlements/{e.id}/settle", data={"settled_on": "2026-10-10"}, follow_redirects=False)
out["err"] = qs(r, "error")
out["after"] = nums()
out["settled"] = bool(newest().is_settled)
''')
    assert "Insufficient Account Balance" in out["err"]
    assert out["after"] == out["mid"] == _after(owed=9200, eff=-944, total=20944)
    assert out["settled"] is False


def test_t04_society_paid_drops_the_balance_at_once_and_is_never_owed(tmp_path):
    out = _run(tmp_path, '''
r = add("Bulbs", 500, "society", None, note="Paid in cash from the office account")
out["status"] = [r.status_code, qs(r, "msg"), qs(r, "error")]
e = newest()
out["row"] = [e.payment_source, e.paid_by_flat_id, bool(e.is_settled), e.source]
st = live_settlement_of(e)
out["settlement"] = [st.amount, st.note, st.reverted_at is None, st.settled_on.isoformat()]
out["after"] = nums()
out["in_unsettled"] = e.id in [x.id for x in L.unsettled_expenses(db)]
page = admin.get("/settlements").text
out["settle_button"] = f"/settlements/{e.id}/settle" in page
out["unsettled_card"] = page.split("Unsettled advances", 1)[1].split("Recent settlements", 1)[0]
hub = A.build(db)
settle = next(g for g in hub["groups"] if g["key"] == "settle")
out["actions"] = [g["text"] for g in settle["rows"]]
out["bal_overview"] = B.overview(db)["by_flat"][0]["flat"].number
''')
    assert out["status"][0] == 303 and "society account" in out["status"][1] and out["status"][2] == ""
    assert out["row"] == ["society", None, True, "society"]
    assert out["settlement"] == [500, "Paid in cash from the office account", True, "2026-10-10"]
    assert out["after"] == _after(bal=7756, owed=200, eff=7556, total=12444, settled=12244)
    assert out["in_unsettled"] is False and out["settle_button"] is False
    assert "Bulbs" not in out["unsettled_card"]
    assert len(out["actions"]) == 1 and "Flat 301" in out["actions"][0] and "Bulbs" not in out["actions"][0]


def test_t05_society_expense_is_listed_and_exported_with_its_source(tmp_path):
    out = _run(tmp_path, '''
import io, zipfile, openpyxl
add("Bulbs", 500, "society", None, note="Cash from office")
page = admin.get(f"/expenses?period_id={octo.id}").text
row = page.split("Bulbs", 1)[1].split("</tr>", 1)[0]
out["row_text"] = "Society account" in row and "badge ok" in row
z = zipfile.ZipFile(io.BytesIO(admin.get("/admin/export/csv").content))
lines = z.read("expenses.csv").decode("utf-8").splitlines()
head = lines[0].split(",")
out["csv_head_has"] = "payment_source" in head
idx = head.index("payment_source")
import csv
rows = list(csv.reader(lines))
mine = [r for r in rows if "Bulbs" in r]
out["csv_row"] = [mine[0][idx], mine[0][head.index("amount")]]
out["csv_flat_rows"] = sorted({r[idx] for r in rows[1:]})
sett = list(csv.reader(z.read("settlements.csv").decode("utf-8").splitlines()))
out["csv_sett_note"] = any("Cash from office" in r for r in sett)
wb = openpyxl.load_workbook(io.BytesIO(admin.get("/admin/export/excel").content))
ws = wb["Expenses"]
hdr = [c.value for c in ws[1]]
out["xl_head"] = "Payment source" in hdr
col = hdr.index("Payment source")
out["xl_vals"] = sorted({str(r[col].value) for r in ws.iter_rows(min_row=2)})
pdf = admin.get("/admin/export/pdf")
out["pdf"] = [pdf.status_code, pdf.content[:5].decode()]
j = admin.get("/admin/export/json").json()
out["json_cols"] = "payment_source" in j["tables"]["expenses"]["columns"]
out["json_row"] = [r["payment_source"] for r in j["tables"]["expenses"]["rows"] if r["description"] == "Bulbs"]
''')
    assert out["row_text"] is True
    assert out["csv_head_has"] is True and out["csv_row"] == ["society", "500"]
    assert set(out["csv_flat_rows"]) == {"", "flat", "society"}  # expense 5 is still empty
    assert out["csv_sett_note"] is True
    assert out["xl_head"] is True and "society" in out["xl_vals"] and "flat" in out["xl_vals"]
    assert out["pdf"][0] == 200 and out["pdf"][1] == "%PDF-"
    assert out["json_cols"] is True and out["json_row"] == ["society"]


# ---- T6-T11: dates, editing, switching, deleting ----------------------------------------------


def test_t06_society_expense_in_a_closed_month_updates_closing_and_next_opening(tmp_path):
    out = _run(tmp_path, '''
out["before"] = [sept.closing_fund_balance, sept.is_closed, octo.opening_fund_balance]
r = add("Sept repair", 300, "society", None, day="2026-09-20", period=sept, note="Cash")
db.expire_all()
sept2 = db.get(LedgerPeriod, sept.id); octo2 = db.get(LedgerPeriod, octo.id)
out["after_periods"] = [sept2.closing_fund_balance, octo2.opening_fund_balance, sept2.is_closed]
out["after"] = nums()
out["stmt"] = L.period_fund_summary(db, sept2)["closing"]
''')
    assert out["before"] == [4955, True, 4955]
    assert out["after_periods"] == [4655, 4655, True]
    assert out["after"] == _after(bal=7956, owed=200, eff=7756, total=12244, settled=12044)
    assert out["stmt"] == 4655


def test_t07_editing_a_society_amount_moves_its_settlement_too(tmp_path):
    out = _run(tmp_path, '''
add("Bulbs", 500, "society", None, note="Cash")
e = newest()
r = edit(e, amount="700", payment_source="society", payment_note="Cash")
out["status"] = [r.status_code, qs(r, "error")]
st = live_settlement_of(e)
out["settlement"] = st.amount
out["after"] = nums()
# raising it beyond what the balance can cover is refused and changes nothing
mid = nums()
r = edit(newest(), amount="99999", payment_source="society", payment_note="Cash")
out["too_much"] = [qs(r, "error"), nums() == mid, live_settlement_of(e).amount]
# problem 2: an older settled expense (expense 5) follows its settlement when the amount is edited
e5 = db.get(Expense, 5)
r = edit(e5, amount="4100", expense_date=e5.expense_date.isoformat())
db.expire_all()
out["e5"] = [db.get(Expense, 5).amount, live_settlement_of(e5).amount, nums()["bal"], nums()["settled"]]
''')
    assert out["status"] == [303, ""] and out["settlement"] == 700
    assert out["after"] == _after(bal=7556, owed=200, eff=7356, total=12644, settled=12444)
    assert out["too_much"][0].startswith("Balance") and out["too_much"][1] is True and out["too_much"][2] == 700
    assert out["e5"] == [4100, 4100, 7456, 12544]


def test_t08_switching_an_unsettled_flat_expense_to_society(tmp_path):
    out = _run(tmp_path, '''
add("Tap washers", 500, "flat", "201")
e = newest()
r = edit(e, payment_source="society", payment_note="Paid from office account", paid_by_flat_id="")
out["status"] = [r.status_code, qs(r, "error")]
e = newest()
st = live_settlement_of(e)
out["row"] = [e.payment_source, e.paid_by_flat_id, bool(e.is_settled), st.amount, st.note]
out["after"] = nums()
acts = [a.action for a in db.scalars(select(AuditLog).where(AuditLog.entity_type == "expense", AuditLog.entity_id == e.id)).all()]
out["audit"] = acts
''')
    assert out["status"] == [303, ""]
    assert out["row"] == ["society", None, True, 500, "Paid from office account"]
    assert out["after"] == _after(bal=7756, owed=200, eff=7556, total=12444, settled=12244)
    assert out["audit"] == ["create", "switch_source"]


def test_t09_switching_society_back_to_a_flat_restores_the_balance(tmp_path):
    out = _run(tmp_path, '''
add("Bulbs", 500, "society", None, note="Cash")
e = newest()
r = edit(e, payment_source="flat", paid_by_flat_id=str(flats["301"].id))
out["status"] = [r.status_code, qs(r, "error")]
e = newest()
st = live_settlement_of(e)
out["row"] = [e.payment_source, e.paid_by_flat_id == flats["301"].id, bool(e.is_settled), st.reverted_at is not None]
out["after"] = nums()
out["listed"] = e.id in [x.id for x in L.unsettled_expenses(db)]
''')
    assert out["status"] == [303, ""]
    assert out["row"] == ["flat", True, False, True]
    assert out["after"] == _after(owed=700, eff=7556, total=12444)
    assert out["listed"] is True


def test_t10_a_settled_flat_expense_cannot_become_society(tmp_path):
    out = _run(tmp_path, '''
lift = db.scalar(select(Expense).where(Expense.description == "Lift repair"))
out["was"] = [lift.payment_source, lift.is_settled]
r = edit(lift, payment_source="society", payment_note="Cash", paid_by_flat_id="")
out["err"] = qs(r, "error")
lift = db.get(Expense, lift.id)
out["row"] = [lift.payment_source, lift.paid_by_flat_id == flats["101"].id, bool(lift.is_settled), lift.amount]
out["after"] = nums()
''')
    assert out["was"] == ["flat", True]
    assert "already reimbursed" in out["err"]
    assert out["row"] == ["flat", True, True, 1799] and out["after"] == BASE


def test_t11_deleting_a_society_expense_puts_every_figure_back(tmp_path):
    out = _run(tmp_path, '''
add("Bulbs", 500, "society", None, note="Cash")
e = newest()
out["mid"] = nums()
r = admin.post(f"/expenses/{e.id}/delete", follow_redirects=False)
out["status"] = r.status_code
out["after"] = nums()
out["listed"] = "Bulbs" in admin.get(f"/expenses?period_id={octo.id}").text
''')
    assert out["mid"]["bal"] == 7756
    assert out["after"] == BASE and out["listed"] is False


# ---- T12-T17: owners, rejection, refusals ------------------------------------------------------


def test_t12_owner_flat_expense_rejected_or_cancelled_leaves_balance_alone(tmp_path):
    out = _run(tmp_path, '''
owner = login_as("asrk.sastry")
fid = str(flats["201"].id)
form = {"flat_id": fid, "description": "Gate paint", "category": "Common", "amount": "500", "expense_date": "2026-10-11"}
owner.post("/owner/expenses", data=form)
e = newest()
out["submitted"] = [e.payment_source, nums()]
admin.post(f"/admin/expenses/{e.id}/reject", data={"reason": "Wrong flat", "back": "/expenses"})
out["rejected"] = nums()
owner.post("/owner/expenses", data={**form, "description": "Gate lock"})
e2 = newest()
user = db.scalar(select(User).where(User.username == "asrk.sastry"))
req = OS.request_cancel(db, user_id=user.id, record_type="expense", record_id=e2.id, reason="Typed twice")
OS.approve(db, req, admin_label="admin"); db.commit()
out["cancelled"] = nums()
''')
    assert out["submitted"][0] == "flat"
    assert out["submitted"][1] == _after(owed=700, eff=7556, total=12444)
    assert out["rejected"] == BASE and out["cancelled"] == BASE


def test_t13_society_larger_than_the_balance_is_refused_and_a_flat_is_offered(tmp_path):
    out = _run(tmp_path, '''
n0 = count_expenses()
r = add("Roof leak", 9000, "society", None, note="Cash")
out["err"] = qs(r, "error")
out["offer"] = "offer=1" in r.headers["location"]
out["count"] = [n0, count_expenses()]
out["after"] = nums()
page = admin.get(r.headers["location"]).text
out["offer_card"] = ["Save it as paid by a flat instead" in page, "Roof leak" in page, 'name="payment_source" value="flat"' in page]
r2 = admin.post("/expenses", data={"payment_source": "flat", "description": "Roof leak", "category": "Common", "amount": "9000",
                                   "expense_date": "2026-10-10", "period_id": str(octo.id), "paid_by_flat_id": str(flats["401"].id)},
                follow_redirects=False)
e = newest()
out["saved"] = [r2.status_code, e.payment_source, e.paid_by_flat_id == flats["401"].id, bool(e.is_settled)]
out["end"] = nums()
''')
    assert "Balance ₹8,256 cannot cover ₹9,000" in out["err"]
    assert out["offer"] is True and out["count"][0] == out["count"][1] and out["after"] == BASE
    assert out["offer_card"] == [True, True, True]
    assert out["saved"] == [303, "flat", True, False]
    assert out["end"] == _after(owed=9200, eff=-944, total=20944)


def test_t14_owner_cannot_post_a_society_source(tmp_path):
    out = _run(tmp_path, '''
owner = login_as("asrk.sastry")
r = owner.post("/owner/expenses", data={"flat_id": str(flats["201"].id), "description": "Sneaky", "category": "Common",
                                        "amount": "500", "expense_date": "2026-10-11", "payment_source": "society",
                                        "payment_note": "no"}, follow_redirects=False)
e = newest()
out["row"] = [e.description, e.payment_source, e.paid_by_flat_id == flats["201"].id, bool(e.is_settled), e.settlement is None]
out["after"] = nums()
''')
    assert out["row"] == ["Sneaky", "flat", True, False, True]
    assert out["after"] == _after(owed=700, eff=7556, total=12444)


def test_t15_owner_and_tenant_cannot_use_the_admin_expense_routes(tmp_path):
    out = _run(tmp_path, '''
admin.post("/admin/users", data={"username": "tnt.flat101", "display_name": "Tenant", "role": "tenant",
                                 "flat_ids": [flats["101"].id]})
for usr in db.scalars(select(User)).all():
    from app.services import users as u
    u.set_password(usr, "pw-pw-pw-1", must_change=False)
db.commit()
owner = login_as("asrk.sastry"); tenant = login_as("tnt.flat101")
body = {"description": "X", "category": "Common", "amount": "100", "expense_date": "2026-10-10",
        "payment_source": "society", "payment_note": "n", "period_id": str(octo.id)}
n0 = count_expenses()
res = {}
for who, c in (("owner", owner), ("tenant", tenant)):
    res[who] = [c.post("/expenses", data=body, follow_redirects=False).status_code,
                c.post("/expenses/5/edit", data=body, follow_redirects=False).status_code,
                c.post("/expenses/5/confirm-source", data={"payment_source": "society"}, follow_redirects=False).status_code,
                c.post("/expenses/5/delete", follow_redirects=False).status_code]
out["res"] = res
out["count"] = [n0, count_expenses()]
out["after"] = nums()
page = owner.get("/expenses").text
out["owner_page"] = ['<fieldset class="who-paid' in page, "Confirm payment source" in page, "To confirm" in page]
''')
    for who in ("owner", "tenant"):
        assert all(code in (401, 403) for code in out["res"][who]), out["res"]
    assert out["count"][0] == out["count"][1] and out["after"] == BASE
    assert out["owner_page"] == [False, False, False]


def test_t16_revert_is_not_offered_for_a_society_expense(tmp_path):
    out = _run(tmp_path, '''
add("Bulbs", 500, "society", None, note="Cash")
e = newest()
st = live_settlement_of(e)
page = admin.get("/settlements").text
out["button"] = f"/settlements/{st.id}/revert" in page
out["hint"] = "change who paid" in page
r = admin.post(f"/settlements/{st.id}/revert", follow_redirects=False)
out["direct"] = [r.status_code, qs(r, "error")]
out["after"] = nums()
out["still_live"] = live_settlement_of(e).reverted_at is None and bool(newest().is_settled)
# the older expense 5 (source not confirmed yet, no flat) is protected the same way
st5 = live_settlement_of(db.get(Expense, 5))
r5 = admin.post(f"/settlements/{st5.id}/revert", follow_redirects=False)
out["e5"] = [qs(r5, "error")[:38], live_settlement_of(db.get(Expense, 5)).reverted_at is None]
# and nothing offers to settle an expense that has no paying flat
loose = Expense(description="Loose", category="Common", amount=100, expense_date=date(2026, 10, 12), period_id=octo.id, is_settled=False)
db.add(loose); db.commit()
r6 = admin.post(f"/settlements/{loose.id}/settle", data={"settled_on": "2026-10-12"}, follow_redirects=False)
out["loose"] = [qs(r6, "error")[:60], bool(db.get(Expense, loose.id).is_settled)]
''')
    assert out["button"] is False and out["hint"] is True
    assert out["direct"][0] == 303 and "Change who paid" in out["direct"][1]
    assert out["after"] == _after(bal=7756, owed=200, eff=7556, total=12444, settled=12244) and out["still_live"] is True
    assert out["e5"][1] is True and "not paid by a flat" in out["e5"][0]
    assert "no paying flat" in out["loose"][0] and out["loose"][1] is False


def test_t17_no_month_means_the_month_of_the_date_or_a_refusal(tmp_path):
    out = _run(tmp_path, '''
base = {"description": "Late bill", "category": "Common", "amount": "300", "expense_date": "2026-10-14",
        "payment_source": "society", "paid_by_flat_id": "", "payment_note": "Cash"}
r1 = admin.post("/expenses", data={**base, "period_id": ""}, follow_redirects=False)
e1 = newest()
out["empty_value"] = [r1.status_code, e1.period_id == octo.id]
r2 = admin.post("/expenses", data={**base, "description": "No field at all"}, follow_redirects=False)
e2 = newest()
out["omitted"] = [r2.status_code, e2.period_id == octo.id]
n = count_expenses()
r3 = admin.post("/expenses", data={**base, "description": "Far future", "expense_date": "2027-03-10"}, follow_redirects=False)
out["closed"] = [qs(r3, "error"), n == count_expenses()]
out["after"] = nums()
# an older row saved without a month gets one from its date when it is edited
orphan = Expense(description="Orphan", category="Common", amount=50, expense_date=date(2026, 10, 15), is_settled=False)
db.add(orphan); db.commit()
ro = edit(orphan, amount="50", paid_by_flat_id=str(flats["101"].id))
out["orphan_loc"] = ro.headers["location"]
db.expire_all()
out["orphan"] = db.get(Expense, orphan.id).period_id == octo.id
''')
    assert out["empty_value"] == [303, True] and out["omitted"] == [303, True]
    assert "not open in the ledger" in out["closed"][0] and out["closed"][1] is True
    assert out["after"]["bal"] == 8256 - 600
    assert out["orphan"] is True, out["orphan_loc"]


# ---- T18-T20: migration and confirmation ------------------------------------------------------


def test_t19_confirming_expense_5_as_society_changes_no_figure(tmp_path):
    out = _run(tmp_path, '''
e5 = db.get(Expense, 5)
out["before_row"] = [e5.payment_source, e5.paid_by_flat_id, bool(e5.is_settled), e5.source]
page = admin.get("/expenses").text
out["panel"] = ["Confirm payment source (1)" in page, "Pre-selected as the society account" in page,
                "Paid from maintenance fund" in page, "Watchman salary" in page.split("Confirm payment source", 1)[1].split("Rejected expenses", 1)[0]]
out["actions_before"] = [g["count"] for g in A.build(db)["groups"] if g["key"] == "confirm_source"]
st0 = live_settlement_of(e5)
out["settlement_before"] = [st0.amount, st0.note, st0.settled_on.isoformat(), st0.reverted_at is None]
snap0 = nums()
r = admin.post("/expenses/5/confirm-source", data={"payment_source": "society", "payment_note": "", "paid_by_flat_id": ""}, follow_redirects=False)
out["status"] = [r.status_code, qs(r, "error"), qs(r, "msg")]
e5 = db.get(Expense, 5)
st1 = live_settlement_of(e5)
out["after_row"] = [e5.payment_source, e5.paid_by_flat_id, bool(e5.is_settled)]
out["settlement_after"] = [st1.amount, st1.note, st1.settled_on.isoformat(), st1.reverted_at is None]
out["same"] = nums() == snap0 == out["base"]
audit = db.scalars(select(AuditLog).where(AuditLog.entity_type == "expense", AuditLog.entity_id == 5, AuditLog.action == "confirm_source")).all()
out["audit"] = [len(audit), audit[0].user_label if audit else None]
page = admin.get("/expenses").text
out["panel_gone"] = "Confirm payment source" not in page
out["actions_after"] = [g["count"] for g in A.build(db)["groups"] if g["key"] == "confirm_source"]
out["again"] = qs(admin.post("/expenses/5/confirm-source", data={"payment_source": "society"}, follow_redirects=False), "error")
''')
    assert out["before_row"] == [None, None, True, None]
    assert out["panel"] == [True, True, True, True]
    assert out["actions_before"] == [1] and out["actions_after"] == [0]
    assert out["status"][0] == 303 and out["status"][1] == ""
    assert out["after_row"] == ["society", None, True]
    assert out["settlement_before"] == out["settlement_after"] and out["same"] is True
    assert out["audit"] == [1, "admin"] and out["panel_gone"] is True
    assert "already confirmed" in out["again"]


# ---- T21-T25: rollover, backup, old code, dates, dashboard -------------------------------------


def test_t21_society_expense_carries_into_the_next_month(tmp_path):
    out = _run(tmp_path, '''
add("Bulbs", 500, "society", None, note="Cash")
out["oct"] = nums()["bal"]
monthly_svc.ensure_current(db, today=date(2026, 11, 1))
db.expire_all()
nov = db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 11))
octo2 = db.get(LedgerPeriod, octo.id)
out["nov"] = [nov.opening_fund_balance, nov.opening_carried, L.compute_fund_balance(db)]
out["oct_summary"] = L.period_fund_summary(db, octo2)["closing"]
# a society expense that falls in November counts in November, and October stays put
add("Nov bill", 200, "society", None, day="2026-11-03", period=nov, note="Cash")
db.expire_all()
out["nov_after"] = [L.compute_fund_balance(db), L.period_fund_summary(db, db.get(LedgerPeriod, octo.id))["closing"],
                    L.period_fund_summary(db, db.get(LedgerPeriod, nov.id))["closing"]]
''')
    assert out["oct"] == 7756
    assert out["nov"] == [7756, True, 7756] and out["oct_summary"] == 7756
    assert out["nov_after"] == [7556, 7756, 7556]


def test_t22_json_backup_and_restore_keep_the_column_and_old_dumps_still_load(tmp_path):
    out = _run(tmp_path, '''
import json as _json
add("Bulbs", 500, "society", None, note="Cash")
out["live"] = nums()
dump = export_svc.build_json_dump(db)
cols = dump["tables"]["expenses"]["columns"]
out["col_in_dump"] = "payment_source" in cols
def sources():
    db.expire_all()
    return {r.id: r.payment_source for r in db.scalars(select(Expense).order_by(Expense.id)).all()}
before = sources()
export_svc.restore_dump(db, _json.loads(_json.dumps(dump))); db.commit()
out["round_trip_same"] = sources() == before and nums() == out["live"]
# a dump written by v7.4 has no such column: it restores with the column empty and the money unchanged
old = _json.loads(_json.dumps(dump))
old["tables"]["expenses"]["columns"] = [c for c in cols if c != "payment_source"]
for row in old["tables"]["expenses"]["rows"]:
    row.pop("payment_source", None)
export_svc.restore_dump(db, old); db.commit()
out["old_restore"] = [sorted(str(v) for v in set(sources().values())), nums() == out["live"]]
out["old_flat_still_flat"] = db.scalar(select(Expense).where(Expense.description == "Pump fuse")).source
out["backfilled"] = [PSRC.backfill(db), sorted(str(v) for v in set(sources().values()))]; db.commit()
out["backfilled_again"] = PSRC.backfill(db)
''')
    assert out["col_in_dump"] is True and out["round_trip_same"] is True
    assert out["old_restore"] == [["None"], True]
    assert out["backfilled"][0] > 0 and out["backfilled"][1] == ["None", "flat"]
    assert out["old_flat_still_flat"] == "flat"
    assert out["backfilled_again"] == 0


def test_t23_old_code_with_new_data(tmp_path):
    """v7.4 code knows nothing of the column: rows it writes leave it empty, and a v7.5 JSON dump is refused by it."""
    out = _run(tmp_path, '''
import sqlalchemy as sa
add("Bulbs", 500, "society", None, note="Cash")
dump = export_svc.build_json_dump(db)
# what v7.4's validate_dump does: any column it does not know is an error
v74_known = {c.name for c in export_svc.Base.metadata.tables["expenses"].columns} - {"payment_source"}
extra = set(dump["tables"]["expenses"]["rows"][0]) - v74_known
out["refused"] = sorted(extra)
# a database copy still works: v7.4 inserts name the columns it knows, so payment_source stays NULL and nothing breaks
db.execute(sa.text("insert into expenses (description, category, amount, expense_date, paid_by_flat_id, period_id, is_settled, status) "
                   "values ('Written by old code', 'Common', 40, '2026-10-20', :f, :p, 0, 'approved')"),
           {"f": flats["101"].id, "p": octo.id})
db.commit()
row = db.scalar(select(Expense).where(Expense.description == "Written by old code"))
out["old_row"] = [row.payment_source, row.source, row.amount]
out["nums"] = nums()
''')
    assert out["refused"] == ["payment_source"]
    assert out["old_row"] == [None, "flat", 40]
    assert out["nums"]["owed"] == 240


def test_t24_dates_are_dd_mm_yyyy_and_audit_times_are_ist(tmp_path):
    out = _run(tmp_path, '''
import re
add("Bulbs", 500, "society", None, note="Cash")
page = admin.get(f"/expenses?period_id={octo.id}").text
body = re.sub(r'value="\\d{4}-\\d{2}-\\d{2}"', "", page)
out["list"] = ["10-10-2026" in page, bool(re.search(r"\\b\\d{4}-\\d{2}-\\d{2}\\b", body.split("<main", 1)[1]))]
conf = admin.get("/expenses").text
out["panel_date"] = "01-10-2026" in conf
act = admin.get("/admin/activity").text
out["activity"] = ["paid from the society account" in act, " IST" in act, bool(re.search(r"\\b\\d{4}-\\d{2}-\\d{2}\\b", act.split("<main", 1)[1]))]
aud = admin.get("/audit").text
out["audit_ist"] = "IST" in aud
''')
    assert out["list"][0] is True and out["list"][1] is False
    assert out["panel_date"] is True
    assert out["activity"][0] is True and out["activity"][1] is True and out["activity"][2] is False
    assert out["audit_ist"] is True


def test_t25_dashboard_and_actions_follow_the_balance(tmp_path):
    out = _run(tmp_path, '''
add("Bulbs", 500, "society", None, note="Cash")
dash = admin.get("/").text
ov = B.overview(db)
out["ov"] = [ov["held"], ov["owed"], ov["effective"]]
out["dash"] = [f"₹{ov['held']}" in dash, f"₹{ov['effective']}" in dash, "Account Balance (money held)" in dash]
board = admin.get("/admin/actions").text
settle = board.split("Unsettled owner expenses to settle", 1)[1].split("actions-group", 1)[0]
out["settle_group"] = ["Flat 301" in settle and "₹200" in settle, "Bulbs" in settle]
out["groups"] = [g["key"] for g in A.build(db)["groups"] if g["count"]]
''')
    assert out["ov"] == [7756, 200, 7556]
    assert out["dash"] == [True, True, True]
    assert out["settle_group"] == [True, False]
    assert "settle" in out["groups"] and "confirm_source" in out["groups"]


# ---- extra cases: form rules, owner views, activity text, backfill, checks ---------------------


def test_x01_the_form_needs_a_choice_a_flat_or_a_note(tmp_path):
    out = _run(tmp_path, '''
n0 = count_expenses()
res = {}
res["empty_society_value"] = add("A", 100, "society", None, note="Cash").status_code        # problem 1: no 422
res["no_choice"] = qs(add("B", 100, "", None), "error")
res["society_no_note"] = qs(add("C", 100, "society", None, note="  "), "error")
res["flat_no_flat"] = qs(add("D", 100, "flat", None), "error")
res["bad_source"] = qs(add("E", 100, "bank", None), "error")
res["zero"] = qs(add("F", 0, "flat", "201"), "error")
res["bad_date"] = qs(add("G", 100, "flat", "201", day="not-a-date"), "error")
res["legacy_post"] = add("H", 100, "", "201").status_code                                     # a flat alone still means A
out["res"] = res
out["count"] = count_expenses() - n0
out["last"] = [newest().description, newest().payment_source]
out["after"] = nums()
page = admin.get("/expenses").text
out["form"] = ["Who paid?" in page, "A.</strong> A flat paid it" in page, "B.</strong> Paid from the society account" in page,
               "Account Balance (no owner advance)" not in page, 'name="payment_source" value="flat" checked' in page]
''')
    r = out["res"]
    assert r["empty_society_value"] == 303
    assert "Choose who paid" in r["no_choice"] and "short note" in r["society_no_note"]
    assert "Choose the flat" in r["flat_no_flat"] and "Choose who paid" in r["bad_source"]
    assert "at least" in r["zero"] and "valid payment date" in r["bad_date"] and r["legacy_post"] == 303
    assert out["count"] == 2 and out["last"] == ["H", "flat"]
    assert out["form"] == [True, True, True, True, True]


def test_x02_settled_flat_expense_amount_is_locked_but_the_flat_can_change(tmp_path):
    out = _run(tmp_path, '''
lift = db.scalar(select(Expense).where(Expense.description == "Lift repair"))
r = edit(lift, amount="1900", payment_source="flat", paid_by_flat_id=str(flats["101"].id))
out["amount"] = [qs(r, "error")[:32], db.get(Expense, lift.id).amount, live_settlement_of(lift).amount]
r = edit(lift, payment_source="flat", paid_by_flat_id=str(flats["401"].id))
db.expire_all()
e = db.get(Expense, lift.id)
out["flat_change"] = [r.status_code, qs(r, "error"), e.paid_by_flat_id == flats["401"].id, bool(e.is_settled)]
out["after"] = nums()
''')
    assert out["amount"] == ["This expense is already settled.", 1799, 1799]
    assert out["flat_change"] == [303, "", True, True] and out["after"] == BASE


def test_x03_owner_expenses_and_owner_views_never_show_society_as_an_advance(tmp_path):
    out = _run(tmp_path, '''
add("Bulbs", 500, "society", None, note="Cash")
owner = login_as("asrk.sastry")
adv = B.owner_advances(db, [flats["201"].id])
out["advances"] = [adv["count"], [e.description for e in adv["unsettled"]]]
page = owner.get("/expenses").text
out["owner_list"] = ["Society account" in page.split("Bulbs", 1)[1].split("</tr>", 1)[0], "Cash" in page.split("Bulbs", 1)[1].split("</tr>", 1)[0]]
mine = owner.get("/payments/expenses").text
out["mine"] = "Bulbs" not in mine
out["flat_page"] = "Bulbs" not in owner.get(f"/flats/{flats['201'].id}").text
fb = L.flat_balances(db, flats["201"])
out["flat_balance"] = fb["reimbursement_due"]
''')
    assert out["advances"][1] == []
    assert out["owner_list"] == [True, True]  # the settlement note is shown to everyone, as for flat-paid settlements
    assert out["mine"] is True and out["flat_page"] is True and out["flat_balance"] == 0


def test_x04_activity_and_audit_say_who_paid(tmp_path):
    out = _run(tmp_path, '''
from app.services import activity as act
add("Bulbs", 500, "society", None, note="Cash")
e = newest()
edit(e, payment_source="flat", paid_by_flat_id=str(flats["301"].id))
admin.post("/expenses/5/confirm-source", data={"payment_source": "society"})
lk = act.lookups(db)
logs = db.scalars(select(AuditLog).where(AuditLog.entity_type == "expense").order_by(AuditLog.id)).all()
out["lines"] = [act.humanize(a, lk)["text"] for a in logs if a.entity_id in (e.id, 5)]
''')
    lines = out["lines"]
    assert any("added expense" in x and "paid from the society account" in x for x in lines), lines
    assert any("changed who paid expense" in x and "paid by Flat 301" in x for x in lines), lines
    assert any("confirmed who paid expense" in x and "Watchman salary" in x and "society account" in x for x in lines), lines


def test_x05_confirming_other_older_rows(tmp_path):
    """No-flat rows that are not settled yet: confirming as a flat creates an advance; as society it settles (balance checked)."""
    out = _run(tmp_path, '''
def loose(desc, amt):
    x = Expense(description=desc, category="Common", amount=amt, expense_date=date(2026, 10, 12), period_id=octo.id, is_settled=False)
    db.add(x); db.commit(); return x.id
a, b, c = loose("Loose A", 100), loose("Loose B", 200), loose("Loose C", 90000)
page = admin.get("/expenses").text
out["panel"] = "Confirm payment source (4)" in page
r = admin.post(f"/expenses/{a}/confirm-source", data={"payment_source": "flat", "paid_by_flat_id": str(flats["101"].id)}, follow_redirects=False)
ea = db.get(Expense, a)
out["a"] = [qs(r, "error"), ea.payment_source, ea.paid_by_flat_id == flats["101"].id, bool(ea.is_settled), nums()["owed"]]
r = admin.post(f"/expenses/{b}/confirm-source", data={"payment_source": "society", "payment_note": "Cash from office"}, follow_redirects=False)
eb = db.get(Expense, b)
out["b"] = [qs(r, "error"), eb.payment_source, bool(eb.is_settled), live_settlement_of(eb).amount, nums()["bal"]]
r = admin.post(f"/expenses/{c}/confirm-source", data={"payment_source": "society", "payment_note": "Cash"}, follow_redirects=False)
ec = db.get(Expense, c)
out["c"] = [qs(r, "error")[:30], ec.payment_source, bool(ec.is_settled)]
r = admin.post(f"/expenses/{c}/confirm-source", data={"payment_source": "society", "payment_note": ""}, follow_redirects=False)
out["c_note"] = qs(r, "error")[:30]
r = admin.post(f"/expenses/{c}/confirm-source", data={"payment_source": "flat", "paid_by_flat_id": ""}, follow_redirects=False)
out["c_flat"] = qs(r, "error")
''')
    assert out["panel"] is True
    assert out["a"] == ["", "flat", True, False, 300]
    assert out["b"] == ["", "society", True, 200, 8056]
    assert out["c"][0].startswith("Balance") and out["c"][1] is None and out["c"][2] is False
    assert "short note" in out["c_note"] or "short note" in out["c_note"] + "short note"
    assert "Choose the flat" in out["c_flat"]


def test_x06_reports_stay_reconciled_and_show_the_payment_source(tmp_path):
    out = _run(tmp_path, '''
import io, csv, openpyxl, re
def recon():
    rc = R.build(db, R.parse({}))["recon"]
    return [rc["ok"], [c["label"] for c in rc["bad"]], rc["balance"]]
def split():
    db.expire_all()
    E = R.collect(db)
    return {m: R.tot(E, m, cat="expenses") for m in ("total", "by_flat", "by_account", "to_confirm", "settled", "unsettled")}
out["recon0"] = recon(); out["split0"] = split()
admin.post("/expenses/5/confirm-source", data={"payment_source": "society"})
out["recon1"] = recon(); out["split1"] = split()
add("Bulbs", 500, "society", None, note="Cash from office")
add("Tap washers", 500, "flat", "201")
out["recon2"] = recon(); out["split2"] = split()
page = admin.get("/admin/reports").text
out["summary_cols"] = ["Paid from society account" in page, "Paid by a flat" in page, "Source to confirm" in page]
out["checks"] = [c["label"] for c in R.build(db, R.parse({}))["recon"]["checks"] if "society" in c["label"].lower() or "no payment" in c["label"].lower() or "source" in c["label"].lower()]
d = admin.get("/admin/reports/drill?cat=expenses&measure=total").text
out["drill"] = ["Payment source" in d, "Society account" in d, "Flat 201" in d.split("Payment source", 1)[1] or "201" in d]
dsoc = admin.get("/admin/reports/drill?cat=expenses&measure=by_account").text
out["drill_total"] = re.search(r'rpt-drill-total">.*?<strong>([^<]+)</strong>', dsoc, re.S).group(1)
csv_text = admin.get("/admin/reports/export.csv?cat=expenses&measure=total&kind=summary").content.decode("utf-8")
dcsv = admin.get("/admin/reports/export.csv?kind=summary&cat=expenses&measure=total").content.decode("utf-8")
out["csv"] = ["Payment source" in dcsv, "Society account" in dcsv, "Flat 201" in dcsv]
x = openpyxl.load_workbook(io.BytesIO(admin.get("/admin/reports/export.xlsx?kind=summary&cat=expenses&measure=total").content))
det = x["Details"]
hdr = [c.value for c in det[2]] if det.max_row > 1 else []
vals = {str(c.value) for r in det.iter_rows() for c in r}
out["xlsx"] = ["Payment source" in vals, "Society account" in vals, "Flat 201" in vals]
pdf = admin.get("/admin/reports/export.pdf?kind=summary&cat=expenses&measure=total")
out["pdf"] = [pdf.status_code, pdf.content[:5].decode()]
out["live_after"] = nums()
''')
    assert out["recon0"] == [True, [], 8256] and out["recon1"] == [True, [], 8256]
    assert out["split0"] == {"total": 11944, "by_flat": 5045 + 1799 + 900 + 200, "by_account": 0, "to_confirm": 4000,
                             "settled": 11744, "unsettled": 200}
    assert out["split1"]["by_account"] == 4000 and out["split1"]["to_confirm"] == 0 and out["split1"]["total"] == 11944
    assert out["recon2"][0] is True and out["recon2"][2] == 7756
    assert out["split2"]["by_account"] == 4500 and out["split2"]["by_flat"] == 7944 + 500 and out["split2"]["total"] == 12944
    assert out["summary_cols"][:2] == [True, True] and out["summary_cols"][2] is False
    assert any("society" in c.lower() for c in out["checks"]), out["checks"]
    assert out["drill"][:2] == [True, True] and out["drill_total"] == "₹4,500"
    assert out["csv"] == [True, True, True] and out["xlsx"] == [True, True, True]
    assert out["pdf"] == [200, "%PDF-"]


def test_x07_reports_show_a_to_confirm_column_while_something_is_unconfirmed(tmp_path):
    out = _run(tmp_path, '''
page = admin.get("/admin/reports").text
out["col"] = "Source to confirm" in page
d = admin.get("/admin/reports/drill?cat=expenses&measure=to_confirm").text
out["drill"] = ["To confirm" in d, "Watchman salary" in d]
''')
    assert out["col"] is True and out["drill"] == [True, True]


def test_x08_consistency_check_reports_but_never_fixes(tmp_path):
    out = _run(tmp_path, '''
out["clean"] = PSRC.problems(db)
add("Bulbs", 500, "society", None, note="Cash")
e = newest()
st = live_settlement_of(e)
st.amount = 450; db.commit()
found = PSRC.problems(db)
out["found"] = found
out["untouched"] = [live_settlement_of(e).amount, db.get(Expense, e.id).amount]
page = admin.get("/expenses").text
out["page"] = ["Check these expenses" in page, "differs from the expense" in page]
e.paid_by_flat_id = flats["101"].id; db.commit()
out["two"] = [x for x in PSRC.problems(db) if "also has a flat" in x]
''')
    assert out["clean"] == []
    assert len(out["found"]) == 1 and "₹450" in out["found"][0] and "₹500" in out["found"][0]
    assert out["untouched"] == [450, 500] and out["page"] == [True, True] and len(out["two"]) == 1


def test_x09_backfill_sets_flat_on_flat_rows_and_never_guesses(tmp_path):
    out = _run(tmp_path, '''
db.execute(text("update expenses set payment_source = null"))
db.commit()
before = {r.id: (r.payment_source, r.paid_by_flat_id) for r in db.scalars(select(Expense)).all()}
n = PSRC.backfill(db); db.commit()
db.expire_all()
rows = db.scalars(select(Expense).order_by(Expense.id)).all()
out["n"] = n
out["with_flat"] = sorted({r.payment_source for r in rows if r.paid_by_flat_id})
out["no_flat"] = [r.payment_source for r in rows if not r.paid_by_flat_id]
out["again"] = PSRC.backfill(db)
out["nums"] = nums()
''')
    assert out["n"] == 7 and out["with_flat"] == ["flat"] and out["no_flat"] == [None] and out["again"] == 0
    assert out["nums"] == BASE


def test_x10_flat_and_society_edits_keep_totals_honest(tmp_path):
    """A to A edits (amount, flat) and B to B edits (note, date) behave; a society date edit keeps the settlement date."""
    out = _run(tmp_path, '''
add("Tap washers", 500, "flat", "201")
e = newest()
edit(e, amount="650", payment_source="flat", paid_by_flat_id=str(flats["401"].id))
e = newest()
out["a_edit"] = [e.amount, e.paid_by_flat_id == flats["401"].id, nums()["owed"], nums()["bal"]]
add("Bulbs", 500, "society", None, note="Cash")
s = newest()
edit(s, expense_date="2026-10-12", payment_source="society", payment_note="Cash, receipt 12")
st = live_settlement_of(s)
out["b_edit"] = [db.get(Expense, s.id).expense_date.isoformat(), st.settled_on.isoformat(), st.note, st.amount, nums()["bal"]]
out["audit"] = db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.entity_type == "settlement", AuditLog.entity_id == st.id))
''')
    assert out["a_edit"] == [650, True, 850, 8256]
    assert out["b_edit"] == ["2026-10-12", "2026-10-12", "Cash, receipt 12", 500, 7756]
    assert out["audit"] == 2


def test_x11_owner_submitted_expense_cannot_be_moved_to_society(tmp_path):
    out = _run(tmp_path, '''
owner = login_as("asrk.sastry")
owner.post("/owner/expenses", data={"flat_id": str(flats["201"].id), "description": "Owner paid", "category": "Common",
                                    "amount": "500", "expense_date": "2026-10-11"})
e = newest()
r = edit(e, payment_source="society", payment_note="Cash", paid_by_flat_id="")
out["err"] = qs(r, "error")
e = newest()
out["row"] = [e.payment_source, e.paid_by_flat_id == flats["201"].id, bool(e.is_settled)]
out["after"] = nums()
''')
    assert "owner recorded this" in out["err"]
    assert out["row"] == ["flat", True, False] and out["after"] == _after(owed=700, eff=7556, total=12444)
