"""v7: Simple / Detailed owner dashboard, saved default view, pay sheet, UPI link."""
import html
import os
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from sqlalchemy import select

from app.config import settings
from app.models import (
    CorpusPayment,
    Flat,
    LedgerPeriod,
    MaintenancePayment,
    Project,
    ProjectPayment,
    User,
)
from app.services import dashboard_view as dashboard_view_svc
from app.services import upi as upi_svc
from tests.conftest import OWNER_PASSWORD, make_client

ROOT = Path(__file__).resolve().parents[1]


def _flat_id(db, number):
    return db.scalar(select(Flat.id).where(Flat.number == number))


def _current_period(db):
    today = date.today()
    return db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == today.year, LedgerPeriod.month == today.month)) \
        or db.scalar(select(LedgerPeriod).order_by(LedgerPeriod.year.desc(), LedgerPeriod.month.desc()))


def _unpay(db, flat_number, period=None):
    period = period or _current_period(db)
    mp = db.scalar(select(MaintenancePayment).where(
        MaintenancePayment.period_id == period.id, MaintenancePayment.flat_id == _flat_id(db, flat_number)))
    mp.is_paid, mp.paid_on = False, None
    db.commit()
    return mp


def _goal(admin_client, db, name="Lift fund v7", target=50000):
    due = (date.today() + timedelta(days=15)).isoformat()
    r = admin_client.post("/corpus/campaigns", data={"name": name, "target_amount": str(target), "due_date": due},
                          follow_redirects=False)
    assert r.status_code == 303
    from app.models import CorpusCampaign
    db.expire_all()
    return db.scalar(select(CorpusCampaign).where(CorpusCampaign.name == name))


