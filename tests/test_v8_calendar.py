"""v8: in-portal Calendar (service, owner visibility, admin-only events) and its migration."""
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import (
    Asset,
    AssetMaintenanceLog,
    AssetMaintenanceSchedule,
    AuditLog,
    CalendarEvent,
    Flat,
    LedgerPeriod,
    MaintenancePayment,
    ScheduleFrequency,
)
from app.services import portal_calendar as cal
from app.services import schedule as schedule_svc
from tests.conftest import OWNER_PASSWORD, make_client
from tests.test_v7_simple_dashboard import _flat_id, _goal, _project

ROOT = Path(__file__).resolve().parents[1]
F = ScheduleFrequency


def _task(next_due, frequency=F.MONTHLY, days=None):
    return SimpleNamespace(next_due_on=next_due, frequency=frequency, custom_interval_days=days)


def _data(page: str) -> dict:
    raw = re.search(r'<script type="application/json" id="cal-data">(.*?)</script>', page, re.S).group(1)
    return json.loads(raw.replace("<\\/", "</"))


def _month_key(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def _purge_events(db):
    for ev in db.scalars(select(CalendarEvent)).all():
        db.delete(ev)
    db.commit()


def _add_event(admin_client, **over):
    data = {"title": "Owners meeting", "event_type": "Meeting", "event_date": date.today().isoformat(),
            "visible_to_owners": "1"}
    data.update(over)
    return admin_client.post("/calendar/events", data=data, follow_redirects=False)


# ---- recurrence + overdue (pure) ------------------------------------------------------------------


def test_monthly_task_projects_into_later_months_but_not_the_past():
    today = date(2026, 10, 12)
    task = _task(date(2026, 10, 20))
    assert cal.task_occurrences(task, date(2026, 10, 1), date(2026, 10, 31), today) == [(date(2026, 10, 20), "open", False)]
    assert cal.task_occurrences(task, date(2026, 11, 1), date(2026, 11, 30), today) == [(date(2026, 11, 20), "open", True)]
    assert cal.task_occurrences(task, date(2026, 12, 1), date(2026, 12, 31), today) == [(date(2026, 12, 20), "open", True)]
    assert cal.task_occurrences(task, date(2026, 9, 1), date(2026, 9, 30), today) == []  # history comes from logs


def test_overdue_task_is_flagged_and_not_repeated_in_the_past():
    today = date(2026, 10, 12)
    task = _task(date(2026, 10, 3), F.WEEKLY)
    got = cal.task_occurrences(task, date(2026, 10, 1), date(2026, 10, 31), today)
    assert got[0] == (date(2026, 10, 3), "overdue", False)
    assert [d for d, _, _ in got[1:]] == [date(2026, 10, 17), date(2026, 10, 24), date(2026, 10, 31)]
    assert all(status == "open" and projected for _, status, projected in got[1:])


def test_daily_weekly_custom_quarterly_yearly():
    today = date(2026, 10, 12)
    month = (date(2026, 10, 1), date(2026, 10, 31))
    daily = cal.task_occurrences(_task(date(2026, 10, 12), F.DAILY), *month, today)
    assert len(daily) == 20 and daily[0][0] == date(2026, 10, 12) and daily[-1][0] == date(2026, 10, 31)
    weekly = cal.task_occurrences(_task(date(2026, 10, 5), F.WEEKLY), *month, today)
    assert [d.day for d, _, _ in weekly] == [5, 12, 19, 26]
    assert weekly[0][1] == "overdue" and weekly[1][1] == "open"  # 12th is today: not overdue
    custom = cal.task_occurrences(_task(date(2026, 10, 14), F.CUSTOM, 10), *month, today)
    assert [d.day for d, _, _ in custom] == [14, 24]
    quarterly = cal.task_occurrences(_task(date(2026, 10, 15), F.QUARTERLY), date(2027, 1, 1), date(2027, 1, 31), today)
    assert quarterly == [(date(2027, 1, 15), "open", True)]
    assert cal.task_occurrences(_task(date(2026, 10, 15), F.QUARTERLY), date(2026, 11, 1), date(2026, 12, 31), today) == []
    yearly = cal.task_occurrences(_task(date(2026, 10, 15), F.YEARLY), date(2027, 10, 1), date(2027, 10, 31), today)
    assert yearly == [(date(2027, 10, 15), "open", True)]


def test_no_next_due_means_no_occurrence():
    assert cal.task_occurrences(_task(None), date(2026, 10, 1), date(2026, 10, 31), date(2026, 10, 12)) == []


def test_month_helpers():
    assert cal.parse_month("2026-02") == (2026, 2)
    today = date.today()
    for bad in ("", "x", "2026-13", "1999-01", "2026", None):
        assert cal.parse_month(bad) == (today.year, today.month)
    assert cal.shift_month(2026, 12, 1) == (2027, 1) and cal.shift_month(2026, 1, -1) == (2025, 12)
    assert cal.month_bounds(2028, 2) == (date(2028, 2, 1), date(2028, 2, 29))


# ---- asset items from the real schedule + log ------------------------------------------------------


def test_asset_tasks_and_completed_logs_in_month_items(seeded_db):
    today = date.today()
    asset = schedule_svc.get_or_create_asset(seeded_db, "Cal test pump")
    task = AssetMaintenanceSchedule(asset_id=asset.id, asset="Cal test pump", task="Greasing", frequency=F.WEEKLY,
                                    next_due_on=today - timedelta(days=2), last_done_on=today - timedelta(days=9))
    seeded_db.add(task)
    seeded_db.flush()
    log = AssetMaintenanceLog(asset_id=asset.id, schedule_id=task.id, done_on=today, done_by="Ravi", cost=500, notes="ok")
    seeded_db.add(log)
    seeded_db.commit()
    try:
        admin = cal.Viewer(is_admin=True)
        owner = cal.Viewer(is_admin=False, flat_ids={_flat_id(seeded_db, "102")})
        items = cal.month_items(seeded_db, today.year, today.month, admin, today)
        mine = [i for i in items if "Cal test pump" in i["title"]]
        tasks = [i for i in mine if i["type"] == "task"]
        done = [i for i in mine if i["type"] == "done"]
        assert tasks and done
        overdue = [i for i in tasks if i["status"] == "overdue"]
        if (today - timedelta(days=2)).month == today.month:
            assert len(overdue) == 1 and overdue[0]["date"] == today - timedelta(days=2) and not overdue[0]["projected"]
        assert all(i["status"] == "open" and i["projected"] for i in tasks if i["status"] != "overdue")
        assert done[0]["status"] == "done" and done[0]["date"] == today and any("Ravi" in l for l in done[0]["lines"])
        assert any("Cost" in l for l in done[0]["lines"])
        o_done = [i for i in cal.month_items(seeded_db, today.year, today.month, owner, today)
                  if i["type"] == "done" and "Cal test pump" in i["title"]]
        assert o_done and not any("Cost" in l for l in o_done[0]["lines"])  # owners don't see costs
        assert o_done[0]["link"] == f"/assets/{asset.id}"
    finally:
        seeded_db.delete(log)
        seeded_db.flush()
        seeded_db.delete(task)
        seeded_db.flush()
        seeded_db.delete(asset)
        seeded_db.commit()


# ---- page: both roles ----------------------------------------------------------------------------


def test_nav_item_and_page_for_admin_and_owner(seeded_db, admin_client, owner_client):
    for client in (admin_client, owner_client):
        page = client.get("/calendar")
        assert page.status_code == 200
        assert '<a href="/calendar" class="active" aria-current="page">Calendar</a>' in page.text
        assert client.get("/").text.count('href="/calendar"') >= 1
    assert "Add event" in admin_client.get("/calendar").text
    assert "Add event" not in owner_client.get("/calendar").text


def test_navigation_month_picker_today_and_summary(seeded_db, admin_client):
    today = date.today()
    page = admin_client.get(f"/calendar?m={_month_key(today)}").text
    assert f'href="/calendar?m={cal.month_key(*cal.shift_month(today.year, today.month, -1))}"' in page
    assert f'href="/calendar?m={cal.month_key(*cal.shift_month(today.year, today.month, 1))}"' in page
    assert 'class="cal-today on"' in page and 'name="m"' in page and "This month:" in page
    other = admin_client.get("/calendar?m=2027-03").text
    assert "March 2027" in other and 'class="cal-today"' in other and 'value="2027-03" selected' in other
    assert f"{today.month:02d}-{today.year}" in page  # month picker labels are mm-yyyy
    assert admin_client.get("/calendar?m=garbage").status_code == 200  # falls back to this month
    for a, b in (("legend", "cal-filter"), ("agenda", "cal-agenda"), ("grid", "cal-grid")):
        assert b in page, a
    for key in cal.TYPES:
        assert f'data-type="{key}"' in page


def test_dates_are_dd_mm_yyyy_and_agenda_groups_by_date(seeded_db, admin_client):
    _purge_events(seeded_db)
    day = date.today()
    _add_event(admin_client, title="Agenda A", event_date=day.isoformat())
    _add_event(admin_client, title="Agenda B", event_date=day.isoformat(), start_time="10:00")
    page = admin_client.get("/calendar").text
    agenda = page[page.index('class="cal-agenda"'):]
    assert f"{day:%d-%m-%Y}" in agenda
    section = agenda[agenda.index(f"{day:%d-%m-%Y}"):]
    assert section.index("Agenda A") < section.index("</section>") and section.index("Agenda B") < section.index("</section>")
    assert agenda.count("<h3>") >= 1
    assert re.search(r"\d{4}-\d{2}-\d{2}", re.sub(r"<[^>]+>", "", re.sub(r"<script.*?</script>", "", agenda, flags=re.S))) is None
    assert _data(page)[f"event-{seeded_db.scalar(select(CalendarEvent.id).where(CalendarEvent.title == 'Agenda B'))}"]["date"] == f"{day:%d-%m-%Y}"
    _purge_events(seeded_db)


def test_no_ics_or_subscription_endpoints(seeded_db, admin_client):
    for url in ("/calendar.ics", "/calendar/feed.ics", "/calendar/ics", "/calendar/subscribe"):
        assert admin_client.get(url).status_code == 404


# ---- maintenance / corpus / projects: owner visibility ------------------------------------------------


def _period(db, today):
    return db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == today.year, LedgerPeriod.month == today.month))


