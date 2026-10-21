"""v7.3 (in place): admin dashboard order, collapsed activity, no duplicate Settlement card, due dates on the Action board."""
import re

from tests.test_v71_empty_and_review import run_scratch

SETUP = r'''
import re
from datetime import date, timedelta
OWNER = "asrk.sastry"
owner = login(OWNER)
sast = db.scalar(select(User).where(User.username == OWNER))
oflat_id = sast.flats[0].id
by_no = {f.number: f.id for f in db.scalars(select(Flat)).all()}
sep = db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 9))
octo = db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10))
def mrow(number, period):
    return db.scalar(select(MaintenancePayment).where(MaintenancePayment.period_id == period.id, MaintenancePayment.flat_id == by_no[number]))
sep_301, oct_401 = mrow("301", sep).id, mrow("401", octo).id
admin.post(f"/maintenance/{sep_301}/revert")
admin.post(f"/maintenance/{oct_401}/revert")
admin.post("/projects", data={"name": "Roof dated", "total_cost": "25000", "due_date": "2026-09-20"})
admin.post("/projects", data={"name": "Gate undated", "total_cost": "10000"})
admin.post("/corpus/campaigns", data={"name": "Lift dated", "target_amount": "50000", "due_date": "2026-12-15"})
owner.post("/owner/expenses", data={"flat_id": oflat_id, "description": "Pump motor repair", "category": "Common", "amount": "1850", "expense_date": "2026-10-04"})
db.expire_all()
exp_id = db.scalar(select(Expense.id).where(Expense.description == "Pump motor repair"))
def dash(client=admin, period=None):
    return client.get("/" + (f"?period_id={period.id}" if period else "")).text
def board(html):
    return re.search(r'<section class="card dashboard-action">.*?</section>', html, re.S).group(0)
def table(html):
    return re.search(r'<table class="dashboard-matrix">.*?</table>', board(html), re.S).group(0)
'''


def test_admin_dashboard_order_collapsed_activity_and_no_duplicate_settlement_card(tmp_path):
    code = r'''
html = dash()
def pos(marker):
    i = html.find(marker)
    return i
marks = ["Opening balance", "Account Balance (money held)", "Action board", 'class="card alerts-panel', 'class="card dashboard-actions', 'class="card dashboard-activity']
out["positions"] = [pos(m) for m in marks]
out["sections_once"] = [html.count(m) for m in ("Action board", 'class="card alerts-panel', 'class="card dashboard-actions', 'class="card dashboard-activity')]
act = re.search(r'<details class="card dashboard-activity no-print">.*?</details>', html, re.S).group(0)
out["activity"] = [act.startswith('<details class="card dashboard-activity no-print">\n  <summary>'), "open" in act.split("<summary>")[0],
                   re.search(r"<summary>.*?Recent activity.*?<span class=\"badge\">(\d+)</span>", act, re.S).group(1),
                   len(re.findall('class="activity-item"', act)), 'href="/admin/activity"' in act]
# the redundant Settlement card is gone; the metrics, the Actions item and the Settlements page remain
out["card_gone"] = ['class="card settlement-card"' in html, "Settlements page →" in html, 'action="/settlements/' in html]
out["metrics_kept"] = ["Unsettled owner expenses" in html, "Available to settle now" in html, "Effective balance after settling" in html]
hub = html[html.find('class="card dashboard-actions'):html.find('class="card dashboard-activity')]
out["hub_settle"] = ["Unsettled owner expenses to settle" in hub, 'href="/settlements"' in hub, ">Settle →" in hub]
out["nav"] = 'href="/settlements"' in html.split("</nav>")[0]
out["settlements_page"] = [admin.get("/settlements").status_code, "Pump motor repair" in admin.get("/settlements").text]
out["actions_page"] = "Unsettled owner expenses to settle" in admin.get("/admin/actions").text
r = admin.post(f"/settlements/{exp_id}/settle", data={"settled_on": "2026-10-19", "back": "/"}, follow_redirects=False)
db.expire_all()
out["settled"] = [r.status_code, db.get(Expense, exp_id).is_settled]
out["after_settle"] = ["Unsettled owner expenses to settle" in dash(), "Unsettled owner expenses to settle" in admin.get("/admin/actions").text]

# owner dashboard keeps its old order: alerts first, board last, no collapsed activity, no due hints
o = dash(owner)
out["owner_order"] = [o.find('class="card alerts-panel') < o.find("Opening balance") < o.find("Action board"), "dashboard-activity" in o,
                      "due-hint" in o, 'class="card settlement-card"' in o]
'''
    out = run_scratch(tmp_path, SETUP + code)
    p = out["positions"]
    assert all(i >= 0 for i in p) and p == sorted(p), p
    assert out["sections_once"] == [1, 1, 1, 1]
    assert out["activity"][0] is True and out["activity"][1] is False and int(out["activity"][2]) == out["activity"][3] > 0 and out["activity"][4]
    assert out["card_gone"] == [False, False, False]
    assert out["metrics_kept"] == [True, True, True]
    assert out["hub_settle"] == [True, True, True] and out["nav"] is True
    assert out["settlements_page"] == [200, True]
    assert out["actions_page"] is True
    assert out["settled"] == [303, True] and out["after_settle"] == [False, False]
    assert out["owner_order"] == [True, False, False, False]


