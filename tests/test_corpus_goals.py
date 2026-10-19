"""Corpus goals: categories, line items, one Corpus area, migration."""
import os
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select

from app.models import CorpusCampaign, CorpusCategory, CorpusLineItem, CorpusPayment, Flat
from app.services import corpus_campaigns as campaigns_svc

ROOT = Path(__file__).resolve().parents[1]


def _flats(db):
    return {f.number: f.id for f in db.scalars(select(Flat)).all()}


def _drop(db, campaign_id):
    db.expire_all()
    campaign = db.get(CorpusCampaign, campaign_id)
    for p in db.scalars(select(CorpusPayment).where(CorpusPayment.campaign_id == campaign_id)).all():
        db.delete(p)
    db.flush()
    db.delete(campaign)
    db.commit()


def test_parse_line_items():
    assert campaigns_svc.parse_line_items("Door sensors: 20,000\n\nCabin panels\nNote: see: 500") == [
        ("Door sensors", 20000), ("Cabin panels", None), ("Note: see", 500)
    ]


def test_goal_with_category_and_line_items(admin_client, owner_client, seeded_db):
    flats = _flats(seeded_db)
    r = admin_client.post("/corpus/campaigns", data={
        "name": "Lift modernisation", "category": "Lift", "target_amount": "50000",
        "due_date": (date.today() + timedelta(days=30)).isoformat(),
        "line_items": "Door sensors: 20000\nCabin panels: 30000",
    }, follow_redirects=False)
    goal = seeded_db.scalar(select(CorpusCampaign).where(CorpusCampaign.name == "Lift modernisation"))
    assert r.headers["location"].startswith(f"/corpus/goals/{goal.id}")
    other = None
    try:
        assert goal.category == "Lift" and [(i.title, i.target_amount) for i in goal.line_items] == [
            ("Door sensors", 20000), ("Cabin panels", 30000)]
        sensors = goal.line_items[0]
        sensors_id = sensors.id

        page = owner_client.get("/corpus").text
        assert "Lift modernisation" in page and "Door sensors" in page
        assert f'value="{flats["201"]}:{goal.id}"' in page and f'value="{flats["101"]}:{goal.id}"' not in page
        row = f'<a href="/corpus/goals/{goal.id}"><strong>Lift modernisation'
        assert row not in owner_client.get("/corpus?category=Painting").text
        assert row in owner_client.get("/corpus?category=Lift").text

        r = owner_client.post("/corpus/pay", data={
            "target": f"{flats['201']}:{goal.id}", "line_item_id": str(sensors_id), "amount": "4000",
            "paid_on": date.today().isoformat(), "payment_mode": "UPI", "payment_note": "UTR123456789", "back": "/corpus",
        }, follow_redirects=False)
        assert r.headers["location"].startswith("/corpus?msg=") and "Door+sensors" in r.headers["location"]
        r = owner_client.post("/corpus/pay", data={
            "target": f"{flats['201']}:{goal.id}", "amount": "1000", "paid_on": date.today().isoformat(), "payment_note": "UTR123456789",
        }, follow_redirects=False)
        assert "msg=" in r.headers["location"]

        seeded_db.expire_all()
        items = campaigns_svc.line_item_summary(seeded_db, seeded_db.get(CorpusCampaign, goal.id))
        assert [(row["item"].title, row["paid"], row["progress_pct"]) for row in items["rows"]] == [
            ("Door sensors", 4000, 20), ("Cabin panels", 0, 0)]
        assert items["unassigned"] == 1000
        assert campaigns_svc.remaining_for(seeded_db, goal, flats["201"]) == 10000 - 5000

        detail = owner_client.get(f"/corpus/goals/{goal.id}").text
        assert "Door sensors" in detail and "₹4000" in detail and "Line items" in detail
        assert "<td>101</td>" not in detail  # other flats' rows stay admin-only
        assert "<td>101</td>" in admin_client.get(f"/corpus/goals/{goal.id}").text

        # A line item from another goal is refused.
        admin_client.post("/corpus/campaigns", data={"name": "Painting 2027", "category": "Painting", "target_amount": "5000",
                                                     "line_items": "Primer"})
        other = seeded_db.scalar(select(CorpusCampaign).where(CorpusCampaign.name == "Painting 2027"))
        r = owner_client.post("/corpus/pay", data={
            "target": f"{flats['201']}:{goal.id}", "line_item_id": str(other.line_items[0].id), "amount": "10",
            "paid_on": date.today().isoformat()}, follow_redirects=False)
        assert "error=" in r.headers["location"]

        # Admin adds / removes line items; removing keeps the payments on the goal.
        r = admin_client.post(f"/corpus/goals/{goal.id}/line-items", data={"title": "Lighting", "target_amount": ""},
                              follow_redirects=False)
        assert "msg=" in r.headers["location"]
        assert owner_client.post(f"/corpus/goals/{goal.id}/line-items", data={"title": "x"}, follow_redirects=False).status_code == 403
        admin_client.post(f"/corpus/goals/{goal.id}/line-items/{sensors_id}/delete")
        seeded_db.expire_all()
        assert seeded_db.get(CorpusLineItem, sensors_id) is None
        assert campaigns_svc.campaign_summary(seeded_db, seeded_db.get(CorpusCampaign, goal.id))["paid_total"] == 5000
    finally:
        _drop(seeded_db, goal.id)
        if other is not None:
            _drop(seeded_db, other.id)


