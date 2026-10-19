"""Asset schedule: asset register, recurring tasks, maintenance log, buckets, alerts, migration."""
from app.dates import ist_today
import os
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select

from app.models import Asset, AssetMaintenanceLog, AssetMaintenanceSchedule, ScheduleFrequency
from app.services import alerts as alerts_svc
from app.services import schedule as schedule_svc

ROOT = Path(__file__).resolve().parents[1]


def _purge(db, asset_ids):
    db.expire_all()
    for log in db.scalars(select(AssetMaintenanceLog).where(AssetMaintenanceLog.asset_id.in_(asset_ids))).all():
        db.delete(log)
    db.flush()
    for task in db.scalars(select(AssetMaintenanceSchedule).where(AssetMaintenanceSchedule.asset_id.in_(asset_ids))).all():
        db.delete(task)
    db.flush()
    for aid in asset_ids:
        db.delete(db.get(Asset, aid))
    db.commit()


def test_intervals():
    start = date(2026, 1, 31)
    assert schedule_svc.add_interval(start, ScheduleFrequency.DAILY, None) == date(2026, 2, 1)
    assert schedule_svc.add_interval(start, ScheduleFrequency.WEEKLY, None) == date(2026, 2, 7)
    assert schedule_svc.add_interval(start, ScheduleFrequency.MONTHLY, None) == date(2026, 2, 28)
    assert schedule_svc.add_interval(start, ScheduleFrequency.QUARTERLY, None) == date(2026, 4, 28)
    assert schedule_svc.add_interval(start, ScheduleFrequency.YEARLY, None) == date(2027, 1, 28)
    assert schedule_svc.add_interval(start, ScheduleFrequency.CUSTOM, 10) == date(2026, 2, 10)
    assert schedule_svc.guess_type("DG generator") == "Generator"
    assert schedule_svc.guess_type("Borewell pump") == "Motor/pump"


def test_seeded_register(seeded_db, owner_client):
    names = {a.name: a for a in schedule_svc.list_assets(seeded_db)}
    assert {"Lift", "Generator", "Water tank", "Motor/pump"} <= set(names)
    for task in schedule_svc.list_schedules(seeded_db):
        assert task.asset_id is not None and task.asset_ref.name == task.asset
    page = owner_client.get("/schedule").text
    for name in ("Lift", "Generator", "Water tank", "Motor/pump"):
        assert f">{name}</a>" in page
    assert "Last log entry" in page and "View only" in page
    assert "Clear all asset tasks" not in page  # admin only


