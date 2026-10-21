"""v7.4: admin-only Reports page (consolidated summary, monthly trend, category detail, drilldown, exports).

Each test runs against its own brand-new database (`run_scratch`), built from the legacy sample seed plus the
"live-like" October expenses: Account Balance 8,256; expenses 11,944 = 11,744 settled + 200 owed to flat 301.
"""
import json
import re

from tests.test_v71_empty_and_review import run_scratch

LIVE = r'''
import csv, io, re, openpyxl
from datetime import date, datetime
from tests.legacy_seed import add_live_like
from app.services import reports as R, ledger as L, balances as B, corpus_campaigns as CC, projects as PS
from app.config import APP_VERSION

add_live_like(db)
octo = db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10))
sept = db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 9))
flats = db.scalars(select(Flat).order_by(Flat.number)).all()
by_no = {f.number: f for f in flats}

camp = CorpusCampaign(name="Lift fund", target_amount=50000, due_date=date(2026, 12, 15)); db.add(camp); db.flush()
for f, amt in zip(flats, CC.equal_split(50000, len(flats))):
    db.add(CorpusCampaignShare(campaign_id=camp.id, flat_id=f.id, share_amount=amt))
li1 = CorpusLineItem(campaign_id=camp.id, title="Motor", target_amount=30000)
li2 = CorpusLineItem(campaign_id=camp.id, title="Wiring", target_amount=20000)
db.add_all([li1, li2]); db.flush()
db.add(CorpusPayment(flat_id=flats[0].id, campaign_id=camp.id, amount=4000, is_paid=True, paid_on=date(2026, 10, 8),
                     payment_mode="UPI", payment_note="UTR111", line_item_id=li1.id, period_id=octo.id))
db.add(CorpusPayment(flat_id=flats[1].id, campaign_id=camp.id, amount=2500, is_paid=True, paid_on=date(2026, 10, 9),
                     payment_mode="Cash", line_item_id=li2.id, period_id=octo.id))
db.commit()
admin.post("/projects", data={"name": "Roof", "total_cost": "25000", "start_date": "2026-09-01", "due_date": "2026-11-20"})
admin.post("/projects", data={"name": "Gate", "total_cost": "10000", "start_date": "2026-09-01", "due_date": "2026-12-20"})
db.expire_all()
roof = db.scalar(select(Project).where(Project.name == "Roof")); gate = db.scalar(select(Project).where(Project.name == "Gate"))
for proj, fl, amt, day in ((roof, flats[0], 1000, 6), (roof, flats[0], 500, 7), (roof, flats[2], 2000, 8), (gate, flats[1], 700, 9)):
    pp = db.scalar(select(ProjectPayment).where(ProjectPayment.project_id == proj.id, ProjectPayment.flat_id == fl.id))
    PS.record(db, pp, amt, date(2026, 10, day), payment_mode="UPI", note="UTR55")
db.commit()

LINK = re.compile(r'<a href="(/admin/reports/drill\?[^"]+)" class="rpt-link">([^<]+)</a>')
TOTAL = re.compile(r'rpt-drill-total">.*?<strong>([^<]+)</strong>', re.S)

def drill_check(client, url):
    page = client.get(url).text
    bad, n = [], 0
    for href, shown in LINK.findall(page):
        href = href.replace("&amp;", "&")
        d = client.get(href)
        m = TOTAL.search(d.text)
        n += 1
        if d.status_code != 200 or not m or m.group(1) != shown:
            bad.append([href, shown, m.group(1) if m else None, d.status_code])
    return bad, n

def tiles(params):
    data = R.build(db, R.parse(params))
    return {f"{b['id']}|{t['label']}": t["v"] for b in data["blocks"] for t in b["tiles"]}, data

def money(n):
    from app.services.activity import inr
    return inr(n)
'''


