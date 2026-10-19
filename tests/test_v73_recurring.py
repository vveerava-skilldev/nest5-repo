"""v7.3 B: scheduled recurring admin tasks (service level with a fake clock, plus HTTP in scratch databases)."""
from datetime import date

import pytest
from sqlalchemy import func, select

from app.models import AuditLog, Expense, RecurringTask, RecurringTaskOccurrence
from app.services import recurring as rec
from tests.test_v71_empty_and_review import run_scratch
from tests.test_v73_monthly import db  # noqa: F401  (fixture: own scratch database)


def _task(db, key):
    return db.scalar(select(RecurringTask).where(RecurringTask.seed_key == key))


def _occ(db, task, y, m):
    return db.scalar(select(RecurringTaskOccurrence).where(
        RecurringTaskOccurrence.task_id == task.id, RecurringTaskOccurrence.year == y, RecurringTaskOccurrence.month == m))


# ---- seeding ------------------------------------------------------------------------------------


def test_defaults_are_seeded_once_with_the_right_wording(db):
    assert rec.ensure_defaults(db, date(2026, 10, 1)) == 2
    assert rec.ensure_defaults(db, date(2026, 10, 1)) == 0 and rec.ensure_defaults(db, date(2026, 12, 9)) == 0
    db.commit()
    salary, eb = _task(db, "watchman_salary"), _task(db, "common_eb")
    assert salary.title == "Watchman salary payment" and rec.due_label(salary) == "by 5th"
    assert eb.title == "Common EB (electricity) payment" and rec.due_label(eb) == "before 10th"
    assert salary.day_of_month == 5 and eb.day_of_month == 10 and eb.due_mode == "before"
    assert db.scalar(select(func.count()).select_from(RecurringTask)) == 2


def test_new_tasks_start_in_the_first_month_not_already_overdue(db):
    rec.ensure_defaults(db, date(2026, 10, 19))
    db.commit()
    assert (_task(db, "watchman_salary").start_year, _task(db, "watchman_salary").start_month) == (2026, 11)
    assert (_task(db, "common_eb").start_year, _task(db, "common_eb").start_month) == (2026, 11)
    rec.ensure_defaults(db, date(2026, 10, 3))  # no-op: already seeded
    rec.generate(db, date(2026, 10, 19))
    assert db.scalar(select(func.count()).select_from(RecurringTaskOccurrence)) == 0
    december = rec.first_open_month(5, date(2026, 12, 20))
    assert december == (2027, 1)


# ---- occurrences, rollover, overdue --------------------------------------------------------------


def test_generation_is_lazy_idempotent_and_rolls_over_months(db):
    rec.ensure_defaults(db, date(2026, 10, 1))
    assert rec.generate(db, date(2026, 10, 1)) == 2 and rec.generate(db, date(2026, 10, 20)) == 0
    assert rec.generate(db, date(2026, 11, 1)) == 2
    db.commit()
    salary = _task(db, "watchman_salary")
    assert _occ(db, salary, 2026, 10).due_date == date(2026, 10, 5)
    assert _occ(db, salary, 2026, 11).due_date == date(2026, 11, 5)
    assert _occ(db, _task(db, "common_eb"), 2026, 11).due_date == date(2026, 11, 10)
    rec.generate(db, date(2026, 12, 31))
    rec.generate(db, date(2027, 1, 1))  # December -> January
    assert _occ(db, salary, 2027, 1).due_date == date(2027, 1, 5)


def test_overdue_after_the_due_day_until_done_and_old_months_stay_listed(db):
    rec.ensure_defaults(db, date(2026, 10, 1))
    rec.generate(db, date(2026, 10, 1))
    db.commit()

    def state(today, key="watchman_salary"):
        rows = [r for r in rec.open_occurrences(db, today) if r["task"].seed_key == key]
        return [(r["occurrence"].month, r["status"]) for r in rows]

    assert state(date(2026, 10, 1)) == [(10, "due_soon")]   # 4 days before the 5th
    assert state(date(2026, 10, 5)) == [(10, "due_soon")]   # due today: not yet overdue
    assert state(date(2026, 10, 6)) == [(10, "overdue")]
    rec.generate(db, date(2026, 11, 1))
    assert state(date(2026, 11, 2)) == [(10, "overdue"), (11, "due_soon")]  # October is still outstanding
    assert state(date(2026, 10, 1), "common_eb") == []       # due the 10th: 9 days away, not "soon" yet
    assert state(date(2026, 10, 5), "common_eb") == [(10, "due_soon")]
    assert state(date(2026, 10, 11), "common_eb") == [(10, "overdue")]
    oct_salary = _occ(db, _task(db, "watchman_salary"), 2026, 10)
    rec.mark_done(db, oct_salary, done_on="2026-11-02")
    assert state(date(2026, 11, 2)) == [(11, "due_soon")]


