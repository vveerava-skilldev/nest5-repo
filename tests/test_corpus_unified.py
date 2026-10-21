"""One corpus picture: collections (incl. General corpus) everywhere, Payments tabs, overview pages."""
from app.dates import ist_today
import os
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import select

from app.models import CorpusCampaign, CorpusPayment, Expense, Flat, LedgerPeriod, Project, ProjectContribution, ProjectPayment
from app.services import corpus_campaigns as campaigns_svc
from app.services import ledger as ledger_svc
from app.services import pdf_report
from app.services import projects as projects_svc
from tests.conftest import OWNER_PASSWORD, make_client

ROOT = Path(__file__).resolve().parents[1]


def _flats(db):
    return {f.number: f.id for f in db.scalars(select(Flat)).all()}


def _row(status, number):
    return next(r for r in status["rows"] if r["flat"].number == number)


def _close(db, campaign):
    db.expire_all()
    for p in db.scalars(select(CorpusPayment).where(CorpusPayment.campaign_id == campaign.id)).all():
        db.delete(p)
    for sh in list(campaign.shares):
        db.delete(sh)
    db.delete(campaign)
    db.commit()


def test_partial_corpus_payment_shows_everywhere_counted_once(admin_client, seeded_db, set_password):
    flats = _flats(seeded_db)
    set_password(seeded_db, "manikyamba", OWNER_PASSWORD)
    owner = make_client("manikyamba", OWNER_PASSWORD)
    def snapshot():
        seeded_db.expire_all()
        latest = seeded_db.scalars(select(LedgerPeriod).order_by(LedgerPeriod.year.desc(), LedgerPeriod.month.desc())).first()
        status = ledger_svc.build_dashboard_status(seeded_db, latest)
        return {
            "cell": dict(_row(status, "101")["corpus"]),
            "row_pending": _row(status, "101")["row_pending"],
            "corpus_pending": status["totals"]["corpus_pending"],
            "grand": status["totals"]["grand_pending"],
            "owed": ledger_svc.flat_balances(seeded_db, seeded_db.get(Flat, flats["101"]))["owed_corpus"],
            "flat": campaigns_svc.flat_corpus_totals(seeded_db, flats["101"]),
            "building": campaigns_svc.building_totals(seeded_db),
        }

    r = admin_client.post(
        "/corpus/campaigns",
        data={"name": "Lift upgrade", "target_amount": "50000", "due_date": (ist_today() + timedelta(days=20)).isoformat()},
        follow_redirects=False,
    )
    assert r.status_code == 303
    seeded_db.expire_all()
    campaign = seeded_db.scalar(select(CorpusCampaign).where(CorpusCampaign.name == "Lift upgrade"))
    before = snapshot()

    page = owner.get("/payments/corpus")
    assert f'value="{flats["101"]}:{campaign.id}"' in page.text
    assert f'value="{flats["201"]}:{campaign.id}"' not in page.text
    assert "remaining ₹10000" in page.text

    r = owner.post(
        "/corpus/pay",
        data={"target": f"{flats['101']}:{campaign.id}", "amount": "4000", "paid_on": ist_today().isoformat(), "payment_mode": "UPI", "payment_note": "UTR123456789"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "msg=" in r.headers["location"], r.headers["location"]
    after = snapshot()

    assert after["cell"]["paid"] == before["cell"]["paid"] + 4000
    assert after["cell"]["pending_amount"] == before["cell"]["pending_amount"] - 4000
    assert after["row_pending"] == before["row_pending"] - 4000
    assert after["corpus_pending"] == before["corpus_pending"] - 4000
    assert after["grand"] == before["grand"] - 4000
    assert after["owed"] == before["owed"] - 4000 == after["flat"]["remaining"]
    assert after["flat"]["paid"] == before["flat"]["paid"] + 4000
    assert after["building"]["collected"] == before["building"]["collected"] + 4000
    assert after["building"]["pending"] == before["building"]["pending"] - 4000

    # The same payment on every corpus view.
    page = owner.get("/payments/corpus")
    assert "Lift upgrade" in page.text and "₹4000" in page.text and "remaining ₹6000" in page.text
    flat_page = owner.get(f"/flats/{flats['101']}")
    assert "Lift upgrade" in flat_page.text and "₹4000" in flat_page.text and "₹6000" in flat_page.text
    overview = owner.get("/corpus/campaigns")
    assert f"₹{after['building']['collected']}" in overview.text
    dash = owner.get("/")
    assert f"₹{after['cell']['paid']} / ₹{after['cell']['amount']}" in dash.text
    pdf_rows = pdf_report._flat_rows(seeded_db, None)
    pdf_101 = next(r for r in pdf_rows if r[0] == "101")
    assert pdf_101[5] == pdf_report.rs(after["owed"])

    # Owners cannot pay for a flat that is not theirs; overpaying is refused.
    assert owner.post(
        "/corpus/pay", data={"target": f"{flats['201']}:{campaign.id}", "amount": "100", "paid_on": ist_today().isoformat()},
        follow_redirects=False,
    ).status_code == 403
    too_much = owner.post(
        "/corpus/pay", data={"target": f"{flats['101']}:{campaign.id}", "amount": "6001", "paid_on": ist_today().isoformat()},
        follow_redirects=False,
    )
    assert "error=" in too_much.headers["location"]

    _close(seeded_db, campaign)


def test_absorb_line_items_into_general_corpus(seeded_db):
    flats = _flats(seeded_db)
    general_before = campaigns_svc.general_campaign(seeded_db)
    share_before = campaigns_svc.flat_share(seeded_db, general_before.id, flats["401"]).share_amount if general_before else 0
    paid_before = campaigns_svc.paid_by_flat(seeded_db, general_before.id).get(flats["401"], 0) if general_before else 0

    seeded_db.add_all([
        CorpusPayment(flat_id=flats["401"], amount=3000, is_paid=True, paid_on=date(2026, 9, 1), description="Old fund"),
        CorpusPayment(flat_id=flats["401"], amount=2000, is_paid=False, description="Old fund balance"),
    ])
    seeded_db.commit()
    assert campaigns_svc.absorb_line_items(seeded_db) == 2
    seeded_db.commit()
    seeded_db.expire_all()

    general = campaigns_svc.general_campaign(seeded_db)
    assert general.is_general and general.name == "General corpus"
    assert seeded_db.scalar(select(CorpusPayment).where(CorpusPayment.campaign_id.is_(None))) is None
    assert campaigns_svc.flat_share(seeded_db, general.id, flats["401"]).share_amount == share_before + 5000
    assert campaigns_svc.paid_by_flat(seeded_db, general.id).get(flats["401"], 0) == paid_before + 3000
    assert campaigns_svc.remaining_for(seeded_db, general, flats["401"]) == share_before - paid_before + 2000
    assert general.target_amount == sum(s.share_amount for s in general.shares)
    assert campaigns_svc.absorb_line_items(seeded_db) == 0

    for p in seeded_db.scalars(select(CorpusPayment).where(CorpusPayment.description == "Old fund")).all():
        seeded_db.delete(p)
    campaigns_svc.flat_share(seeded_db, general.id, flats["401"]).share_amount = share_before
    seeded_db.commit()


def test_owner_sees_only_own_rows_on_payment_tabs(admin_client, owner_client, seeded_db):
    flats = _flats(seeded_db)
    r = admin_client.post("/projects", data={"name": "Gate motor", "total_cost": "25000"}, follow_redirects=False)
    assert r.status_code == 303
    seeded_db.expire_all()
    project = seeded_db.scalar(select(Project).where(Project.name == "Gate motor"))
    first_period = seeded_db.scalars(select(Expense.period_id).where(Expense.period_id.isnot(None))).first()
    seeded_db.add_all([
        Expense(description="Bulb by 201", category="electrical", amount=150, expense_date=date(2026, 10, 2),
                paid_by_flat_id=flats["201"], period_id=first_period, submitted_by_user_id=_user_id(seeded_db, "asrk.sastry")),
        Expense(description="Tap by 301", category="plumbing", amount=250, expense_date=date(2026, 10, 3),
                paid_by_flat_id=flats["301"], period_id=first_period, submitted_by_user_id=_user_id(seeded_db, "admin")),
    ])
    seeded_db.commit()
    try:
        projects_tab = owner_client.get("/payments/projects").text
        assert "Gate motor" in projects_tab and "<td>201</td>" in projects_tab
        for other in ("101", "301", "401", "501"):
            assert f"<td>{other}</td>" not in projects_tab
        assert "Mark paid" not in projects_tab

        admin_tab = admin_client.get("/payments/projects").text
        assert all(f"<td>{n}</td>" in admin_tab for n in ("101", "201", "301", "401", "501"))
        assert "Mark paid" in admin_tab

        pp = seeded_db.scalar(select(ProjectPayment).where(ProjectPayment.project_id == project.id, ProjectPayment.flat_id == flats["301"]))
        r = admin_client.post(f"/projects/payments/{pp.id}/pay", data={"paid_on": "2026-10-05", "back": "/payments/projects"}, follow_redirects=False)
        assert r.headers["location"] == "/payments/projects"
        assert owner_client.post(f"/projects/payments/{pp.id}/pay", data={"paid_on": "2026-10-05"}, follow_redirects=False).status_code == 403

        detail = owner_client.get(f"/projects/{project.id}").text
        assert "<td>201</td>" in detail and "<td>301</td>" not in detail
        assert "₹5000" in detail  # building collected (301 paid) is visible as a total

        exp_tab = owner_client.get("/payments/expenses").text
        assert "Bulb by 201" in exp_tab and "Tap by 301" not in exp_tab
        assert 'action="/owner/expenses"' in exp_tab
        admin_exp = admin_client.get("/payments/expenses").text
        assert "Bulb by 201" in admin_exp and "Tap by 301" in admin_exp

        maint = owner_client.get("/payments/maintenance").text
        assert 'href="/payments/projects"' in maint and 'aria-current="page"' in maint

        dash = owner_client.get("/").text
        assert f'href="/flats/{flats["301"]}"' not in dash
    finally:
        seeded_db.expire_all()
        for e in seeded_db.scalars(select(Expense).where(Expense.description.in_(["Bulb by 201", "Tap by 301"]))).all():
            seeded_db.delete(e)
        project = seeded_db.get(Project, project.id)
        for pc in seeded_db.scalars(select(ProjectContribution).where(ProjectContribution.project_id == project.id)).all():
            seeded_db.delete(pc)
        seeded_db.flush()
        for pp in seeded_db.scalars(select(ProjectPayment).where(ProjectPayment.project_id == project.id)).all():
            seeded_db.delete(pp)
        seeded_db.delete(project)
        seeded_db.commit()


def _user_id(db, username):
    from app.services import users as users_svc

    return users_svc.get_by_username(db, username).id


def test_overview_pages_totals(admin_client, owner_client, seeded_db):
    r = admin_client.post("/projects", data={"name": "Roof paint", "total_cost": "10000"}, follow_redirects=False)
    assert r.status_code == 303
    seeded_db.expire_all()
    project = seeded_db.scalar(select(Project).where(Project.name == "Roof paint"))
    pays = seeded_db.scalars(select(ProjectPayment).where(ProjectPayment.project_id == project.id)).all()
    projects_svc.mark_paid(seeded_db, pays[0], date(2026, 10, 1))
    seeded_db.commit()
    try:
        for client in (admin_client, owner_client):
            page = client.get("/projects").text
            assert "Roof paint" in page and "1 / 5" in page and "20%" in page
        building = campaigns_svc.building_totals(seeded_db)
        for client in (admin_client, owner_client):
            if client is admin_client and not building["target"]:
                continue  # admin sees no cards at all until a corpus goal exists (v7.1)
            page = client.get("/corpus/campaigns").text
            assert f"₹{building['target']}" in page and f"₹{building['collected']}" in page and f"₹{building['pending']}" in page
        live = seeded_db.scalars(select(Expense).where(Expense.deleted_at.is_(None))).all()
        all_total = sum(e.amount for e in live)
        all_pending = sum(e.amount for e in live if e.paid_by_flat_id and not e.is_settled)
        exp_page = owner_client.get("/expenses").text
        assert f"₹{all_total} · ₹{all_pending}" in exp_page
    finally:
        for pc in seeded_db.scalars(select(ProjectContribution).where(ProjectContribution.project_id == project.id)).all():
            seeded_db.delete(pc)
        seeded_db.flush()
        for pp in pays:
            seeded_db.delete(pp)
        seeded_db.delete(project)
        seeded_db.commit()


def test_old_urls_redirect_and_title(admin_client, owner_client):
    for client in (admin_client, owner_client):
        assert client.get("/corpus/campaigns", follow_redirects=False).headers["location"] == "/corpus"
        assert client.get("/corpus/campaigns/7", follow_redirects=False).headers["location"] == "/corpus/goals/7"
        assert client.get("/payments", follow_redirects=False).headers["location"] == "/payments/maintenance"
        assert client.get("/maintenance", follow_redirects=False).headers["location"] == "/payments/maintenance"
        page = client.get("/payments/corpus")
        assert page.status_code == 200
        assert "<title>Payments · Corpus — Sri Krishna Mani Flat Owners Portal</title>" in page.text
        assert '<h1>Sri Krishna Mani Flat Owners Portal</h1>' in page.text
        assert 'href="/payments/maintenance" class="active" aria-current="page">Payments</a>' in page.text
        corpus = client.get("/corpus").text
        assert "<h2>Corpus</h2>" in corpus
    from fastapi.testclient import TestClient

    from app.main import app

    login = TestClient(app).get("/login").text
    assert "Sri Krishna Mani Flat Owners Portal" in login


def test_general_corpus_migration_on_legacy_db(tmp_path):
    """Upgrade + downgrade of the General corpus migration on a throwaway DB with legacy line items."""
    db_file = tmp_path / "legacy.db"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db_file}",
        "BACKUP_DIR": str(tmp_path / "backups"),
        "INSTANCE_DIR": str(tmp_path / "instance"),
    }

    def alembic(*args):
        res = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        return res

    alembic("upgrade", "f3a8d1c6b2e9")
    con = sqlite3.connect(db_file)
    flat_cols = {r[1]: r for r in con.execute("PRAGMA table_info(flats)")}
    for n in ("101", "201"):
        cols = ["number"] + [c for c, info in flat_cols.items() if info[3] and info[4] is None and c not in ("id", "number")]
        con.execute(f"INSERT INTO flats ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [n] + [""] * (len(cols) - 1))
    f101, f102 = (con.execute("SELECT id FROM flats WHERE number=?", (n,)).fetchone()[0] for n in ("101", "201"))
    con.executemany(
        "INSERT INTO corpus_payments (flat_id, amount, is_paid, paid_on, description) VALUES (?, ?, ?, ?, ?)",
        [(f101, 5000, 1, "2026-09-01", "Fund A"), (f101, 3000, 0, None, "Fund B"), (f102, 2000, 0, None, "Fund A")],
    )
    con.commit()
    con.close()

    out = alembic("upgrade", "head")
    assert list((tmp_path / "backups").glob("*pre-general-corpus.db")), out.stdout + out.stderr

    con = sqlite3.connect(db_file)
    gid, target = con.execute("SELECT id, target_amount FROM corpus_campaigns WHERE is_general=1").fetchone()
    shares = dict(con.execute("SELECT flat_id, share_amount FROM corpus_campaign_shares WHERE campaign_id=?", (gid,)))
    assert shares == {f101: 8000, f102: 2000} and target == 10000
    assert con.execute("SELECT COUNT(*) FROM corpus_payments WHERE campaign_id IS NULL").fetchone()[0] == 0
    assert con.execute("SELECT flat_id, amount FROM corpus_payments WHERE campaign_id=?", (gid,)).fetchall() == [(f101, 5000)]
    con.close()

    alembic("downgrade", "f3a8d1c6b2e9")
    con = sqlite3.connect(db_file)
    rows = sorted(con.execute("SELECT flat_id, amount, is_paid FROM corpus_payments WHERE campaign_id IS NULL").fetchall())
    assert rows == sorted([(f101, 5000, 1), (f101, 3000, 0), (f102, 2000, 0)])
    assert con.execute("SELECT COUNT(*) FROM corpus_campaigns").fetchone()[0] == 0
    con.close()