def _set_paid(db, flat_no, paid, today):
    period = _period(db, today)
    mp = db.scalar(select(MaintenancePayment).where(MaintenancePayment.period_id == period.id,
                                                    MaintenancePayment.flat_id == _flat_id(db, flat_no)))
    mp.is_paid, mp.paid_on = paid, (today if paid else None)
    db.commit()


def test_maintenance_item_owner_sees_only_own_flats(seeded_db, admin_client, owner_client):
    today = date.today()
    if _period(seeded_db, today) is None:
        pytest.skip("current month not opened in the seed")
    _set_paid(seeded_db, "102", False, today)
    _set_paid(seeded_db, "301", False, today)
    due = date(today.year, today.month, 5)
    mine = [i for i in _data(owner_client.get("/calendar").text).values() if i["type"] == "maintenance"]
    assert [i["title"] for i in mine] == ["Flat 102 maintenance pending"]
    assert mine[0]["date"] == f"{due:%d-%m-%Y}"
    page = owner_client.get("/calendar").text
    assert "Flat 301" not in page and "Flat 101" not in page and "301 (" not in page
    _set_paid(seeded_db, "102", True, today)
    again = [i for i in _data(owner_client.get("/calendar").text).values() if i["type"] == "maintenance"]
    assert again[0]["title"] == "Flat 102 maintenance paid" and again[0]["status"] == "paid"
    adm = [i for i in _data(admin_client.get("/calendar").text).values() if i["type"] == "maintenance"]
    assert len(adm) == 1 and "pending" in adm[0]["title"] and any("301" in l for l in adm[0]["lines"])