def test_register_tasks_and_log(seeded_db, admin_client, owner_client):
    today = ist_today()
    r = admin_client.post("/assets", data={
        "name": "Borewell pump", "asset_type": "Motor/pump", "location": "Cellar", "vendor_contact": "Ravi 98480",
        "install_date": "2021-04-05", "warranty_expiry": (today - timedelta(days=3)).isoformat(), "cost": "18500",
        "notes": "3 HP",
    }, follow_redirects=False)
    asset = seeded_db.scalar(select(Asset).where(Asset.name == "Borewell pump"))
    created = [asset.id]
    try:
        assert r.status_code == 303 and r.headers["location"].startswith(f"/assets/{asset.id}?msg=")
        assert (asset.asset_type, asset.location, asset.cost) == ("Motor/pump", "Cellar", 18500)
        dup = admin_client.post("/assets", data={"name": "borewell PUMP"}, follow_redirects=False)
        assert "already+exists" in dup.headers["location"]

        r = admin_client.post("/schedule", data={
            "asset_id": str(asset.id), "task": "Dry-run check", "frequency": "weekly",
            "last_done_on": (today - timedelta(days=10)).isoformat(), "back": f"/assets/{asset.id}",
        }, follow_redirects=False)
        assert r.headers["location"].startswith(f"/assets/{asset.id}?msg=")
        weekly = seeded_db.scalar(select(AssetMaintenanceSchedule).where(AssetMaintenanceSchedule.task == "Dry-run check"))
        assert weekly.asset == "Borewell pump" and weekly.next_due_on == today - timedelta(days=3)

        admin_client.post("/schedule", data={"asset_id": str(asset.id), "task": "Panel check", "frequency": "daily",
                                             "next_due_on": (today + timedelta(days=3)).isoformat()})
        admin_client.post("/schedule", data={"asset_id": str(asset.id), "task": "Impeller service",
                                             "frequency": "custom", "custom_interval_days": "20",
                                             "next_due_on": (today + timedelta(days=20)).isoformat()})
        board = schedule_svc.board(seeded_db, today)
        bucket = {name: [t.task for t, _ in board[name]] for name in ("overdue", "this_week", "upcoming")}
        assert "Dry-run check" in bucket["overdue"] and "Panel check" in bucket["this_week"]
        assert "Impeller service" in bucket["upcoming"]
        page = admin_client.get("/schedule").text
        assert page.index("Overdue <span") < page.index("Due this week <span") < page.index("Upcoming <span")
        assert page.index("Dry-run check") < page.index(">Borewell pump</a> <span")

        alerts = {a["title"]: a["count"] for a in alerts_svc.build_dashboard_alerts(seeded_db, None)}
        assert alerts.get("Overdue asset maintenance", 0) >= 1 and alerts.get("Due within 7 days", 0) >= 1
        assert "Overdue asset maintenance" in admin_client.get("/").text

        r = admin_client.post(f"/schedule/{weekly.id}/done", data={
            "done_on": today.isoformat(), "done_by": "Ravi", "cost": "350", "notes": "OK", "back": f"/assets/{asset.id}",
        }, follow_redirects=False)
        assert r.headers["location"].startswith(f"/assets/{asset.id}?msg=")
        seeded_db.expire_all()
        assert weekly.last_done_on == today and weekly.next_due_on == today + timedelta(days=7)
        log = seeded_db.scalar(select(AssetMaintenanceLog).where(AssetMaintenanceLog.schedule_id == weekly.id))
        assert (log.done_by, log.cost, log.next_due_on) == ("Ravi", 350, today + timedelta(days=7))

        assert admin_client.post(f"/assets/{asset.id}/log", data={"done_on": today.isoformat()},
                                 follow_redirects=False).headers["location"].count("Add+a+note") == 1
        admin_client.post(f"/assets/{asset.id}/log", data={"done_on": today.isoformat(), "notes": "October notes: noisy bearing"})
        lift = schedule_svc.asset_by_name(seeded_db, "Lift")
        foreign = AssetMaintenanceSchedule(asset_id=lift.id, asset="Lift", task="Foreign task", frequency=ScheduleFrequency.MONTHLY,
                                           next_due_on=today + timedelta(days=90))
        seeded_db.add(foreign)
        seeded_db.commit()
        foreign_id = foreign.id
        assert admin_client.post(f"/assets/{asset.id}/log", data={"done_on": today.isoformat(),
                                 "schedule_id": str(foreign.id)}).status_code == 400

        detail = owner_client.get(f"/assets/{asset.id}").text
        assert "Cellar" in detail and "Ravi 98480" in detail and "05-04-2021" in detail and "Expired" in detail
        assert "October notes: noisy bearing" in detail and "₹350" in detail and "Logged spend" in detail
        assert today.strftime("%d-%m-%Y") in detail
        assert "/assets/logs/" not in detail and 'action="/schedule"' not in detail
        assert "Record a check" in admin_client.get(f"/assets/{asset.id}").text

        r = admin_client.post(f"/schedule/{weekly.id}/edit", data={"task": "Dry-run check", "frequency": "monthly",
                                                                   "back": "/schedule"}, follow_redirects=False)
        assert r.headers["location"].startswith("/schedule?msg=")
        seeded_db.expire_all()
        assert weekly.frequency == ScheduleFrequency.MONTHLY and weekly.next_due_on > today + timedelta(days=27)

        log_id = log.id
        admin_client.post(f"/assets/logs/{log_id}/delete")
        seeded_db.expire_all()
        assert seeded_db.get(AssetMaintenanceLog, log_id) is None

        r = admin_client.post(f"/assets/{asset.id}/edit", data={"name": "Borewell motor", "asset_type": "Motor/pump"},
                              follow_redirects=False)
        seeded_db.expire_all()
        assert asset.name == "Borewell motor" and weekly.asset == "Borewell motor"

        admin_client.post(f"/assets/{asset.id}/delete")
        seeded_db.expire_all()
        assert asset.deleted_at is not None and weekly.deleted_at is not None
        assert "Borewell motor" not in admin_client.get("/schedule").text
        assert admin_client.get(f"/assets/{asset.id}").status_code == 404
    finally:
        seeded_db.expire_all()
        stray = seeded_db.get(AssetMaintenanceSchedule, foreign_id) if "foreign_id" in locals() else None
        if stray is not None:
            seeded_db.delete(stray)
            seeded_db.commit()
        _purge(seeded_db, created)