def test_admin_adds_category(admin_client, owner_client, seeded_db):
    assert owner_client.post("/corpus/categories", data={"name": "Water tank"}, follow_redirects=False).status_code == 403
    r = admin_client.post("/corpus/categories", data={"name": "  Water   tank "}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert "Water tank" in campaigns_svc.categories(seeded_db)
    admin_client.post("/corpus/categories", data={"name": "water TANK"})
    seeded_db.expire_all()
    assert len([c for c in campaigns_svc.categories(seeded_db) if c.lower() == "water tank"]) == 1
    assert "<option>Water tank</option>" in admin_client.get("/corpus").text
    for c in seeded_db.scalars(select(CorpusCategory).where(CorpusCategory.name == "Water tank")).all():
        seeded_db.delete(c)
    seeded_db.commit()


def test_corpus_goals_migration(tmp_path):
    db_file = tmp_path / "goals.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_file}", "BACKUP_DIR": str(tmp_path / "backups"),
           "INSTANCE_DIR": str(tmp_path / "instance")}

    def alembic(*args):
        res = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        return res

    alembic("upgrade", "a6c4e2b8d013")
    con = sqlite3.connect(db_file)
    con.executemany(
        "INSERT INTO corpus_campaigns (name, target_amount, created_at, created_by, is_general) VALUES (?, ?, '2026-10-01', 'admin', ?)",
        [("Lift repair 2026", 50000, 0), ("Painting drive", 9000, 0), ("Corpus 2026-27", 100000, 0), ("General corpus", 5000, 1)],
    )
    con.commit()
    con.close()

    alembic("upgrade", "head")
    assert list((tmp_path / "backups").glob("*pre-corpus-goals.db"))
    con = sqlite3.connect(db_file)
    cats = dict(con.execute("SELECT name, category FROM corpus_campaigns"))
    assert cats == {"Lift repair 2026": "Lift", "Painting drive": "Painting", "Corpus 2026-27": "General",
                    "General corpus": "General"}
    assert {r[0] for r in con.execute("SELECT name FROM corpus_categories")} >= {"General", "Building repair", "Lift", "Painting"}
    con.close()

    alembic("downgrade", "a6c4e2b8d013")
    con = sqlite3.connect(db_file)
    assert "category" not in {r[1] for r in con.execute("PRAGMA table_info(corpus_campaigns)")}
    assert "line_item_id" not in {r[1] for r in con.execute("PRAGMA table_info(corpus_payments)")}
    assert con.execute("SELECT COUNT(*) FROM corpus_campaigns").fetchone()[0] == 4
    con.close()