def test_past_unpaid_maintenance_is_overdue(seeded_db):
    today = date.today()
    prev_y, prev_m = cal.shift_month(today.year, today.month, -1)
    period = seeded_db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == prev_y, LedgerPeriod.month == prev_m))
    if period is None:
        pytest.skip("previous month not in the seed")
    mp = seeded_db.scalar(select(MaintenancePayment).where(MaintenancePayment.period_id == period.id,
                                                           MaintenancePayment.flat_id == _flat_id(seeded_db, "102")))
    saved = (mp.is_paid, mp.paid_on)
    mp.is_paid, mp.paid_on = False, None
    seeded_db.commit()
    try:
        owner = cal.Viewer(is_admin=False, flat_ids={mp.flat_id})
        item = [i for i in cal.month_items(seeded_db, prev_y, prev_m, owner, today) if i["type"] == "maintenance"][0]
        assert item["status"] == "overdue"
    finally:
        mp.is_paid, mp.paid_on = saved
        seeded_db.commit()


def test_corpus_and_project_due_dates_without_leaking_other_flats(seeded_db, admin_client, owner_client):
    today = date.today()
    due = today.isoformat()
    r = admin_client.post("/corpus/campaigns", data={"name": "Cal lift fund", "target_amount": "50000", "due_date": due},
                          follow_redirects=False)
    assert r.status_code == 303
    r = admin_client.post("/projects", data={"name": "Cal roof", "total_cost": "25000", "due_date": due}, follow_redirects=False)
    assert r.status_code == 303
    own = _flat_id(seeded_db, "102")
    seeded_db.expire_all()
    from app.models import CorpusCampaign
    goal = seeded_db.scalar(select(CorpusCampaign).where(CorpusCampaign.name == "Cal lift fund"))
    owner_items = _data(owner_client.get("/calendar").text)
    corpus = next(i for i in owner_items.values() if i["type"] == "corpus" and "Cal lift fund" in i["title"])
    project = next(i for i in owner_items.values() if i["type"] == "project" and "Cal roof" in i["title"])
    for item in (corpus, project):
        text = " ".join(item["lines"])
        assert "Flat 102" in text and "remaining" in text
        for other in ("101", "301", "401", "501"):
            assert f"Flat {other}" not in text and f"{other} (" not in text
        assert item["status"] in ("pending",) and item["date"] == f"{today:%d-%m-%Y}"
    assert corpus["link"] == f"/corpus/goals/{goal.id}"
    adm = _data(admin_client.get("/calendar").text)
    admin_corpus = next(i for i in adm.values() if i["type"] == "corpus" and "Cal lift fund" in i["title"])
    assert any("101" in l and "301" in l for l in admin_corpus["lines"])  # building-wide pending list
    # A past due date with money outstanding is overdue.
    goal.due_date = today - timedelta(days=1)
    seeded_db.commit()
    start, end = cal.month_bounds((today - timedelta(days=1)).year, (today - timedelta(days=1)).month)
    items = cal.month_items(seeded_db, start.year, start.month, cal.Viewer(is_admin=False, flat_ids={own}), today)
    assert [i["status"] for i in items if i["type"] == "corpus" and "Cal lift fund" in i["title"]] == ["overdue"]