def test_totals_tie_to_dashboard_balances_and_flat_statements(tmp_path):
    out = run_scratch(tmp_path, r'''
t, data = tiles({})
rc = data["recon"]
out["t"] = t
out["rc"] = [rc["ok"], rc["balance"], rc["owed"], rc["effective"], [c["label"] for c in rc["bad"]], rc["warnings"]]
ov = B.overview(db)
out["ledger"] = [L.compute_fund_balance(db), ov["held"], ov["owed"], L.expense_total(db), L.expense_total(db, settled_only=True),
                 L.maintenance_collected(db), L.settlements_from_fund(db)]
fb = [L.flat_balances(db, f) for f in flats]
out["fb"] = [sum(x["owed_maintenance"] for x in fb), sum(x["owed_corpus"] for x in fb), sum(x["owed_projects"] for x in fb),
             sum(x["reimbursement_due"] for x in fb)]
out["camp"] = sum(CC.campaign_summary(db, c)["paid_total"] for c in db.scalars(select(CorpusCampaign)).all())
out["projects"] = sum(PS.totals(list(p.payments))["collected"] for p in db.scalars(select(Project)).all())
dash = admin.get("/").text
out["dash"] = bool(re.search(r"Account Balance \(money held\).*?₹8256", dash, re.S))
out["page"] = "₹8,256" in admin.get("/admin/reports").text
per_flat = {}
for f in flats:
    tf, _ = tiles({"flat": str(f.id)})
    x = L.flat_balances(db, f)
    per_flat[f.number] = [tf["maintenance|Pending"] == x["owed_maintenance"], tf["corpus|Pending"] == x["owed_corpus"],
                          tf["projects|Pending"] == x["owed_projects"], tf["expenses|Owed to owners"] == x["reimbursement_due"]]
out["per_flat"] = per_flat
''', prelude=LIVE)
    t = out["t"]
    assert (t["expenses|Total"], t["expenses|Settled"], t["expenses|Unsettled"], t["expenses|Owed to owners"]) == (11944, 11744, 200, 200)
    assert t["settlements|Paid out"] == 11744
    assert out["rc"][:4] == [True, 8256, 200, 8056], out["rc"]
    assert out["rc"][4] == [] and out["rc"][5] == []
    assert out["ledger"] == [8256, 8256, 200, 11944, 11744, 20000, 11744]
    assert t["maintenance|Collected"] == 20000 and t["maintenance|Billed"] == 20000 and t["maintenance|Pending"] == 0
    assert t["corpus|Collected"] == out["camp"] == 6500 and t["projects|Collected"] == out["projects"] == 4200
    assert [t["maintenance|Pending"], t["corpus|Pending"], t["projects|Pending"], t["expenses|Owed to owners"]] == out["fb"]
    assert t["corpus|Pending"] == 50000 - 6500 and t["projects|Pending"] == 35000 - 4200
    assert out["dash"] is True and out["page"] is True
    assert all(all(v) for v in out["per_flat"].values()), out["per_flat"]


def test_every_clicked_number_equals_its_drilldown_footer(tmp_path):
    out = run_scratch(tmp_path, r'''
urls = ["/admin/reports", "/admin/reports?include=1", "/admin/reports?kind=trend", "/admin/reports?kind=trend&cum=1",
        "/admin/reports?scope=fy&fy=2026", "/admin/reports?scope=months&from=2026-10&to=2026-10&include=1",
        "/admin/reports?flat=%d" % by_no["301"].id, "/admin/reports?flat=%d&include=1" % by_no["101"].id]
urls += [f"/admin/reports?kind=detail&cat={c}&include=1" for c in R.CATS]
res = {}
for u_ in urls:
    res[u_] = drill_check(admin, u_)
out["res"] = res
''', prelude=LIVE)
    total = 0
    for url, (bad, n) in out["res"].items():
        assert bad == [], (url, bad[:3])
        total += n
    assert total > 150


