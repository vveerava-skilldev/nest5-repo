"""v7.1: admin Activity feed, humanized audit log, and the migration for submission reviews / rejections."""
import ast
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

import pytest

from app.models import AuditLog, Flat, LedgerPeriod
from app.services import activity as activity_svc
from tests.test_v71_empty_and_review import run_scratch

ROOT = Path(__file__).resolve().parents[1]


def _entry(entity, action, label="admin", before=None, after=None, entity_id=7, when=None):
    return AuditLog(
        entity_type=entity, entity_id=entity_id, action=action, user_label=label,
        before_json=json.dumps(before) if before is not None else None,
        after_json=json.dumps(after) if after is not None else None,
        created_at=when or datetime(2026, 10, 19, 14, 32),
    )


def _flat_period(db):
    flat = db.scalar(select(Flat).where(Flat.number == "102"))
    period = db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10))
    return flat, period


# ---- humanizing ------------------------------------------------------------------------------


def test_owner_submission_sentence_matches_the_spec(seeded_db):
    flat, period = _flat_period(seeded_db)
    lk = activity_svc.lookups(seeded_db)
    e = _entry("maintenance_payment", "owner_submit", "sastry",
               after={"flat_id": flat.id, "period_id": period.id, "amount": 2000})
    h = activity_svc.humanize(e, lk)
    assert h["sentence"] == "sastry (owner, Flat 102) submitted maintenance ₹2,000 for 10-2026"
    assert h["role"] == "owner" and h["badge"] == "Owner" and h["amount"] == 2000 and h["flat"] == "Flat 102"


def test_admin_reject_expense_sentence(seeded_db):
    lk = activity_svc.lookups(seeded_db)
    e = _entry("expense", "reject", "admin", before={"description": "Bulbs", "amount": 640}, after={"description": "Bulbs", "amount": 640})
    h = activity_svc.humanize(e, lk)
    assert h["sentence"] == "admin rejected expense “Bulbs” ₹640"
    assert h["role"] == "admin" and h["badge"] == "Admin" and h["who"] == "admin"


def test_rejected_owner_payments_have_sentences(seeded_db):
    flat, period = _flat_period(seeded_db)
    lk = activity_svc.lookups(seeded_db)
    mp = activity_svc.humanize(_entry("maintenance_payment", "reject", "admin",
                                      after={"flat_id": flat.id, "period_id": period.id, "amount": 2500}), lk)
    assert mp["sentence"] == "admin rejected maintenance payment ₹2,500 for 10-2026" and mp["flat"] == "Flat 102"
    cp = activity_svc.humanize(_entry("corpus_payment", "reject", "admin", after={"amount": 100000, "flat_id": flat.id}), lk)
    assert cp["sentence"] == "admin rejected corpus payment ₹1,00,000"


def test_tenant_role_and_flat_in_sentence(seeded_db, admin_client):
    flat = seeded_db.scalar(select(Flat).where(Flat.number == "101"))
    admin_client.post("/admin/users", data={"username": "tnt.act101", "display_name": "T", "role": "tenant", "flat_ids": [flat.id]})
    try:
        lk = activity_svc.lookups(seeded_db)
        h = activity_svc.humanize(_entry("maintenance_payment", "owner_submit", "tnt.act101", after={"amount": 1500}), lk)
        assert h["sentence"].startswith("tnt.act101 (tenant, Flat 101) submitted maintenance ₹1,500") and h["badge"] == "Tenant"
    finally:
        from app.models import User
        seeded_db.expire_all()
        u = seeded_db.scalar(select(User).where(User.username == "tnt.act101"))
        if u is not None:
            seeded_db.delete(u)
            seeded_db.commit()


def test_unknown_entries_use_a_generic_fallback_and_never_crash(seeded_db):
    lk = activity_svc.lookups(seeded_db)
    h = activity_svc.humanize(_entry("gizmo", "do_a_thing", "ghost", entity_id=9), lk)
    assert h["text"] == "do a thing on gizmo #9" and h["badge"] == "System" and h["role"] == "system"
    for raw in ("not json", "[1, 2]", '"just a string"', "null", ""):
        e = AuditLog(entity_type="expense", entity_id=1, action="create", user_label="admin", before_json=raw, after_json=raw,
                     created_at=datetime(2026, 10, 1, 8, 0))
        assert activity_svc.humanize(e, lk)["sentence"].startswith("admin ")
    # snapshots that were stored as a JSON string inside JSON still read well
    inner = json.dumps({"description": "Paint", "amount": 1200})
    twice = AuditLog(entity_type="expense", entity_id=1, action="delete", user_label="admin", before_json=json.dumps(inner),
                     created_at=datetime(2026, 10, 1, 8, 0))
    assert activity_svc.humanize(twice, lk)["sentence"] == "admin deleted expense “Paint” ₹1,200"