def test_interval_and_short_month_clamping(db):
    task = rec.create_task(db, title="Quarterly lift check", day_of_month=31, due_mode="by",
                           interval_months=3, notes="", today=date(2026, 10, 3))
    db.commit()
    assert (task.start_year, task.start_month) == (2026, 10)
    assert [rec.applies_to(task, 2026, m) for m in (10, 11, 12)] == [True, False, False]
    assert rec.applies_to(task, 2027, 1) and not rec.applies_to(task, 2027, 2) and rec.applies_to(task, 2027, 4)
    assert rec.due_date_for(task, 2027, 2) == date(2027, 2, 28) and rec.due_date_for(task, 2028, 2) == date(2028, 2, 29)
    assert rec.generate(db, date(2026, 11, 5)) == 0 and _occ(db, task, 2026, 11) is None


def test_validation(db):
    for bad in (dict(title=" "), dict(day_of_month="0"), dict(day_of_month="32"), dict(day_of_month="x"),
                dict(interval_months="0"), dict(interval_months="13")):
        fields = dict(title="T", day_of_month="5", due_mode="by", interval_months="1", notes="")
        fields.update(bad)
        with pytest.raises(rec.RecurringError):
            rec.create_task(db, **fields)


def test_mark_done_records_date_amount_note_never_an_expense_and_undo(db):
    rec.ensure_defaults(db, date(2026, 10, 1))
    rec.generate(db, date(2026, 10, 1))
    db.commit()
    occ = _occ(db, _task(db, "watchman_salary"), 2026, 10)
    expenses = db.scalar(select(func.count()).select_from(Expense))
    rec.mark_done(db, occ, done_on="2026-10-04", amount="₹4,000", note="Paid by cash", actor="admin")
    db.commit()
    assert (occ.done_on, occ.amount, occ.note, occ.done_by) == (date(2026, 10, 4), 4000, "Paid by cash", "admin")
    assert db.scalar(select(func.count()).select_from(Expense)) == expenses
    with pytest.raises(rec.RecurringError):
        rec.mark_done(db, occ, done_on="2026-10-05")
    with pytest.raises(rec.RecurringError):
        rec.mark_done(db, _occ(db, _task(db, "common_eb"), 2026, 10), done_on="not-a-date")
    with pytest.raises(rec.RecurringError):
        rec.mark_done(db, _occ(db, _task(db, "common_eb"), 2026, 10), done_on="2026-10-05", amount="-5")
    rec.undo_done(db, occ)
    db.commit()
    assert occ.done_on is None and occ.amount is None and occ.note is None
    actions = [a.action for a in db.scalars(select(AuditLog).where(AuditLog.entity_type == "recurring_task_occurrence").order_by(AuditLog.id))]
    assert actions == ["done", "undo"]


def test_edit_moves_open_occurrences_but_not_done_ones_and_disable_hides(db):
    rec.ensure_defaults(db, date(2026, 10, 1))
    rec.generate(db, date(2026, 10, 1))
    db.commit()
    salary = _task(db, "watchman_salary")
    oct_occ = _occ(db, salary, 2026, 10)
    rec.update_task(db, salary, title="Watchman salary payment", day_of_month=7, due_mode="by", interval_months=1,
                    notes="", today=date(2026, 10, 2))
    db.commit()
    assert oct_occ.due_date == date(2026, 10, 7)
    rec.mark_done(db, oct_occ, done_on="2026-10-07")
    rec.update_task(db, salary, title="Watchman salary payment", day_of_month=9, due_mode="by", interval_months=1,
                    notes="", today=date(2026, 10, 8))
    assert oct_occ.due_date == date(2026, 10, 7)  # done occurrences keep what was recorded
    rec.set_active(db, salary, False)
    db.commit()
    assert [r for r in rec.open_occurrences(db, date(2026, 11, 20)) if r["task"].id == salary.id] == []
    assert rec.generate(db, date(2026, 11, 1)) == 1  # only the other task
    rec.set_active(db, salary, True, today=date(2026, 11, 1))
    assert _occ(db, salary, 2026, 11) is not None
    acts = [a.action for a in db.scalars(select(AuditLog).where(AuditLog.entity_type == "recurring_task").order_by(AuditLog.id))]
    assert acts.count("update") == 2 and "disable" in acts and "enable" in acts