def test_owner_without_share_in_a_goal_does_not_see_it(seeded_db, admin_client, owner_client):
    from app.models import CorpusCampaignShare, CorpusCampaign

    today = date.today()
    admin_client.post("/corpus/campaigns", data={"name": "Cal no-share goal", "target_amount": "10000",
                                                 "due_date": today.isoformat()})
    seeded_db.expire_all()
    goal = seeded_db.scalar(select(CorpusCampaign).where(CorpusCampaign.name == "Cal no-share goal"))
    for share in seeded_db.scalars(select(CorpusCampaignShare).where(CorpusCampaignShare.campaign_id == goal.id)).all():
        if share.flat_id == _flat_id(seeded_db, "102"):
            share.share_amount = 0
    seeded_db.commit()
    page = owner_client.get("/calendar").text
    assert "Cal no-share goal" not in page


# ---- custom events: admin CRUD, owner RBAC -------------------------------------------------------------


def test_admin_creates_edits_deletes_event_with_audit(seeded_db, admin_client):
    _purge_events(seeded_db)
    day = date.today()
    form = admin_client.get("/calendar/events/new")
    assert form.status_code == 200 and 'name="title"' in form.text and 'name="visible_to_owners"' in form.text
    assert 'value="1" checked' in form.text  # visible to owners by default
    for t in cal.EVENT_TYPES:
        assert f"<option" in form.text and t in form.text
    r = _add_event(admin_client, title="AGM 2026", event_type="AGM", event_date=day.isoformat(),
                   end_date=(day + timedelta(days=1)).isoformat(), start_time="18:30", end_time="20:00", notes="Terrace")
    assert r.status_code == 303 and r.headers["location"].startswith("/calendar?m=")
    ev = seeded_db.scalar(select(CalendarEvent).where(CalendarEvent.title == "AGM 2026"))
    ev_id = ev.id
    assert ev.event_type == "AGM" and ev.end_date == day + timedelta(days=1) and ev.start_time == "18:30"
    assert ev.visible_to_owners is True and ev.created_by == "admin"
    log = seeded_db.scalars(select(AuditLog).where(AuditLog.entity_type == "calendar_event", AuditLog.action == "create")).all()[-1]
    assert log.entity_id == ev.id and "AGM 2026" in log.after_json

    edit = admin_client.get(f"/calendar/events/{ev.id}/edit")
    assert edit.status_code == 200 and "AGM 2026" in edit.text and f"/calendar/events/{ev.id}/delete" in edit.text
    r = admin_client.post(f"/calendar/events/{ev.id}", data={"title": "AGM 2026 (moved)", "event_type": "Meeting",
                          "event_date": day.isoformat(), "notes": "Hall"}, follow_redirects=False)
    assert r.status_code == 303
    seeded_db.expire_all()
    ev = seeded_db.get(CalendarEvent, ev.id)
    assert ev.title == "AGM 2026 (moved)" and ev.end_date is None and ev.start_time is None
    assert ev.visible_to_owners is False  # checkbox left empty
    log = seeded_db.scalars(select(AuditLog).where(AuditLog.entity_type == "calendar_event", AuditLog.action == "edit")).all()[-1]
    assert "AGM 2026 (moved)" in log.after_json and '"title": "AGM 2026"' in log.before_json

    r = admin_client.post(f"/calendar/events/{ev.id}/delete", follow_redirects=False)
    assert r.status_code == 303
    seeded_db.expire_all()
    assert seeded_db.get(CalendarEvent, ev_id) is None
    log = seeded_db.scalars(select(AuditLog).where(AuditLog.entity_type == "calendar_event", AuditLog.action == "delete")).all()[-1]
    assert "AGM 2026 (moved)" in log.before_json
    assert admin_client.get(f"/calendar/events/{ev_id}/edit").status_code == 404
    assert admin_client.post(f"/calendar/events/{ev_id}/delete").status_code == 404