def test_action_board_shows_due_dates_once_under_the_column_names_not_on_rows(tmp_path):
    code = r'''
admin.post("/corpus/campaigns", data={"name": "Tank repaint", "target_amount": "30000", "due_date": "2027-01-10"})
sep_html, oct_html = dash(period=sep), dash(period=octo)
out["titles"] = [re.findall(r"Action board[^<]*", sep_html), re.findall(r"Action board[^<]*", oct_html)]
def heads(html):
    return {re.sub(r"<[^>]+>", "", th.split("<small")[0]).strip(): re.findall(r'<small class="(due-hint[^"]*)">([^<]*)</small>', th)
            for th in re.findall(r"<th[^>]*>.*?</th>", re.search(r"<thead>.*?</thead>", table(html), re.S).group(0), re.S)}
out["sep_heads"], out["oct_heads"] = heads(sep_html), heads(oct_html)
def body_and_cards(html):
    tbody = re.search(r"<tbody>.*?</tbody>", table(html), re.S).group(0)
    tfoot = re.search(r"<tfoot>.*?</tfoot>", table(html), re.S).group(0)
    cards = re.search(r'<div class="dashboard-cards">.*?</section>', board(html), re.S).group(0)
    return tbody + tfoot + cards
for name, html in (("sep", sep_html), ("oct", oct_html)):
    rows = body_and_cards(html)
    out[name + "_rows_clean"] = [("due-hint" in rows), ("overdue since" in rows), bool(re.search(r"\bdue \d", rows))]
strip = re.search(r'<p class="due-strip".*?</p>', sep_html, re.S).group(0)
out["strip"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", strip)).strip()
out["strip_once"] = sep_html.count('class="due-strip"')
out["hint_total"] = [sep_html.count('<small class="due-hint'), oct_html.count('<small class="due-hint')]
out["owner"] = ["due-hint" in dash(owner, sep), "due-strip" in dash(owner, sep)]
page = admin.get("/admin/actions").text
out["hub"] = ["due-hint" in page, "Due 20-09-2026" in page, "Overdue maintenance" in page]
'''
    out = run_scratch(tmp_path, SETUP + code)
    assert out["titles"] == [["Action board \u2014 2026-09"], ["Action board \u2014 2026-10"]]
    sep_h, oct_h = out["sep_heads"], out["oct_heads"]
    # maintenance: the 5th of the viewed month, once, overdue because a flat is still unpaid
    assert sep_h["Maintenance"] == [["due-hint overdue", "overdue since 05-09-2026"]]
    assert oct_h["Maintenance"] == [["due-hint overdue", "overdue since 05-10-2026"]]
    # corpus goals: one line per goal with a due date, grouped under the one column name; open goals are plain
    assert sep_h["Corpus"] == [["due-hint", "Lift dated: due 15-12-2026"], ["due-hint", "Tank repaint: due 10-01-2027"]] or \
        sep_h["Corpus"] == [["due-hint", "Tank repaint: due 10-01-2027"], ["due-hint", "Lift dated: due 15-12-2026"]]
    assert {t for _, t in sep_h["Corpus"]} == {"Lift dated: due 15-12-2026", "Tank repaint: due 10-01-2027"}
    # projects: per project heading; a past date with money outstanding is overdue; no date, nothing
    assert sep_h["Roof dated"] == [["due-hint overdue", "overdue since 20-09-2026"]] and oct_h["Roof dated"] == sep_h["Roof dated"]
    assert sep_h["Gate undated"] == [] and sep_h["Flat / owner"] == [] and sep_h["Pending"] == []
    # nothing about due dates on any flat row, in the totals row or in the phone cards
    assert out["sep_rows_clean"] == [False, False, False] and out["oct_rows_clean"] == [False, False, False]
    # compact phone strip: once, same facts
    assert out["strip_once"] == 1
    for bit in ("Maintenance overdue since 05-09-2026", "Lift dated due 15-12-2026", "Roof dated overdue since 20-09-2026"):
        assert bit in out["strip"], out["strip"]
    assert "Gate undated" not in out["strip"]
    assert out["hint_total"] == [8, 8]
    assert out["owner"] == [False, False]
    # the Actions list keeps its original wording; the board change did not touch it
    assert out["hub"] == [False, True, True]