def test_old_schedule_form_still_works(seeded_db, admin_client):
    r = admin_client.post("/schedule", data={"asset": "Fire extinguishers", "task": "Pressure check",
                                             "frequency": "yearly", "last_done_on": "2026-03-01"},
                          follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/schedule")
    asset = seeded_db.scalar(select(Asset).where(Asset.name == "Fire extinguishers"))
    try:
        assert asset.asset_type == "Fire safety"
        task = seeded_db.scalar(select(AssetMaintenanceSchedule).where(AssetMaintenanceSchedule.asset_id == asset.id))
        assert task.next_due_on == date(2027, 3, 1)
        r = admin_client.post(f"/schedule/{task.id}/done", data={"done_on": "2026-10-01"}, follow_redirects=False)
        assert r.headers["location"].startswith("/schedule")
        seeded_db.expire_all()
        assert task.next_due_on == date(2027, 10, 1)
        assert admin_client.get("/assets", follow_redirects=False).headers["location"] == "/schedule"
    finally:
        _purge(seeded_db, [asset.id])


def test_owner_view_only(seeded_db, owner_client):
    lift = schedule_svc.asset_by_name(seeded_db, "Lift")
    task = AssetMaintenanceSchedule(asset_id=lift.id, asset="Lift", task="Owner view task", frequency=ScheduleFrequency.MONTHLY,
                                    next_due_on=ist_today() + timedelta(days=60))
    seeded_db.add(task)
    seeded_db.commit()
    try:
        _owner_view_only_checks(seeded_db, owner_client, lift, task)
    finally:
        seeded_db.expire_all()
        seeded_db.delete(seeded_db.get(AssetMaintenanceSchedule, task.id))
        seeded_db.commit()


def _owner_view_only_checks(seeded_db, owner_client, lift, task):
    page = owner_client.get("/schedule").text
    assert 'action="/assets"' not in page and "/done" not in page and "Add recurring task" not in page
    assert owner_client.get(f"/assets/{lift.id}").status_code == 200
    for path, data in [("/assets", {"name": "X"}), (f"/assets/{lift.id}/edit", {"name": "X"}),
                       (f"/assets/{lift.id}/delete", {}), (f"/assets/{lift.id}/log", {"done_on": "2026-10-01", "notes": "x"}),
                       ("/schedule", {"asset": "X", "task": "Y"}), (f"/schedule/{task.id}/done", {"done_on": "2026-10-01"}),
                       (f"/schedule/{task.id}/edit", {"task": "Y"}), (f"/schedule/{task.id}/delete", {})]:
        assert owner_client.post(path, data=data, follow_redirects=False).status_code == 403, path
    seeded_db.expire_all()
    assert lift.name == "Lift" and task.deleted_at is None


def test_migration_seeds_register_and_downgrades(tmp_path):
    db_file = tmp_path / "copy.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_file}", "BACKUP_DIR": str(tmp_path / "backups"),
           "INSTANCE_DIR": str(tmp_path / "instance")}

    def alembic(*args):
        res = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        return res

    alembic("upgrade", "d3a1c5e7f902")
    con = sqlite3.connect(db_file)
    con.executemany(
        "INSERT INTO asset_maintenance_schedules (asset, task, frequency, last_done_on, next_due_on, vendor) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [("Water tank", "Cleaning", "QUARTERLY", "2026-07-01", "2026-10-01", None),
         ("Lift", "Door inspection", "MONTHLY", "2026-09-01", "2026-10-01", "Otis AMC"),
         ("lift", "Annual licence", "YEARLY", None, "2027-01-01", None)],
    )
    con.commit()
    con.close()

    alembic("upgrade", "head")
    assert list((tmp_path / "backups").glob("*pre-asset-register.db"))
    con = sqlite3.connect(db_file)
    assets = {name: (aid, kind, vendor) for aid, name, kind, vendor in
              con.execute("SELECT id, name, asset_type, vendor_contact FROM assets")}
    assert set(assets) == {"Water tank", "Lift", "Generator", "Motor/pump"}
    assert assets["Lift"][1:] == ("Lift", "Otis AMC") and assets["Motor/pump"][1] == "Motor/pump"
    links = dict(con.execute("SELECT task, asset_id FROM asset_maintenance_schedules"))
    assert links == {"Cleaning": assets["Water tank"][0], "Door inspection": assets["Lift"][0],
                     "Annual licence": assets["Lift"][0]}
    logs = con.execute("SELECT asset_id, done_on FROM asset_maintenance_logs ORDER BY done_on").fetchall()
    assert logs == [(assets["Water tank"][0], "2026-07-01"), (assets["Lift"][0], "2026-09-01")]
    con.execute("UPDATE asset_maintenance_schedules SET frequency = 'WEEKLY' WHERE task = 'Cleaning'")
    con.commit()
    con.close()

    alembic("downgrade", "d3a1c5e7f902")
    con = sqlite3.connect(db_file)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "assets" not in tables and "asset_maintenance_logs" not in tables
    assert "asset_id" not in {r[1] for r in con.execute("PRAGMA table_info(asset_maintenance_schedules)")}
    assert con.execute("SELECT frequency, custom_interval_days FROM asset_maintenance_schedules WHERE task='Cleaning'"
                       ).fetchone() == ("CUSTOM", 7)
    assert con.execute("SELECT COUNT(*) FROM asset_maintenance_schedules").fetchone()[0] == 3
    con.close()
    alembic("upgrade", "head")