EXTRA_PAIRS = [
    ("settlement", "create"), ("settlement", "update"), ("settlement", "revert"),
    ("user", "role_change"), ("user", "update"),
    ("corpus_payment", "owner_submit"), ("corpus_payment", "reject"),
    ("maintenance_payment", "reject"), ("project_contribution", "reject"),
    ("project_contribution", "delete"), ("project_payment", "delete"), ("expense", "delete"),
]


def _written_pairs():
    pairs = set(EXTRA_PAIRS)
    for f in (ROOT / "app").rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) == "log_audit":
                kw = {k.arg: k.value for k in n.keywords}
                et, ac = kw.get("entity_type"), kw.get("action")
                if isinstance(et, ast.Constant) and isinstance(ac, ast.Constant):
                    pairs.add((et.value, ac.value))
    return pairs


def test_every_audit_action_the_app_writes_has_a_real_sentence(seeded_db):
    pairs = _written_pairs()
    assert len(pairs) > 55
    lk = activity_svc.lookups(seeded_db)
    generic = []
    for entity, action in sorted(pairs):
        h = activity_svc.humanize(_entry(entity, action, after={"description": "X"}), lk)
        if h["text"].startswith(f"{action.replace('_', ' ')} on "):
            generic.append((entity, action))
    assert not generic, f"fall back to the generic sentence: {generic}"


def test_inr_uses_indian_grouping():
    assert activity_svc.inr(640) == "₹640" and activity_svc.inr(2000) == "₹2,000"
    assert activity_svc.inr(123456) == "₹1,23,456" and activity_svc.inr(10000000) == "₹1,00,00,000"


# ---- pages, filters, access -------------------------------------------------------------------

FEED_SCRIPT = """
from datetime import datetime, timedelta
from app.services import activity as act
db.execute(text('DELETE FROM audit_logs')); db.commit()
now = datetime.utcnow()
fl = db.scalar(select(Flat).where(Flat.number == '102')).id
def add(entity, action, label, days, **after):
    db.add(AuditLog(entity_type=entity, entity_id=3, action=action, user_label=label,
                    after_json=json.dumps(after), created_at=now - timedelta(days=days)))
add('maintenance_payment', 'owner_submit', 'sastry', 0, amount=2000, flat_id=fl)
add('expense', 'reject', 'admin', 1, description='Bulbs', amount=640)
add('expense', 'create', 'admin', 10, description='Paint', amount=900)
add('maintenance_payment', 'owner_submit', 'sastry', 20, amount=1111, flat_id=fl)
add('expense', 'create', 'admin', 45, description='OldBill', amount=50)
add('gizmo', 'wobble', 'admin', 2)
for i in range(30):
    add('expense', 'create', 'admin', 3, description='Bulk %d' % i, amount=10 + i)
db.commit()
out['total'] = db.scalar(text('SELECT COUNT(*) FROM audit_logs'))
admin.post('/admin/users', data={'username': 'tnt.feed', 'display_name': 'T', 'role': 'tenant', 'flat_ids': [fl]})
tu = db.scalar(select(User).where(User.username == 'tnt.feed')); u.set_password(tu, 'pw-pw-pw-1', must_change=False)
tu2 = db.scalar(select(User).where(User.username == 'tnt.feed'))
db.add(AuditLog(entity_type='maintenance_payment', entity_id=3, action='owner_submit', user_label='tnt.feed',
                after_json=json.dumps({'amount': 777, 'flat_id': fl}), created_at=now))
db.commit()
def get(url, c=None):
    r = (c or admin).get(url)
    return r.status_code, r.text
out['all'] = get('/admin/activity')
out['owners'] = get('/admin/activity?role=owners')
out['admins'] = get('/admin/activity?role=admin')
out['tenants'] = get('/admin/activity?role=tenants')
out['r7'] = get('/admin/activity?range=7&role=admin')
out['r30'] = get('/admin/activity?range=30&role=admin')
out['r30b'] = get('/admin/activity?range=30&role=admin&page=2')
out['dates'] = get('/admin/activity?date_from=' + (now - timedelta(days=12)).strftime('%Y-%m-%d') + '&date_to=' + (now - timedelta(days=8)).strftime('%Y-%m-%d'))
out['p1'] = get('/admin/activity?role=admin&range=7')
out['p2'] = get('/admin/activity?role=admin&range=7&page=2')
out['p3'] = get('/admin/activity?role=admin&range=7&page=3')
out['bad'] = get('/admin/activity?role=zzz&range=99&date_from=garbage&page=-4')
out['dash'] = get('/')
out['audit'] = get('/audit')
sastry = login('sastry'); tenant = login('tnt.feed')
out['owner_activity'] = [get('/admin/activity', sastry)[0], get('/audit', sastry)[0]]
out['tenant_activity'] = [get('/admin/activity', tenant)[0], get('/audit', tenant)[0]]
out['owner_dash'] = get('/', sastry)
out['owner_nav'] = get('/corpus', sastry)
"""