def test_trend_months_sum_to_the_consolidated_totals(tmp_path):
    out = run_scratch(tmp_path, r'''
res = []
for params in ({}, {"scope": "fy", "fy": "2026"}, {"scope": "months", "from": "2026-10", "to": "2026-10"},
               {"scope": "months", "from": "2026-09", "to": "2026-09"}, {"flat": str(by_no["301"].id)}):
    t, _ = tiles(params)
    tr = R.build(db, R.parse({**params, "kind": "trend"}))["trend"]
    cum = R.build(db, R.parse({**params, "kind": "trend", "cum": "1"}))["trend"]
    sums = {k: sum(r["vals"][k] for r in tr["rows"]) for k, _l in tr["cols"]}
    res.append({
        "params": params, "months": [r["label"] for r in tr["rows"]],
        "ok": [sums["m_billed"] == t["maintenance|Billed"], sums["m_collected"] == t["maintenance|Collected"],
               sums["m_pending"] == t["maintenance|Pending"], sums["corpus"] == t["corpus|Collected"],
               sums["projects"] == t["projects|Collected"], sums["expenses"] == t["expenses|Total"],
               sums["paid_out"] == t["settlements|Paid out"],
               cum["totals"]["expenses"] == t["expenses|Total"], cum["totals"]["m_collected"] == t["maintenance|Collected"]],
        "cum_monotonic": all(a["vals"]["expenses"] <= b["vals"]["expenses"] for a, b in zip(cum["rows"], cum["rows"][1:])),
        "exp": t["expenses|Total"],
    })
out["res"] = res
out["svg"] = admin.get("/admin/reports?kind=trend").text.count("<svg")
out["svg_cum"] = "running totals" in admin.get("/admin/reports?kind=trend&cum=1").text
''', prelude=LIVE)
    for r in out["res"]:
        assert all(r["ok"]), r
        assert r["cum_monotonic"]
    assert out["res"][0]["months"] == ["Sep 2026", "Oct 2026"] and out["res"][2]["months"] == ["Oct 2026"]
    assert out["res"][3]["exp"] == 5045 and out["res"][2]["exp"] == 6899
    assert out["svg"] >= 1 and out["svg_cum"] is True


def test_reconciliation_ties_and_warns_when_forced_to_mismatch(tmp_path):
    out = run_scratch(tmp_path, r'''
good = admin.get("/admin/reports").text
out["good"] = ["Ties out." in good, "Check needed" in good, "rpt-tick ok" in good]
s = db.get(LedgerPeriod, sept.id); s.closing_fund_balance = 1234; db.commit()
db.expire_all()
rc = R.build(db, R.parse({}))["recon"]
bad = admin.get("/admin/reports").text
out["bad"] = [rc["ok"], [c["label"] for c in rc["bad"]], "Check needed" in bad, "Ties out." in bad, "differs" in bad or "does not add up" in bad]
s = db.get(LedgerPeriod, sept.id); s.closing_fund_balance = 4955; db.commit()
o = db.get(LedgerPeriod, octo.id); o.opening_fund_balance = 5000; db.commit()
db.expire_all()
rc2 = R.build(db, R.parse({}))["recon"]
out["chain"] = [rc2["ok"], [c["label"] for c in rc2["bad"]]]
''', prelude=LIVE)
    assert out["good"] == [True, False, True]
    assert out["bad"][0] is False and out["bad"][2] is True and out["bad"][3] is False and out["bad"][4] is True
    assert any("Sep 2026" in x for x in out["bad"][1])
    assert out["chain"][0] is False and any("opening = Sep 2026 closing" in x for x in out["chain"][1])