def test_month_view_projects_future_months_only(db):
    rec.ensure_defaults(db, date(2026, 10, 1))
    rec.generate(db, date(2026, 10, 1))
    db.commit()
    today = date(2026, 10, 19)
    cur = rec.occurrences_for_month(db, 2026, 10, today)
    assert len(cur) == 2 and not any(r["projected"] for r in cur) and {r["status"] for r in cur} == {"overdue"}
    future = rec.occurrences_for_month(db, 2026, 12, today)
    assert len(future) == 2 and all(r["projected"] and r["status"] == "open" for r in future)
    assert rec.occurrences_for_month(db, 2026, 8, today) == []  # history is never invented


# ---- HTTP: calendar, actions, dashboard, admin-only, audit -------------------------------------------


def test_http_calendar_actions_dashboard_done_undo_and_access(tmp_path):
    code = r'''
import re
from sqlalchemy import func
def cal(client, m=""):
    page = client.get("/calendar" + m).text
    data = json.loads(re.search(r'application/json[^>]*>(.*?)</script>', page, re.S).group(1))
    return page, [i for i in data.values() if i["type"] == "recurring"]

page, items = cal(admin)
out["initial_recurring_items"] = len(items)           # seeded tasks start in November: nothing this month
out["admin_list"] = admin.get("/admin/recurring").text
out["actions_empty"] = "Recurring tasks due" in admin.get("/admin/actions").text
r = admin.post("/admin/recurring/tasks", data={"title": "Lift service check", "day_of_month": "22", "due_mode": "by", "interval_months": "1", "notes": ""}, follow_redirects=False)
out["add_status"] = r.status_code
db.expire_all()
t = db.scalar(select(RecurringTask).where(RecurringTask.title == "Lift service check"))
occ = db.scalar(select(RecurringTaskOccurrence).where(RecurringTaskOccurrence.task_id == t.id))
out["occ_due"] = str(occ.due_date)
page, items = cal(admin)
out["cal_item"] = [i["title"] for i in items]
out["cal_status"] = [i["status"] for i in items]
out["actions_soon"] = admin.get("/admin/actions").text
admin.post(f"/admin/recurring/tasks/{t.id}/edit", data={"title": "Lift service check", "day_of_month": "15", "due_mode": "by", "interval_months": "1", "notes": ""})
db.expire_all()
out["occ_due_after_edit"] = str(db.get(RecurringTaskOccurrence, occ.id).due_date)
out["cal_legend_admin"] = "Recurring task" in page
ownerc = login("asrk.sastry")
opage, oitems = cal(ownerc)
out["owner_items"] = len(oitems)
out["owner_legend"] = "Recurring task" in opage
out["owner_403"] = [ownerc.get("/admin/recurring").status_code, ownerc.post(f"/admin/recurring/occurrence/{occ.id}/done", data={"done_on": "2026-10-19"}).status_code,
                    ownerc.post("/admin/recurring/tasks", data={"title": "x", "day_of_month": "1"}).status_code]
out["owner_actions_403"] = ownerc.get("/admin/actions").status_code
out["actions"] = admin.get("/admin/actions").text
out["dash"] = admin.get("/").text
out["schedule"] = admin.get("/schedule").text
out["owner_schedule_has_recurring"] = "Recurring monthly tasks" in ownerc.get("/schedule").text
exp_before = db.scalar(select(func.count()).select_from(Expense))
bal_before = None
r = admin.post(f"/admin/recurring/occurrence/{occ.id}/done", data={"done_on": "2026-10-18", "amount": "1,250", "note": "Vendor paid", "back": "/admin/actions"}, follow_redirects=False)
out["done_redirect"] = r.headers["location"]
db.expire_all()
out["expenses_unchanged"] = db.scalar(select(func.count()).select_from(Expense)) == exp_before
o2 = db.get(RecurringTaskOccurrence, occ.id)
out["done_fields"] = [str(o2.done_on), o2.amount, o2.note]
out["actions_after_done"] = "Lift service check" in admin.get("/admin/actions").text
out["hub_group_gone"] = "Recurring tasks due" not in admin.get("/admin/actions").text
page, items = cal(admin)
out["cal_done"] = [(i["title"], i["status"]) for i in items]
admin.post(f"/admin/recurring/occurrence/{occ.id}/undo", data={})
db.expire_all()
out["undone"] = db.get(RecurringTaskOccurrence, occ.id).done_on is None
admin.post(f"/admin/recurring/tasks/{t.id}/edit", data={"title": "Lift service check", "day_of_month": "20", "due_mode": "before", "interval_months": "1", "notes": ""})
db.expire_all()
out["moved_due"] = str(db.get(RecurringTaskOccurrence, occ.id).due_date)
admin.post(f"/admin/recurring/tasks/{t.id}/toggle", data={})
page, items = cal(admin)
out["cal_disabled"] = len(items)
out["actions_disabled"] = "Lift service check" in admin.get("/admin/actions").text
page, items = cal(admin, "?m=2026-12")
out["dec_items"] = sorted(i["title"] for i in items)
out["dec_projected"] = [i["projected"] for i in items]
audits = db.scalars(select(AuditLog).where(AuditLog.entity_type.in_(["recurring_task", "recurring_task_occurrence"])).order_by(AuditLog.id)).all()
out["audit"] = [(a.entity_type, a.action, a.user_label) for a in audits]
out["activity"] = admin.get("/admin/activity").text
'''
    out = run_scratch(tmp_path, code)
    assert out["initial_recurring_items"] == 0 and out["add_status"] == 303
    assert "Watchman salary payment" in out["admin_list"] and "Common EB (electricity) payment" in out["admin_list"]
    assert "by 5th" in out["admin_list"] and "before 10th" in out["admin_list"]
    assert out["actions_empty"] is False
    assert out["occ_due"] == "2026-10-22" and out["occ_due_after_edit"] == "2026-10-15"
    assert "in 3 day(s)" in out["actions_soon"]
    assert out["cal_item"] == ["Lift service check: due by 22nd"] and out["cal_status"] == ["open"] and out["cal_legend_admin"]
    assert out["owner_items"] == 0 and out["owner_legend"] is False
    assert out["owner_403"] == [403, 403, 403] and out["owner_actions_403"] == 403
    assert "Recurring tasks due" in out["actions"] and "Lift service check" in out["actions"] and "overdue since 15-10-2026" in out["actions"]
    assert "Lift service check" in out["dash"]  # admin dashboard reminders (Actions card)
    assert "Recurring monthly tasks" in out["schedule"] and out["owner_schedule_has_recurring"] is False
    assert out["done_redirect"].startswith("/admin/actions?msg=")
    assert out["expenses_unchanged"] is True and out["done_fields"] == ["2026-10-18", 1250, "Vendor paid"]
    assert out["actions_after_done"] is False and out["hub_group_gone"] is True  # empty groups are hidden
    assert out["cal_done"] == [["Lift service check: done", "done"]]
    assert out["undone"] is True and out["moved_due"] == "2026-10-20"
    assert out["cal_disabled"] == 0 and out["actions_disabled"] is False
    assert out["dec_items"] == ["Common EB (electricity) payment: due before 10th", "Watchman salary payment: due by 5th"]
    assert out["dec_projected"] == [True, True]
    assert out["audit"] == [list(x) for x in [
        ("recurring_task", "create", "admin"), ("recurring_task", "update", "admin"),
        ("recurring_task_occurrence", "done", "admin"), ("recurring_task_occurrence", "undo", "admin"),
        ("recurring_task", "update", "admin"), ("recurring_task", "disable", "admin")]]
    assert "marked recurring task “Lift service check”" in out["activity"] and "added recurring task" in out["activity"]