def test_event_validation(seeded_db, admin_client):
    _purge_events(seeded_db)
    day = date.today()
    cases = [
        {"title": "  "},
        {"event_date": ""},
        {"event_date": "not-a-date"},
        {"end_date": (day - timedelta(days=1)).isoformat()},
        {"start_time": "25:99"},
        {"end_time": "10:00"},
        {"start_time": "10:00", "end_time": "09:00"},
        {"title": "x" * 201},
    ]
    for over in cases:
        r = _add_event(admin_client, **over)
        assert r.status_code == 400, over
    assert seeded_db.scalar(select(CalendarEvent)) is None
    assert _add_event(admin_client, event_type="Bogus").status_code == 303
    assert seeded_db.scalar(select(CalendarEvent)).event_type == "Other"
    # End time on a multi-day event may be earlier than the start time.
    assert _add_event(admin_client, title="Camp", end_date=(day + timedelta(days=2)).isoformat(),
                      start_time="10:00", end_time="09:00").status_code == 303
    _purge_events(seeded_db)


def test_owner_cannot_manage_events(seeded_db, admin_client, owner_client):
    _purge_events(seeded_db)
    _add_event(admin_client, title="Locked event")
    ev = seeded_db.scalar(select(CalendarEvent))
    day = date.today().isoformat()
    assert owner_client.get("/calendar/events/new").status_code == 403
    assert owner_client.get(f"/calendar/events/{ev.id}/edit").status_code == 403
    assert owner_client.post("/calendar/events", data={"title": "Hack", "event_date": day}).status_code == 403
    assert owner_client.post(f"/calendar/events/{ev.id}", data={"title": "Hack", "event_date": day}).status_code == 403
    assert owner_client.post(f"/calendar/events/{ev.id}/delete").status_code == 403
    seeded_db.expire_all()
    assert seeded_db.scalar(select(CalendarEvent.title)) == "Locked event"
    assert seeded_db.scalars(select(CalendarEvent)).all().__len__() == 1
    _purge_events(seeded_db)