def test_rejected_cancelled_reverted_are_excluded_then_shown_only_when_toggled(tmp_path):
    out = run_scratch(tmp_path, r'''
from datetime import datetime
exps = {e.description: e for e in db.scalars(select(Expense)).all()}
base, _ = tiles({})
rejected = exps["Garden tools"]; rejected.status = "rejected"; rejected.rejection_reason = "duplicate"; rejected.rejected_at = datetime.utcnow()
cancelled = exps["Pump fuse"]; cancelled.deleted_at = datetime.utcnow()
rev = exps["Lift repair"].settlement; rev.reverted_at = datetime.utcnow(); exps["Lift repair"].is_settled = False
mp = db.scalar(select(MaintenancePayment).where(MaintenancePayment.period_id == octo.id, MaintenancePayment.flat_id == by_no["501"].id))
mp.is_paid = False; mp.paid_on = None; mp.reverted_at = datetime.utcnow()
db.add(SubmissionRejection(record_type="project_contribution", record_id=None, flat_id=by_no["201"].id, summary="Project Roof — Flat 201 — ₹55",
                           amount=55, paid_on=date(2026, 10, 6), reason="not received", rejected_by="admin"))
db.commit(); db.expire_all()
after, data = tiles({})
out["base"], out["after"] = base, after
out["ledger_total"] = L.expense_total(db)
out["recon_ok"] = data["recon"]["ok"]
off = admin.get("/admin/reports").text
on = admin.get("/admin/reports?include=1").text
out["off_has_block"] = "Not counted (shown separately)" in off
out["on_has_block"] = "Not counted (shown separately)" in on
ex = R.build(db, R.parse({"include": "1"}))["blocks"][-1]["tables"][0]["rows"]
out["excluded_rows"] = [[c["v"] for c in r] for r in ex]
dt = admin.get("/admin/reports/drill?cat=expenses&measure=total").text
dt_inc = admin.get("/admin/reports/drill?cat=expenses&measure=total&include=1").text
out["drill_off"] = ["Garden tools" in dt, "Pump fuse" in dt]
out["drill_on"] = ["Garden tools" in dt_inc, "Pump fuse" in dt_inc, "Not counted" in dt_inc]
out["drill_totals"] = [TOTAL.search(dt).group(1), TOTAL.search(dt_inc).group(1)]
xd = admin.get("/admin/reports/drill?cat=expenses&measure=excluded&reason=rejected").text
out["x_rej"] = [TOTAL.search(xd).group(1), "duplicate" in xd]
out["links_ok"] = [drill_check(admin, "/admin/reports?include=1")[0], drill_check(admin, "/admin/reports")[0]]
''', prelude=LIVE)
    b, a = out["base"], out["after"]
    # settled owner advance lift repair reverted -> unsettled; rejected / cancelled expenses leave every total
    assert b["expenses|Total"] == 11944 and a["expenses|Total"] == 11944 - 900 - 200
    assert a["expenses|Total"] == out["ledger_total"] == 10844
    assert a["expenses|Settled"] == 11744 - 900 - 1799 and a["expenses|Unsettled"] == 1799
    assert a["expenses|Owed to owners"] == 1799
    assert a["maintenance|Pending"] == 2000 and a["maintenance|Collected"] == 20000 - 2000
    assert a["settlements|Paid out"] == 11744 - 900 - 1799
    assert out["recon_ok"] is True
    assert out["off_has_block"] is False and out["on_has_block"] is True
    reasons = {(r[0], r[1]): r for r in out["excluded_rows"]}
    assert reasons[("Expenses", "Rejected")][3] == 900 and reasons[("Expenses", "Cancelled")][3] == 200
    assert reasons[("Settlements", "Reverted")][3] == 1799 and reasons[("Maintenance", "Reverted")][3] == 2000
    assert reasons[("Projects", "Rejected")][3] == 55
    assert out["drill_off"] == [False, False] and out["drill_on"] == [True, True, True]
    assert out["drill_totals"][0] == out["drill_totals"][1] == "₹10,844"
    assert out["x_rej"] == ["₹900", True]
    assert out["links_ok"] == [[], []]


def test_closed_month_and_sub_goal_and_project_totals(tmp_path):
    out = run_scratch(tmp_path, r'''
t, data = tiles({"scope": "months", "from": "2026-09", "to": "2026-09"})
m = data["recon"]["months"]
out["sept"] = [t["maintenance|Collected"], t["expenses|Total"], t["settlements|Paid out"], [(x["label"], x["opening"], x["collected"], x["paid_out"], x["closing"], x["closed"], x["ok"]) for x in m]]
full = R.build(db, R.parse({}))
blocks = {b["id"]: b for b in full["blocks"]}
corpus_rows = [[c["v"] for c in r] for r in blocks["corpus"]["tables"][0]["rows"]]
corpus_total = [c["v"] for c in blocks["corpus"]["tables"][0]["total"]]
proj_rows = [[c["v"] for c in r] for r in blocks["projects"]["tables"][0]["rows"]]
proj_total = [c["v"] for c in blocks["projects"]["tables"][0]["total"]]
out["corpus"] = [corpus_rows, corpus_total]
out["projects"] = [proj_rows, proj_total]
out["proj_sum"] = [sum(r[3] for r in proj_rows), sum(r[4] for r in proj_rows), sum(r[5] for r in proj_rows)]
out["proj_ledger"] = [sum(p.amount for p in db.scalars(select(ProjectPayment)).all()),
                      sum(PS.totals(list(p.payments))["collected"] for p in db.scalars(select(Project)).all()),
                      sum(PS.totals(list(p.payments))["pending"] for p in db.scalars(select(Project)).all())]
out["roof_flat"] = [[c["v"] for c in r] for r in R.build(db, R.parse({"kind": "detail", "cat": "projects"}))["detail_tables"][-1]["rows"]]
out["detail_page"] = admin.get("/admin/reports?kind=detail&cat=corpus").text
''', prelude=LIVE)
    s = out["sept"]
    assert s[0] == 10000 and s[1] == 5045 and s[2] == 5045
    assert s[3] == [["Sep 2026", 0, 10000, 5045, 4955, True, True]]
    rows, total = out["corpus"]
    assert rows[0][0] == "Lift fund" and rows[0][1] == 50000 and rows[0][2] == 50000 and rows[0][3] == 6500 and rows[0][4] == 43500
    assert [r[0].strip() for r in rows[1:]] == ["- Motor", "- Wiring"] and rows[1][3] == 4000 and rows[2][3] == 2500
    assert total[3] == 6500 and total[4] == 43500
    prows, ptotal = out["projects"]
    names = {r[0]: r for r in prows}
    assert names["Roof"][1] == 25000 and names["Roof"][4] == 3500 and names["Gate"][4] == 700
    assert ptotal[3] == out["proj_sum"][0] and ptotal[4] == out["proj_sum"][1] == 4200 and ptotal[5] == out["proj_sum"][2]
    assert out["proj_ledger"][1:] == [ptotal[4], ptotal[5]]
    assert out["roof_flat"], "per-flat project breakdown present"
    assert "Motor" in out["detail_page"] and "Wiring" in out["detail_page"]


