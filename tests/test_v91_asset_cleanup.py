"""v9.1: no sample asset tasks/alerts on a new install; admin + CLI 'clear all asset tasks & alerts'."""
import os
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.models import Asset, AssetMaintenanceLog, AssetMaintenanceSchedule, AuditLog, ScheduleFrequency
from app.services import actions as actions_svc
from app.services import alerts as alerts_svc
from app.services import schedule as schedule_svc

ROOT = Path(__file__).resolve().parents[1]
PAGES = ["/", "/?view=simple", "/schedule", "/calendar", "/payments/maintenance", "/corpus", "/expenses", "/complaints"]


def _asset_alert_titles(db):
    return [a["title"] for a in alerts_svc.build_dashboard_alerts(db, None)
            if "asset" in a["title"].lower() or a["title"].startswith("Due within")]


def _add_task(db, asset_name, task, next_due, *, with_log=True):
    asset = schedule_svc.get_or_create_asset(db, asset_name)
    row = AssetMaintenanceSchedule(asset_id=asset.id, asset=asset.name, task=task, frequency=ScheduleFrequency.MONTHLY,
                                   last_done_on=next_due - timedelta(days=30), next_due_on=next_due)
    db.add(row)
    db.flush()
    if with_log:
        db.add(AssetMaintenanceLog(asset_id=asset.id, schedule_id=row.id, done_on=next_due - timedelta(days=30),
                                   notes="old", created_by="test"))
    db.commit()
    return row.id