def test_anonymous_cannot_use_calendar(seeded_db):
    from fastapi.testclient import TestClient
    from app.main import app

    anon = TestClient(app)
    assert anon.get("/calendar", follow_redirects=False).status_code == 303
    assert anon.post("/calendar/events", data={"title": "x", "event_date": "2026-10-10"}, follow_redirects=False).status_code == 401


def test_hidden_events_are_not_shown_to_owners(seeded_db, admin_client, owner_client):
    _purge_events(seeded_db)
    day = date.today().isoformat()
    _add_event(admin_client, title="Public coffee morning", event_date=day, visible_to_owners="1")
    _add_event(admin_client, title="Committee only budget talk", event_date=day, visible_to_owners="", notes="secret notes")
    owner_page = owner_client.get("/calendar").text
    assert "Public coffee morning" in owner_page
    assert "Committee only budget talk" not in owner_page and "secret notes" not in owner_page
    admin_page = admin_client.get("/calendar").text
    assert "Public coffee morning" in admin_page and "Committee only budget talk" in admin_page
    assert "Hidden from owners." in admin_page
    assert not any("Committee" in i["title"] for i in _data(owner_page).values())
    owner_events = [i for i in _data(owner_page).values() if i["type"] == "event"]
    assert owner_events and all(i["event_id"] is None and i["link"] is None for i in owner_events)  # read-only
    admin_events = [i for i in _data(admin_page).values() if i["type"] == "event"]
    assert all(i["event_id"] and i["link"].endswith("/edit") for i in admin_events)
    assert "cal-p-delete" not in owner_page and "cal-p-delete" in admin_page
    _purge_events(seeded_db)


def test_summary_counts_events_and_overdue(seeded_db, admin_client):
    _purge_events(seeded_db)
    today = date.today()
    base = cal.summary(cal.month_items(seeded_db, today.year, today.month, cal.Viewer(is_admin=True), today))
    _add_event(admin_client, title="Count me")
    after = cal.summary(cal.month_items(seeded_db, today.year, today.month, cal.Viewer(is_admin=True), today))
    assert after["events"] == base["events"] + 1 and after["total"] == base["total"] + 1
    page = admin_client.get("/calendar").text
    assert re.search(rf"<strong>{after['total']}</strong> item", page) and "overdue</p>" in page
    _purge_events(seeded_db)


def test_multi_day_event_shows_on_each_day_and_in_neighbouring_month(seeded_db, admin_client):
    _purge_events(seeded_db)
    first = date(2027, 1, 30)
    _add_event(admin_client, title="Painting works", event_date=first.isoformat(), end_date="2027-02-02")
    admin = cal.Viewer(is_admin=True)
    jan = cal.month_items(seeded_db, 2027, 1, admin, date(2027, 1, 1))
    feb = cal.month_items(seeded_db, 2027, 2, admin, date(2027, 1, 1))
    assert any(i["title"] == "Painting works" for i in jan) and any(i["title"] == "Painting works" for i in feb)
    jan_days = cal.expand_days(jan, *cal.month_bounds(2027, 1))
    feb_days = cal.expand_days(feb, *cal.month_bounds(2027, 2))
    assert [d.day for d, items in sorted(jan_days.items()) if any(i["title"] == "Painting works" for i in items)] == [30, 31]
    assert [d.day for d, items in sorted(feb_days.items()) if any(i["title"] == "Painting works" for i in items)] == [1, 2]
    _purge_events(seeded_db)


def test_event_appears_in_exports(seeded_db, admin_client):
    _purge_events(seeded_db)
    _add_event(admin_client, title="Exported event")
    dump = admin_client.get("/admin/export/json").json()
    assert any(r["title"] == "Exported event" for r in dump["tables"]["calendar_events"]["rows"])
    _purge_events(seeded_db)


# ---- migration -----------------------------------------------------------------------------------------


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)