@pytest.fixture(scope="module")
def feed_data(tmp_path_factory):
    return run_scratch(tmp_path_factory.mktemp("feed"), FEED_SCRIPT)


def _items(page: str) -> list[str]:
    return re.findall(r'<div class="activity-text">(.*?)</div>', page, re.S)


def test_activity_page_filters_ranges_and_pagination(feed_data):
    d = feed_data
    code, page = d["all"]
    assert code == 200 and 'class="activity-list"' in page and "<table" not in page.split("activity-list")[1].split("</ul>")[0]
    assert "sastry (owner, Flat 102) submitted maintenance ₹2,000" in page
    assert "admin rejected expense “Bulbs” ₹640" in page
    assert "wobble on gizmo #3" in page  # unknown action: generic fallback
    assert re.search(r"\d\d-\d\d-\d{4} \d\d:\d\d", page)
    for chip in ("All", "Owners", "Admin", "Tenants", "Last 7 days", "Last 30 days"):
        assert f">{chip}</a>" in page
    assert page.index("sastry (owner") < page.index("Bulbs") < page.index("wobble on gizmo")  # newest first

    owners = d["owners"][1]
    assert "submitted maintenance ₹2,000" in owners and "Bulbs" not in owners and "tnt.feed" not in owners
    assert 'chip active" href="/admin/activity?role=owners"' in owners
    admins = d["admins"][1]
    assert "Bulbs" in admins and "sastry (owner" not in admins
    tenants = d["tenants"][1]
    assert "tnt.feed (tenant, Flat 102) submitted maintenance ₹777" in tenants and "Bulbs" not in tenants and "sastry" not in tenants

    r7, r30 = d["r7"][1], d["r30"][1] + d["r30b"][1]
    assert "Bulbs" in r7 and "Paint" not in r7
    assert "Paint" in r30 and "OldBill" not in r30
    dates = d["dates"][1]
    assert "Paint" in dates and "Bulbs" not in dates and "OldBill" not in dates

    assert len(_items(d["p1"][1])) == 25 and "Load more" in d["p1"][1] and "Page 1" in d["p1"][1]
    assert len(_items(d["p2"][1])) == 8 and "Load more" not in d["p2"][1] and "Newer" in d["p2"][1]  # 33 admin rows in 7 days (incl. the user it created)
    assert "No activity" in d["p3"][1] or not _items(d["p3"][1])
    assert d["bad"][0] == 200


def test_dashboard_recent_activity_card(feed_data):
    d = feed_data
    code, dash = d["dash"]
    card = re.search(r'<section class="card dashboard-activity.*?</section>', dash, re.S).group(0)
    assert code == 200 and "Recent activity" in card
    assert 'href="/admin/activity"' in card and "View all" in card
    assert len(re.findall(r'class="activity-item"', card)) == 10
    assert 'href="/admin/activity"' in dash.split("</nav>")[0]  # menu item


def test_activity_and_audit_are_admin_only(feed_data):
    d = feed_data
    assert d["owner_activity"] == [403, 403] and d["tenant_activity"] == [403, 403]
    assert "/admin/activity" not in d["owner_dash"][1] and "Recent activity" not in d["owner_dash"][1]
    assert "/admin/activity" not in d["owner_nav"][1]


def test_audit_log_page_is_readable_with_details_toggle(feed_data):
    code, page = feed_data["audit"]
    assert code == 200
    assert "admin rejected expense “Bulbs” ₹640" in page
    assert page.count('<details class="audit-details">') >= 30 and "<summary>Details</summary>" in page
    raw = re.search(r'<details class="audit-details">.*?</details>', page, re.S).group(0)
    assert "&#34;" in raw or '"' in raw  # raw JSON is still there, just collapsed