def test_seed_creates_assets_but_no_tasks(tmp_path):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'fresh.db'}", "INSTANCE_DIR": str(tmp_path / "i"),
           "BACKUP_DIR": str(tmp_path / "b")}
    out = subprocess.run([sys.executable, "scripts/seed.py"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    con = sqlite3.connect(tmp_path / "fresh.db")
    assert con.execute("SELECT COUNT(*) FROM asset_maintenance_schedules").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM asset_maintenance_logs").fetchone()[0] == 0
    names = {r[0] for r in con.execute("SELECT name FROM assets")}
    assert {"Lift", "Generator", "Water tank", "Motor/pump"} <= names
    con.close()
    code = (
        "from fastapi.testclient import TestClient\\n"
        "from app.main import app\\n"
        "from app.database import SessionLocal\\n"
        "from app.services import users as u, alerts as a, actions as h\\n"
        "db = SessionLocal(); u.ensure_bootstrap_users(db); db.commit(); ad = u.get_by_username(db, 'admin'); u.set_password(ad, 'pw-pw-pw-1', must_change=False); db.commit()\\n"
        "c = TestClient(app); assert c.post('/login', data={'username': 'admin', 'password': 'pw-pw-pw-1'}, follow_redirects=False).status_code == 303\\n"
        "for url in ('/', '/?view=simple', '/schedule', '/calendar', '/admin/actions', '/assets/1'):\\n"
        "    r = c.get(url); assert r.status_code == 200, (url, r.status_code)\\n"
        "assert 'Overdue asset' not in c.get('/').text and 'Nothing due' in c.get('/schedule').text\\n"
        "assert not [x for x in a.build_dashboard_alerts(db, None) if 'asset' in x['title'].lower()]\\n"
        "assert [g for g in h.build(db)['groups'] if g['key'] == 'assets'][0]['count'] == 0\\n"
        "print('OK')\\n"
    )
    ran = subprocess.run([sys.executable, "-c", code.replace("\\n", "\n")], cwd=ROOT, env=env, capture_output=True, text=True)
    assert "OK" in ran.stdout, ran.stderr[-1500:]


def test_clear_action_removes_tasks_alerts_everywhere_and_keeps_assets(seeded_db, admin_client, owner_client):
    today = date.today()
    asset_count = len(schedule_svc.list_assets(seeded_db))
    ids = [_add_task(seeded_db, "Lift", "Door inspection v91", today - timedelta(days=5)),
           _add_task(seeded_db, "Generator", "Load test v91", today + timedelta(days=3)),
           _add_task(seeded_db, "Water tank", "Cleaning v91", today + timedelta(days=15))]
    lift = schedule_svc.asset_by_name(seeded_db, "Lift")
    seeded_db.add(AssetMaintenanceLog(asset_id=lift.id, schedule_id=None, done_on=today, notes="General note keeps", created_by="t"))
    seeded_db.commit()

    # alerts exist everywhere first
    assert "Overdue asset maintenance" in _asset_alert_titles(seeded_db)
    hub = {g["key"]: g for g in actions_svc.build(seeded_db)["groups"]}
    assert hub["assets"]["count"] == 1
    assert "Overdue asset maintenance" in admin_client.get("/").text
    page = admin_client.get("/schedule").text
    assert "Clear all asset tasks" in page and "Type CLEAR" in page and "3 task(s)" in page
    assert "Clear all asset tasks" not in owner_client.get("/schedule").text

    # Owners 403; admin needs the typed confirmation
    assert owner_client.post("/schedule/clear-all", data={"confirm": "CLEAR"}, follow_redirects=False).status_code == 403
    r = admin_client.post("/schedule/clear-all", data={"confirm": "yes"}, follow_redirects=False)
    assert "Type+CLEAR" in r.headers["location"]
    seeded_db.expire_all()
    assert schedule_svc.task_counts(seeded_db)["tasks"] == 3

    backups_before = len(list(Path(settings.backup_dir).glob("nest5-*-pre-clear-asset-tasks.db")))
    r = admin_client.post("/schedule/clear-all", data={"confirm": "CLEAR"}, follow_redirects=False)
    assert r.headers["location"].startswith("/schedule?msg=") and "Cleared+3+asset+task" in r.headers["location"]
    assert len(list(Path(settings.backup_dir).glob("nest5-*-pre-clear-asset-tasks.db"))) == backups_before + 1

    seeded_db.expire_all()
    assert schedule_svc.task_counts(seeded_db) == {"tasks": 0, "logs": 0}
    assert len(schedule_svc.list_assets(seeded_db)) == asset_count  # assets kept
    assert seeded_db.scalar(select(AssetMaintenanceLog).where(AssetMaintenanceLog.notes == "General note keeps"))
    log = seeded_db.scalar(select(AuditLog).where(AuditLog.entity_type == "asset_task", AuditLog.action == "clear_all"))
    assert log and '"tasks": 3' in log.after_json and "pre-clear-asset-tasks" in log.after_json

    # No alerts / hub / calendar / reminders / pages with zero asset tasks
    assert _asset_alert_titles(seeded_db) == []
    assert {g["key"]: g for g in actions_svc.build(seeded_db)["groups"]}["assets"]["count"] == 0
    for client in (admin_client, owner_client):
        for url in PAGES:
            r = client.get(url)
            assert r.status_code == 200, url
            assert "Door inspection v91" not in r.text and "Overdue asset maintenance" not in r.text, url
    assert admin_client.get("/schedule").status_code == 200 and "Nothing due" in admin_client.get("/schedule").text

    # still usable: add a task again, then it alerts again
    asset = schedule_svc.asset_by_name(seeded_db, "Lift")
    r = admin_client.post("/schedule", data={"asset_id": str(asset.id), "task": "Fresh task v91", "frequency": "monthly",
                                             "next_due_on": (today - timedelta(days=1)).isoformat()}, follow_redirects=False)
    assert r.status_code == 303
    seeded_db.expire_all()
    assert "Overdue asset maintenance" in _asset_alert_titles(seeded_db)
    admin_client.post("/schedule/clear-all", data={"confirm": "CLEAR"})
    seeded_db.expire_all()
    assert schedule_svc.task_counts(seeded_db)["tasks"] == 0
    # a second clear with nothing to delete is harmless
    r = admin_client.post("/schedule/clear-all", data={"confirm": "CLEAR"}, follow_redirects=False)
    assert "Cleared+0+asset" in r.headers["location"]


def test_clear_script(tmp_path):
    db_file = tmp_path / "script.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_file}", "INSTANCE_DIR": str(tmp_path / "i"), "BACKUP_DIR": str(tmp_path / "b")}
    assert subprocess.run([sys.executable, "scripts/seed.py"], cwd=ROOT, env=env, capture_output=True).returncode == 0
    con = sqlite3.connect(db_file)
    asset_id = con.execute("SELECT id FROM assets WHERE name='Lift'").fetchone()[0]
    for n in range(2):
        cur = con.execute("INSERT INTO asset_maintenance_schedules (asset_id, asset, task, frequency, next_due_on) "
                          "VALUES (?, 'Lift', ?, 'MONTHLY', '2026-09-01')", (asset_id, f"Old task {n}"))
        con.execute("INSERT INTO asset_maintenance_logs (asset_id, schedule_id, done_on, created_at) VALUES (?, ?, '2026-08-01', '2026-08-01')",
                    (asset_id, cur.lastrowid))
    con.execute("INSERT INTO asset_maintenance_logs (asset_id, schedule_id, done_on, created_at) VALUES (?, NULL, '2026-08-02', '2026-08-02')", (asset_id,))
    con.commit()
    con.close()

    def run(*args):
        return subprocess.run([sys.executable, "scripts/clear_asset_alerts.py", *args], cwd=ROOT, env=env, capture_output=True, text=True)

    dry = run()
    assert dry.returncode == 0 and "Asset tasks: 2" in dry.stdout and "Dry run" in dry.stdout, dry.stdout + dry.stderr
    con = sqlite3.connect(db_file)
    assert con.execute("SELECT COUNT(*) FROM asset_maintenance_schedules").fetchone()[0] == 2  # dry run changed nothing
    con.close()
    assert not list((tmp_path / "b").glob("*pre-clear-asset-tasks.db"))

    done = run("--yes")
    assert done.returncode == 0 and "Cleared 2 task(s) and 2 log entries" in done.stdout, done.stdout + done.stderr
    backups = list((tmp_path / "b").glob("nest5-*-pre-clear-asset-tasks.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as bk:
        assert bk.execute("SELECT COUNT(*) FROM asset_maintenance_schedules").fetchone()[0] == 2  # backup is the pre-change copy
    con = sqlite3.connect(db_file)
    assert con.execute("SELECT COUNT(*) FROM asset_maintenance_schedules").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM asset_maintenance_logs").fetchone()[0] == 1  # the general note stays
    assert con.execute("SELECT COUNT(*) FROM assets").fetchone()[0] >= 4
    assert con.execute("SELECT user_label FROM audit_logs WHERE action='clear_all' AND entity_type='asset_task'").fetchone()[0] == "cli"
    con.close()
    again = run("--yes")
    assert again.returncode == 0 and "Nothing to clear" in again.stdout