def _tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _version(db_path: Path) -> str:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def test_migration_from_scratch_and_roundtrip(tmp_path):
    db = tmp_path / "fresh.db"
    done = _alembic(db, "upgrade", "head")
    assert done.returncode == 0, done.stderr
    assert "calendar_events" in _tables(db) and _version(db) == "a3d9e6c1b852"
    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(calendar_events)")}
        idx = {r[1] for r in conn.execute("PRAGMA index_list(calendar_events)")}
    assert {"title", "event_date", "end_date", "start_time", "end_time", "event_type", "notes",
            "visible_to_owners", "created_by", "updated_at"} <= cols
    assert "ix_calendar_events_event_date" in idx
    down = _alembic(db, "downgrade", "a7d2c4e9b108")
    assert down.returncode == 0, down.stderr
    assert "calendar_events" not in _tables(db)
    assert _alembic(db, "upgrade", "head").returncode == 0 and "calendar_events" in _tables(db)
    assert _alembic(db, "upgrade", "head").returncode == 0  # idempotent


def test_migration_tolerates_table_created_by_startup_safety_net(tmp_path):
    db = tmp_path / "pre.db"
    assert _alembic(db, "upgrade", "a7d2c4e9b108").returncode == 0
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}", "INSTANCE_DIR": str(tmp_path / "i"), "BACKUP_DIR": str(tmp_path / "b")}
    # What app startup does on a stamped database: create_all() adds the missing table.
    code = "from app.database import Base, engine; from app.models import entities; Base.metadata.create_all(bind=engine)"
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, check=True)
    assert "calendar_events" in _tables(db)
    done = _alembic(db, "upgrade", "head")
    assert done.returncode == 0, done.stderr
    assert _version(db) == "a3d9e6c1b852"


def _v7_db(tmp_path: Path) -> Path:
    """A copy of the real v7 database when it is around, otherwise one built with the seed script."""
    target = tmp_path / "v7copy.db"
    candidates = [os.environ.get("NEST5_V7_DB", ""),
                  "/cursor/stores/bc-46823214-4665-42d8-b73f-67ac43c474c0/nest5-ledger-v7/nest5.db"]
    source = next((Path(c) for c in candidates if c and Path(c).exists()), None)
    if source is not None:
        shutil.copy(source, target)
        return target
    assert _alembic(target, "upgrade", "a7d2c4e9b108").returncode == 0
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{target}", "INSTANCE_DIR": str(tmp_path / "i"), "BACKUP_DIR": str(tmp_path / "b")}
    subprocess.run([sys.executable, "scripts/seed.py"], cwd=ROOT, env=env, check=True, capture_output=True)
    return target


def test_migration_on_a_copy_of_the_v7_database(tmp_path):
    db = _v7_db(tmp_path)
    with sqlite3.connect(db) as conn:
        before = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                  for t in ("flats", "users", "maintenance_payments", "expenses", "audit_logs")
                  if t in _tables(db)}
    done = _alembic(db, "upgrade", "head")
    assert done.returncode == 0, done.stderr
    assert _version(db) == "a3d9e6c1b852" and "calendar_events" in _tables(db)
    with sqlite3.connect(db) as conn:
        after = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in before}
        assert conn.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0] == 0
    assert after == before  # existing rows untouched
    # The upgraded copy serves the calendar for every role.
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}", "INSTANCE_DIR": str(tmp_path / "i2"), "BACKUP_DIR": str(tmp_path / "b2")}
    code = (
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "from app.database import SessionLocal\n"
        "from app.services import users as u\n"
        "db = SessionLocal(); a = u.get_by_username(db, 'admin'); u.set_password(a, 'pw-pw-pw-1', must_change=False); db.commit()\n"
        "c = TestClient(app); assert c.post('/login', data={'username': 'admin', 'password': 'pw-pw-pw-1'}, follow_redirects=False).status_code == 303\n"
        "r = c.get('/calendar'); assert r.status_code == 200, r.text[:300]\n"
        "r = c.post('/calendar/events', data={'title': 'Post-migration', 'event_date': '2026-10-20'}, follow_redirects=False); assert r.status_code == 303\n"
        "print('OK')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert "OK" in out.stdout, out.stderr[-1500:]