def test_exports_match_the_screen_and_each_export_is_audited(tmp_path):
    out = run_scratch(tmp_path, r'''
t, data = tiles({})
csv_resp = admin.get("/admin/reports/export.csv")
rows = list(csv.reader(io.StringIO(csv_resp.content.decode("utf-8-sig"))))
out["csv"] = {"head": rows[:8], "flat": [c for r in rows for c in r]}
x = admin.get("/admin/reports/export.xlsx?include=1")
wb = openpyxl.load_workbook(io.BytesIO(x.content))
out["sheets"] = wb.sheetnames
params = {r[0].value: r[1].value for r in wb["Parameters"].iter_rows(min_row=2) if r[0].value}
out["params"] = params
summ = [[c.value for c in r] for r in wb["Summary"].iter_rows()]
out["summary_has"] = {k: any(len(r) > 2 and r[1] == k.split("|")[1] and r[0] == k.split("|")[0].title() and r[2] == v for r in summ) for k, v in t.items()}
dx = admin.get("/admin/reports/export.xlsx?cat=settlements&measure=paidout")
dwb = openpyxl.load_workbook(io.BytesIO(dx.content))
det = list(dwb["Details"].iter_rows())
header = [c.value for c in det[1]]
body = [r for r in det[2:] if r[0].value]
amt_i, date_i, note_i = header.index("Amount"), header.index("Date"), header.index("Note")
data_rows = [r for r in body if r[0].value != "Total"]
out["details"] = {"title": det[0][0].value, "n": len(data_rows), "sum": sum(r[amt_i].value for r in data_rows),
                  "total": [r[amt_i].value for r in body if r[0].value == "Total"],
                  "dates": [isinstance(r[date_i].value, (date, datetime)) for r in data_rows],
                  "fmt": sorted({r[date_i].number_format for r in data_rows}), "notes": sorted({r[note_i].value or "" for r in data_rows})}
page = admin.get("/admin/reports/drill?cat=settlements&measure=paidout").text
out["screen_total"] = TOTAL.search(page).group(1)
out["screen_rows"] = page.count("<tr class=\"\">")
pdf = admin.get("/admin/reports/export.pdf")
out["pdf"] = [pdf.status_code, pdf.content[:5].decode(), len(pdf.content) > 1500, pdf.headers["content-type"]]
db.expire_all()
logs = [a for a in db.scalars(select(AuditLog).where(AuditLog.entity_type == "report")).all()]
out["audit"] = [[a.action, a.user_label, json.loads(a.after_json)] for a in logs]
act = admin.get("/admin/activity").text
out["activity"] = "exported the" in act
iso = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
out["csv_iso"] = bool(iso.search(csv_resp.content.decode("utf-8")))
''', prelude=LIVE)
    flat = out["csv"]["flat"]
    assert out["csv"]["head"][0][0].endswith("Consolidated summary")
    labels = [r[0] for r in out["csv"]["head"]]
    assert "Period" in labels and "Generated at" in labels and "App version" in labels and "Generated by" in labels
    assert "11944" in flat and "11744" in flat and "20000" in flat and "Reconciliation" in flat
    assert out["csv_iso"] is False
    assert out["sheets"] == ["Parameters", "Summary", "Trend", "Details"]
    p = out["params"]
    assert p["App version"] == "nest5-v7.5" and p["Generated by"] == "admin" and p["Generated at"].endswith(" IST")
    assert p["Period"] == "All time" and p["Flat"] == "All flats" and "Shown separately" in p["Rejected / cancelled / reverted"]
    assert all(out["summary_has"].values()), out["summary_has"]
    d = out["details"]
    assert d["title"].startswith("Settlements - Paid out")
    assert d["total"] == [11744] and d["sum"] == 11744 and d["n"] == out["screen_rows"] == 7
    assert all(d["dates"]) and d["fmt"] == ["dd-mm-yyyy"]
    assert "Paid back to owner" in d["notes"] and "Reimbursed from maintenance fund" in d["notes"]
    assert out["screen_total"] == "₹11,744"
    assert out["pdf"] == [200, "%PDF-", True, "application/pdf"]
    kinds = [(a[0], a[1], a[2]["format"]) for a in out["audit"]]
    assert kinds == [("export", "admin", "csv"), ("export", "admin", "xlsx"), ("export", "admin", "xlsx"), ("export", "admin", "pdf")]
    assert out["audit"][0][2]["report"] == "Consolidated summary" and "Period: All time" in out["audit"][0][2]["filters"]
    assert out["audit"][1][2]["filters"].count("Shown separately") == 1 and out["audit"][2][2]["report"].startswith("Settlements - Paid out")
    assert out["activity"] is True