def test_empty_audit_log_does_not_crash(tmp_path):
    code = """
db.execute(text('DELETE FROM audit_logs')); db.commit()
out['pages'] = {u: [admin.get(u).status_code, admin.get(u).text] for u in ('/admin/activity', '/audit', '/', '/admin/activity?role=owners&range=7', '/admin/actions')}
"""
    pages = run_scratch(tmp_path, code)["pages"]
    assert all(v[0] == 200 for v in pages.values())
    assert "No activity" in pages["/admin/activity"][1] and "No activity" in pages["/admin/activity?role=owners&range=7"][1]
    assert "Recent activity" not in pages["/"][1]  # empty cards stay hidden
    assert "No entries" in pages["/audit"][1] or "<details" not in pages["/audit"][1]


# ---- migration ------------------------------------------------------------------------------

HEAD = "a3d9e6c1b852"
PREV = "d4b8f1a3c627"


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)


def _tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _version(db_path: Path) -> str:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def test_migration_from_scratch_and_up_down_up(tmp_path):
    db = tmp_path / "fresh.db"
    done = _alembic(db, "upgrade", "head")
    assert done.returncode == 0, done.stderr
    assert {"submission_reviews", "submission_rejections"} <= _tables(db) and _version(db) == HEAD
    with sqlite3.connect(db) as conn:
        rev_cols = {r[1] for r in conn.execute("PRAGMA table_info(submission_reviews)")}
        rej_cols = {r[1] for r in conn.execute("PRAGMA table_info(submission_rejections)")}
        idx = {r[1] for r in conn.execute("PRAGMA index_list(submission_reviews)")}
    assert {"audit_id", "kind", "record_id", "reviewed_at", "reviewed_by"} <= rev_cols
    assert {"record_type", "record_id", "flat_id", "submitted_by_user_id", "summary", "amount", "paid_on", "reason",
            "rejected_at", "rejected_by"} <= rej_cols
    assert "ix_submission_reviews_audit_id" in idx
    down = _alembic(db, "downgrade", PREV)
    assert down.returncode == 0, down.stderr
    assert not {"submission_reviews", "submission_rejections"} & _tables(db) and _version(db) == PREV
    assert _alembic(db, "upgrade", "head").returncode == 0 and _version(db) == HEAD
    again = _alembic(db, "upgrade", "head")
    assert again.returncode == 0, again.stderr  # nothing left to do


def test_migration_tolerates_tables_made_by_the_startup_safety_net(tmp_path):
    db = tmp_path / "pre.db"
    assert _alembic(db, "upgrade", PREV).returncode == 0
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}", "INSTANCE_DIR": str(tmp_path / "i"), "BACKUP_DIR": str(tmp_path / "b")}
    code = "from app.database import Base, engine; from app.models import entities; Base.metadata.create_all(bind=engine)"
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, check=True)
    assert {"submission_reviews", "submission_rejections"} <= _tables(db)
    done = _alembic(db, "upgrade", "head")
    assert done.returncode == 0, done.stderr
    assert _version(db) == HEAD
    assert _alembic(db, "downgrade", PREV).returncode == 0 and _alembic(db, "upgrade", "head").returncode == 0


def test_migration_on_a_v70_style_database_keeps_data_and_submissions_pending(tmp_path):
    db = tmp_path / "v70.db"
    assert _alembic(db, "upgrade", PREV).returncode == 0
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}", "INSTANCE_DIR": str(tmp_path / "i"), "BACKUP_DIR": str(tmp_path / "b")}
    subprocess.run([sys.executable, "scripts/seed.py"], cwd=ROOT, env=env, check=True, capture_output=True)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO audit_logs (entity_type, entity_id, action, user_label, after_json, created_at) "
                     "VALUES ('maintenance_payment', 1, 'owner_submit', 'sastry', '{\"amount\": 5}', '2026-10-01 09:00:00')")
        before = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                  for t in ("flats", "users", "maintenance_payments", "expenses", "audit_logs")}
    done = _alembic(db, "upgrade", "head")
    assert done.returncode == 0, done.stderr
    assert _version(db) == HEAD
    with sqlite3.connect(db) as conn:
        after = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in before}
        assert conn.execute("SELECT COUNT(*) FROM submission_reviews").fetchone()[0] == 0  # nothing is auto-reviewed
        assert conn.execute("SELECT COUNT(*) FROM submission_rejections").fetchone()[0] == 0
    assert after == before
    page = run_scratch_on(db, tmp_path)
    assert "Actions" in page


def run_scratch_on(db_path: Path, tmp_path: Path) -> str:
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}", "INSTANCE_DIR": str(tmp_path / "i"), "BACKUP_DIR": str(tmp_path / "b")}
    code = (
        "from app.database import SessionLocal\n"
        "from app.services import actions as a\n"
        "db = SessionLocal(); b = a.build(db); print('Actions', b['total'])\n"
    )
    ran = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr[-1500:]
    return ran.stdout