def _project(admin_client, db, name="Roof v7", total="25000"):
    r = admin_client.post("/projects", data={"name": name, "total_cost": total,
                                             "due_date": (date.today() + timedelta(days=5)).isoformat()},
                          follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    return db.scalar(select(Project).where(Project.name == name, Project.deleted_at.is_(None)))


def _hrefs(page: str) -> list[str]:
    return [html.unescape(h) for h in re.findall(r'class="sv-due" href="([^"]+)"', page)]


def _app_href(page: str, key: str) -> str:
    return html.unescape(re.search(rf'data-open-app="{key}" href="([^"]+)"', page).group(1))


def _fresh_owner(db, set_password, username="asrk.sastry"):
    set_password(db, username, OWNER_PASSWORD)
    return make_client(username, OWNER_PASSWORD)


# ---- toggle + default persistence ---------------------------------------------------------


def test_toggle_is_one_click_and_remembered(seeded_db, owner_client):
    first = owner_client.get("/")
    assert first.status_code == 200
    assert 'class="sv-seg"' in first.text and "Action board" in first.text  # Detailed = v6 dashboard
    simple = owner_client.get("/?view=simple")
    assert "Your dues to pay" in simple.text and "Action board" not in simple.text
    assert "Your dues to pay" in owner_client.get("/").text  # remembered for this login
    detailed = owner_client.get("/?view=detailed")
    assert "Action board" in detailed.text
    assert "Action board" in owner_client.get("/").text


def test_auto_small_screen_hint_only_without_saved_choice(seeded_db, owner_client):
    assert "max-width: 600px" in owner_client.get("/").text
    owner_client.get("/?view=detailed")
    assert "max-width: 600px" not in owner_client.get("/").text


def test_default_view_persists_per_user(seeded_db, owner_client, set_password):
    r = owner_client.post("/account/dashboard-view", data={"view": "simple", "make_default": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?view=simple"
    seeded_db.expire_all()
    assert seeded_db.scalar(select(User).where(User.username == "asrk.sastry")).dashboard_view == "simple"
    assert seeded_db.scalar(select(User).where(User.username == "pavan.kumar")).dashboard_view is None

    # A new login (new session) on another device starts in the saved default.
    again = make_client("asrk.sastry", OWNER_PASSWORD)
    page = again.get("/")
    assert "Your dues to pay" in page.text and "max-width: 600px" not in page.text
    assert 'name="make_default" value="1" checked' in page.text

    # Another owner is unaffected.
    other = _fresh_owner(seeded_db, set_password, "pavan.kumar")
    assert "Action board" in other.get("/").text

    # A one-off toggle does not change the saved default; unticking clears it.
    again.get("/?view=detailed")
    seeded_db.expire_all()
    assert seeded_db.scalar(select(User).where(User.username == "asrk.sastry")).dashboard_view == "simple"
    again.post("/account/dashboard-view", data={"view": "detailed"}, follow_redirects=False)
    seeded_db.expire_all()
    assert seeded_db.scalar(select(User).where(User.username == "asrk.sastry")).dashboard_view is None


def test_default_view_detailed_and_bad_value(seeded_db, owner_client):
    owner_client.post("/account/dashboard-view", data={"view": "detailed", "make_default": "1"})
    assert make_client("asrk.sastry", OWNER_PASSWORD).get("/").text.count("Action board") >= 1
    assert owner_client.post("/account/dashboard-view", data={"view": "weird"}).status_code == 400


def test_admin_keeps_current_dashboard(seeded_db, admin_client):
    page = admin_client.get("/?view=simple")
    assert "Action board" in page.text and "sv-seg" not in page.text and "Your dues to pay" not in page.text


def test_month_navigation_kept_in_simple_view(seeded_db, owner_client):
    page = owner_client.get("/?view=simple").text
    assert 'name="period_id"' in page and "sv-month" in page
    assert "Your dues to pay" in owner_client.get(f"/?view=simple&period_id={_current_period(seeded_db).id}").text


# ---- simple view access rules ---------------------------------------------------------------


def test_simple_view_shows_only_own_flats(seeded_db, admin_client, owner_client):
    _unpay(seeded_db, "201")
    _unpay(seeded_db, "301")
    goal = _goal(admin_client, seeded_db)
    project = _project(admin_client, seeded_db)
    page = owner_client.get("/?view=simple&more=1").text
    assert "Flat 201" in page
    for other in ("101", "301", "401", "501"):
        assert f"Flat {other}" not in page
    for name in ("Venkata", "Pavan", "Manikyamba"):
        assert name not in page
    assert "Maintenance" in page and goal.name in page and project.name in page
    # Every pay link points at a due of this owner's flat only.
    own = _flat_id(seeded_db, "201")
    for href in _hrefs(page):
        key = parse_qs(urlsplit(href).query)["pay"][0]
        if key.startswith("m-"):
            assert key.endswith(f"-{own}")
        if key.startswith("c-"):
            assert key.endswith(f"-{own}")


def test_owner_cannot_open_pay_sheet_or_pay_for_other_flat(seeded_db, admin_client, owner_client):
    period = _current_period(seeded_db)
    _unpay(seeded_db, "301")
    other = _flat_id(seeded_db, "301")
    page = owner_client.get(f"/?view=simple&pay=m-{period.id}-{other}").text
    assert "sv-sheet" not in page and "Flat 301" not in page
    r = owner_client.post("/owner/maintenance", data={
        "flat_id": other, "period_id": period.id, "paid_on": date.today().isoformat(),
        "back": "/?view=simple"}, follow_redirects=False)
    assert r.status_code == 403
    assert owner_client.get(f"/upi/qr.svg?flat_id={other}&amount=100&note=x").status_code == 403
    assert owner_client.get(f"/upi/qr.svg?flat_id={_flat_id(seeded_db, '201')}&amount=100&note=x").status_code == 200


def test_owner_without_flats_sees_message(seeded_db, set_password):
    from app.services import users as users_svc
    user = users_svc.get_by_username(seeded_db, "pavan.kumar")
    user.flats = []
    seeded_db.commit()
    client = _fresh_owner(seeded_db, set_password, "pavan.kumar")
    page = client.get("/?view=simple").text
    assert "No flat is linked" in page
    user.flats = [seeded_db.get(Flat, _flat_id(seeded_db, "501"))]
    seeded_db.commit()


def test_simple_view_requires_login(seeded_db):
    from fastapi.testclient import TestClient
    from app.main import app
    r = TestClient(app).get("/?view=simple", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_more_link_when_many_dues(seeded_db, admin_client, set_password):
    _unpay(seeded_db, "101")
    _unpay(seeded_db, "401")
    for i in range(3):
        _project(admin_client, seeded_db, name=f"Many dues {i}", total="1000")
    client = _fresh_owner(seeded_db, set_password, "manikyamba")
    page = client.get("/?view=simple").text
    assert len(_hrefs(page)) == dashboard_view_svc.VISIBLE_DUES
    assert "more dues" in page
    full = client.get("/?view=simple&more=1").text
    assert len(_hrefs(full)) > dashboard_view_svc.VISIBLE_DUES and "Show fewer" in full


# ---- pay sheet -> existing owner-submission flow ---------------------------------------------


def test_pay_maintenance_through_sheet(seeded_db, owner_client):
    mp = _unpay(seeded_db, "201")
    page = owner_client.get(f"/?view=simple&period_id={mp.period_id}&more=1").text
    href = next(h for h in _hrefs(page) if f"pay=m-{mp.period_id}-{mp.flat_id}" in h)
    sheet = owner_client.get(href).text
    assert "sv-sheet" in sheet and 'action="/owner/maintenance"' in sheet
    assert f'name="flat_id" value="{mp.flat_id}"' in sheet and f'value="{mp.amount}"' in sheet
    assert f'name="paid_on" value="{date.today().isoformat()}"' in sheet and 'name="payment_note"' in sheet
    assert "nothing is marked paid until then" in sheet
    r = owner_client.post("/owner/maintenance", data={
        "flat_id": mp.flat_id, "period_id": mp.period_id, "paid_on": "2026-10-05", "amount": str(mp.amount),
        "payment_mode": "UPI", "payment_note": "UTR123456789012", "back": f"/?view=simple&period_id={mp.period_id}"},
        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/?view=simple&period_id={mp.period_id}&owner_msg=")
    seeded_db.expire_all()
    row = seeded_db.get(MaintenancePayment, mp.id)
    assert row.is_paid and row.payment_note == "UTR123456789012" and row.payment_mode == "UPI"
    assert row.submitted_by_user_id is not None  # "Submitted by owner" -> admin sees/cancels as before
    assert f"pay=m-{mp.period_id}-{mp.flat_id}" not in owner_client.get(r.headers["location"]).text


def test_pay_corpus_part_through_sheet(seeded_db, admin_client, owner_client):
    goal = _goal(admin_client, seeded_db, "Lift part v7", 50000)
    own = _flat_id(seeded_db, "201")
    key = f"c-{goal.id}-{own}"
    sheet = owner_client.get(f"/?view=simple&pay={key}").text
    assert 'action="/corpus/pay"' in sheet and f'value="{own}:{goal.id}"' in sheet and "part payment allowed" in sheet
    r = owner_client.post("/corpus/pay", data={
        "target": f"{own}:{goal.id}", "amount": "3000", "paid_on": "2026-10-06", "payment_mode": "UPI",
        "payment_note": "REF77123456", "back": "/?view=simple"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/?view=simple&msg=")
    seeded_db.expire_all()
    cp = seeded_db.scalar(select(CorpusPayment).where(CorpusPayment.campaign_id == goal.id, CorpusPayment.flat_id == own))
    assert cp.amount == 3000 and cp.payment_note == "REF77123456" and cp.submitted_by_user_id is not None
    again = owner_client.get(f"/?view=simple&pay={key}").text
    assert 'value="7000"' in again  # remaining after the part payment
    over = owner_client.post("/corpus/pay", data={
        "target": f"{own}:{goal.id}", "amount": "999999", "paid_on": "2026-10-06", "back": "/?view=simple"},
        follow_redirects=False)
    assert "error=" in over.headers["location"]


def test_pay_project_part_through_sheet(seeded_db, admin_client, owner_client):
    project = _project(admin_client, seeded_db, "Roof pay v7", "10000")
    own = _flat_id(seeded_db, "201")
    pp = seeded_db.scalar(select(ProjectPayment).where(ProjectPayment.project_id == project.id, ProjectPayment.flat_id == own))
    sheet = owner_client.get(f"/?view=simple&pay=p-{pp.id}").text
    assert 'action="/owner/projects/pay"' in sheet and f'name="share_id" value="{pp.id}"' in sheet
    r = owner_client.post("/owner/projects/pay", data={
        "share_id": pp.id, "amount": "500", "paid_on": "2026-10-06", "payment_mode": "UPI",
        "payment_note": "PREF12345678", "back": "/?view=simple"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/?view=simple&owner_msg=")
    seeded_db.expire_all()
    assert seeded_db.get(ProjectPayment, pp.id).paid == 500


def test_back_address_is_restricted(seeded_db, owner_client):
    mp = _unpay(seeded_db, "201")
    r = owner_client.post("/owner/maintenance", data={
        "flat_id": mp.flat_id, "period_id": mp.period_id, "paid_on": "2026-10-05",
        "back": "https://evil.example/"}, follow_redirects=False)
    assert r.headers["location"].startswith(f"/payments/maintenance?period_id={mp.period_id}")
    assert dashboard_view_svc.safe_back("/?view=simple&period_id=3", "/x") == "/?view=simple&period_id=3"
    for bad in ("//evil.example", "/?view=simple&x=1", "http://x", ""):
        assert dashboard_view_svc.safe_back(bad, "/x") == "/x"


# ---- UPI link + QR ---------------------------------------------------------------------------


def test_upi_link_encoding_amount_and_vpa():
    link = upi_svc.build_link(2000, "Flat 101 Maintenance Oct-2026")
    assert link == (
        "upi://pay?pa=9940255076@ybl&pn=Sri%20Krishna%20Mani%20Flats&am=2000.00&cu=INR"
        "&tn=Flat%20101%20Maintenance%20Oct-2026"
    )
    parts = parse_qs(urlsplit(link).query)
    assert parts["pa"] == ["9940255076@ybl"] and parts["am"] == ["2000.00"] and parts["cu"] == ["INR"]
    tricky = upi_svc.build_link(1250, "Flat 5 & co: 100% #1", vpa="x@okaxis", payee="A & B Flats")
    assert "&co" not in tricky and "%26" in tricky and "%25" in tricky and "%23" in tricky
    q = parse_qs(urlsplit(tricky).query)
    assert q["tn"] == ["Flat 5 & co: 100% #1"] and q["pn"] == ["A & B Flats"] and q["pa"] == ["x@okaxis"]
    assert len(parse_qs(urlsplit(upi_svc.build_link(1, "x" * 200)).query)["tn"][0]) == upi_svc.NOTE_MAX


def test_upi_settings_defaults_and_override(monkeypatch):
    assert settings.upi_vpa == "9940255076@ybl" and settings.upi_payee_name == "Sri Krishna Mani Flats"
    monkeypatch.setattr(settings, "upi_vpa", "pay@upi")
    monkeypatch.setattr(settings, "upi_payee_name", "Test Payee")
    q = parse_qs(urlsplit(upi_svc.build_link(10, "n")).query)
    assert q["pa"] == ["pay@upi"] and q["pn"] == ["Test Payee"]


def test_sheet_has_pay_now_block_without_qr_for_the_due(seeded_db, owner_client):
    mp = _unpay(seeded_db, "201")
    sheet = owner_client.get(f"/?view=simple&pay=m-{mp.period_id}-{mp.flat_id}").text
    assert _app_href(sheet, "phonepe") == "phonepe://"  # launch-only: PhonePe declines prefilled pay links
    assert 'id="upi-qr"' not in sheet and "/upi/qr.svg" not in sheet and "Show QR" not in sheet  # v7.3: no QR on the sheet
    # The list row opens this Pay now sheet; the old direct upi:// link is gone from the list.
    rows = owner_client.get(f"/?view=simple&period_id={mp.period_id}&more=1").text
    assert 'class="sv-upi"' in rows and ">Pay now<" in rows and 'class="sv-upi" href="upi://' not in rows


def test_qr_endpoint_returns_svg_of_same_link(seeded_db, owner_client):
    own = _flat_id(seeded_db, "201")
    r = owner_client.get(f"/upi/qr.svg?flat_id={own}&amount=2000&note=Flat%20201%20Maintenance")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert r.text.lstrip().startswith("<?xml") and "<svg" in r.text and "<path" in r.text
    assert owner_client.get(f"/upi/qr.svg?flat_id={own}&amount=0&note=x").status_code == 400
    assert owner_client.get(f"/upi/qr.svg?flat_id={own}&amount=-5&note=x").status_code == 400


# ---- migration ------------------------------------------------------------------------------


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)


def _user_columns(db_path: Path) -> set[str]:
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(users)")}


def test_migration_from_scratch_and_roundtrip(tmp_path):
    db_path = tmp_path / "fresh.db"
    done = _alembic(db_path, "upgrade", "head")
    assert done.returncode == 0, done.stderr
    assert "dashboard_view" in _user_columns(db_path)
    down = _alembic(db_path, "downgrade", "f2c6a8d4b301")
    assert down.returncode == 0, down.stderr
    assert "dashboard_view" not in _user_columns(db_path)
    assert _alembic(db_path, "upgrade", "head").returncode == 0
    assert "dashboard_view" in _user_columns(db_path)


def test_migration_tolerates_column_added_by_startup_safety_net(tmp_path):
    db_path = tmp_path / "pre.db"
    assert _alembic(db_path, "upgrade", "f2c6a8d4b301").returncode == 0
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE users ADD COLUMN dashboard_view VARCHAR(10)")
    done = _alembic(db_path, "upgrade", "head")
    assert done.returncode == 0, done.stderr