def test_settlement_notes_show_in_the_drilldown(tmp_path):
    out = run_scratch(tmp_path, r'''
page = admin.get("/admin/reports/drill?cat=settlements&measure=paidout").text
ex = admin.get("/admin/reports/drill?cat=expenses&measure=settled").text
out["notes"] = ["Paid back to owner" in page, "Paid from maintenance fund" in page, "Paid back to owner" in ex]
st = db.scalar(select(Settlement).join(Expense).where(Expense.description == "Lift repair"))
r = admin.post(f"/settlements/{st.id}/note", data={"note": "Cheque 000123 to Flat 101"}, follow_redirects=False)
out["post"] = r.status_code
page2 = admin.get("/admin/reports/drill?cat=settlements&measure=paidout").text
out["after"] = ["Cheque 000123 to Flat 101" in page2, "Paid back to owner" in page2, TOTAL.search(page2).group(1)]
''', prelude=LIVE)
    assert out["notes"] == [True, True, True]
    assert out["post"] == 303 and out["after"] == [True, True, "₹11,744"]


def test_reports_are_admin_only(tmp_path):
    out = run_scratch(tmp_path, r'''
from fastapi.testclient import TestClient
tflat = db.scalar(select(Flat).where(Flat.number == "101"))
ten = User(username="ten1", display_name="Tenant", role="tenant", is_active=True, must_change_password=False, password_hash="x")
ten.flats = [tflat]; db.add(ten); db.commit(); u.set_password(ten, "pw-pw-pw-1", must_change=False); db.commit()
urls = ["/admin/reports", "/admin/reports?kind=trend", "/admin/reports/drill?cat=expenses&measure=total",
        "/admin/reports/export.csv", "/admin/reports/export.xlsx", "/admin/reports/export.pdf"]
res = {}
for who in ("asrk.sastry", "manikyamba", "ten1"):
    c = login(who)
    res[who] = [c.get(x, follow_redirects=False).status_code for x in urls]
    res[who + "_nav"] = 'href="/admin/reports"' in c.get("/").text
anon = TestClient(app)
res["anon"] = [anon.get(x, follow_redirects=False).status_code for x in urls]
res["admin"] = [admin.get(x, follow_redirects=False).status_code for x in urls]
res["admin_nav"] = 'href="/admin/reports"' in admin.get("/").text
db.expire_all()
res["audit"] = db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.entity_type == "report"))
out["res"] = res
''', prelude="from sqlalchemy import func\n")
    r = out["res"]
    for who in ("asrk.sastry", "manikyamba", "ten1"):
        assert r[who] == [403] * 6, (who, r[who])
        assert r[who + "_nav"] is False
    assert all(code in (302, 303, 401) for code in r["anon"]), r["anon"]
    assert r["admin"] == [200] * 6 and r["admin_nav"] is True
    assert r["audit"] == 3


def test_empty_states_on_a_fresh_install_and_for_empty_filters(tmp_path):
    out = run_scratch(tmp_path, r'''
import re
out["counts"] = [db.scalar(select(func.count()).select_from(Expense)), db.scalar(select(func.count()).select_from(Settlement)),
                 db.scalar(select(func.count()).select_from(LedgerPeriod))]
pages = {}
for url in ("/admin/reports", "/admin/reports?include=1", "/admin/reports?kind=trend", "/admin/reports?kind=detail&cat=expenses&scope=fy&fy=2019",
            "/admin/reports?kind=detail&cat=corpus", "/admin/reports?scope=fy&fy=2019", "/admin/reports?kind=trend&scope=fy&fy=2019",
            "/admin/reports/drill?cat=expenses&measure=total", "/admin/reports/export.csv", "/admin/reports/export.xlsx",
            "/admin/reports/export.pdf"):
    r = admin.get(url)
    pages[url] = [r.status_code, r.text[:20000] if "html" in r.headers["content-type"] else ""]
out["pages"] = pages
''', prelude="from sqlalchemy import func\n", legacy=False)
    for url, (status, _text) in out["pages"].items():
        assert status == 200, url
    assert out["counts"][:2] == [0, 0]
    assert "No ledger month falls inside this period" in out["pages"]["/admin/reports?scope=fy&fy=2019"][1]
    assert "No months to show" in out["pages"]["/admin/reports?kind=trend&scope=fy&fy=2019"][1]
    assert "No entries behind this number" in out["pages"]["/admin/reports/drill?cat=expenses&measure=total"][1]
    assert "Nothing to show here" in out["pages"]["/admin/reports?kind=detail&cat=expenses&scope=fy&fy=2019"][1]
    assert "₹0" in out["pages"]["/admin/reports"][1]
    assert "Check needed" not in out["pages"]["/admin/reports"][1]


def test_flat_filter_fy_and_month_range_controls(tmp_path):
    out = run_scratch(tmp_path, r'''
f301 = by_no["301"].id
t_all, _ = tiles({})
t_flat, _ = tiles({"flat": str(f301)})
t_fy25, d25 = tiles({"scope": "fy", "fy": "2025"})
t_fy26, _ = tiles({"scope": "fy", "fy": "2026"})
t_oct, _ = tiles({"scope": "months", "from": "2026-10", "to": "2026-10"})
t_swap, _ = tiles({"scope": "months", "from": "2026-10", "to": "2026-09"})
out["flat"] = [t_flat["maintenance|Collected"], t_flat["expenses|Total"], t_flat["expenses|Owed to owners"]]
out["flat_exp"] = sum(e.amount for e in db.scalars(select(Expense).where(Expense.paid_by_flat_id == f301, Expense.counted())).all())
out["fy25"] = [t_fy25["maintenance|Collected"], t_fy25["expenses|Total"]]
out["fy26"] = [t_fy26["maintenance|Collected"] == t_all["maintenance|Collected"], t_fy26["expenses|Total"] == t_all["expenses|Total"]]
out["oct"] = [t_oct["maintenance|Collected"], t_oct["expenses|Total"]]
out["swap_equal_all"] = t_swap["maintenance|Collected"] == t_all["maintenance|Collected"]
page = admin.get("/admin/reports?flat=%d&scope=fy&fy=2026" % f301).text
out["page"] = ["Flat 301" in page, "FY 2026-27" in page]
''', prelude=LIVE)
    assert out["flat"][0] == 2000 * 2 and out["flat"][1] == out["flat_exp"] and out["flat"][2] == 200
    assert out["fy25"] == [0, 0]
    assert out["fy26"] == [True, True]
    assert out["oct"] == [10000, 6899]
    assert out["swap_equal_all"] is True
    assert out["page"] == [True, True]
