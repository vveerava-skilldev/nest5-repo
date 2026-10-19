"""Owner view: menu order, Payments tabs, building-wide view toggle, insights, flat PDF."""
import re
from datetime import date

from sqlalchemy import select

from app.models import Flat, LedgerPeriod, MaintenancePayment
from app.services import app_settings as app_settings_svc
from app.services import insights as insights_svc
from tests.conftest import OWNER_PASSWORD, make_client


def _flats(db):
    return {f.number: f.id for f in db.scalars(select(Flat)).all()}


def _menu(html):
    nav = html.split('<nav class="sidebar">', 1)[1].split("</nav>", 1)[0]
    return [re.sub(r"\s+", " ", label).strip() for label in re.findall(r"<a href=\"[^\"]+\"[^>]*>([^<]+)</a>", nav)]


def _set_building_view(db, on):
    app_settings_svc.set_bool(db, app_settings_svc.OWNER_BUILDING_VIEW, on, updated_by="test")
    db.commit()


def test_owner_menu_order(owner_client):
    menu = _menu(owner_client.get("/").text)
    assert menu[:7] == ["Dashboard", "My flat 102", "Payments", "Complaints", "Asset schedule", "Calendar",
                        "My submissions"]
    assert "Expenses" not in menu and "Projects" not in menu and "Corpus" not in menu


def test_multi_flat_owner_menu(seeded_db, set_password):
    set_password(seeded_db, "manikyamba", OWNER_PASSWORD)
    menu = _menu(make_client("manikyamba", OWNER_PASSWORD).get("/").text)
    assert menu[:8] == ["Dashboard", "My flat 101", "My flat 401", "Payments", "Complaints",
                        "Asset schedule", "Calendar", "My submissions"]


def test_admin_menu_unchanged(admin_client):
    menu = _menu(admin_client.get("/").text)
    assert menu[:9] == ["Dashboard", "Payments", "Expenses", "Corpus", "Projects", "Asset schedule",
                        "Calendar", "Complaints", "Flats"]
    assert "Settlements" in menu and "Users" in menu


def test_owner_payment_tabs(owner_client, admin_client):
    for tab in ("maintenance", "corpus", "projects", "expenses"):
        r = owner_client.get(f"/payments/{tab}")
        assert r.status_code == 200, tab
        assert ">Maintenance</a>" in r.text and ">Corpus</a>" in r.text and ">Projects</a>" in r.text
        assert ">Expenses</a>" in r.text and "Owner expenses" not in r.text
    assert "Owner expenses" in admin_client.get("/payments/expenses").text
    maint = owner_client.get("/payments/maintenance").text
    assert "<td>102</td>" in maint or ">102<" in maint
    assert ">101<" not in maint and ">301<" not in maint


def test_building_view_toggle(seeded_db, owner_client, admin_client):
    try:
        assert "View payments →" in owner_client.get("/payments/maintenance").text
        page = owner_client.get("/payments/building")
        assert page.status_code == 200
        for number in ("101", "102", "301", "401", "501"):
            assert f"<td>{number}</td>" in page.text
        assert 'action="/admin/settings/owner-building-view"' not in page.text
        assert 'action="/admin/settings/owner-building-view"' in admin_client.get("/payments/building").text

        assert owner_client.post("/admin/settings/owner-building-view", data={"enabled": "0"},
                                 follow_redirects=False).status_code == 403
        r = admin_client.post("/admin/settings/owner-building-view", data={"enabled": "0"}, follow_redirects=False)
        assert r.status_code == 303 and "turned+off" in r.headers["location"]
        seeded_db.expire_all()
        assert app_settings_svc.get_bool(seeded_db, app_settings_svc.OWNER_BUILDING_VIEW) is False

        assert "View payments →" not in owner_client.get("/payments/maintenance").text
        assert owner_client.get("/payments/building").status_code == 403
        assert admin_client.get("/payments/building").status_code == 200
        assert "View payments →" in admin_client.get("/payments/maintenance").text

        admin_client.post("/admin/settings/owner-building-view", data={"enabled": "1"})
        assert owner_client.get("/payments/building").status_code == 200
    finally:
        _set_building_view(seeded_db, True)


def test_insights_data_stays_but_page_has_no_charts(seeded_db, owner_client):
    flats = _flats(seeded_db)
    data = insights_svc.flat_insights(seeded_db, [flats["102"]])
    paid = sum(mp.amount for mp in seeded_db.scalars(
        select(MaintenancePayment).where(MaintenancePayment.flat_id == flats["102"],
                                         MaintenancePayment.is_paid.is_(True))).all()
        if mp.paid_on and (date.today().year * 12 + date.today().month) - (mp.paid_on.year * 12 + mp.paid_on.month) < 12)
    assert sum(data["trend"]["maintenance"]) == paid
    assert len(data["status"]["labels"]) == len(data["status"]["paid"]) == len(data["status"]["pending"])

    page = owner_client.get(f"/flats/{flats['102']}").text
    # v9.3: the charts are switched off for every user for now (the data service stays).
    assert "Insights" not in page and '<svg class="chart"' not in page
    assert "Payments made per month" not in page and "Maintenance paid vs pending" not in page
    assert 'action="/flats/%d/statement.pdf"' % flats["102"] in page


def test_flat_statement_pdf(seeded_db, owner_client, admin_client, set_password):
    flats = _flats(seeded_db)
    r = owner_client.get(f"/flats/{flats['102']}/statement.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and "all-time" in r.headers["content-disposition"]
    period = seeded_db.scalars(select(LedgerPeriod).order_by(LedgerPeriod.year.desc(), LedgerPeriod.month.desc())).first()
    r = owner_client.get(f"/flats/{flats['102']}/statement.pdf?period_id={period.id}")
    assert r.status_code == 200 and f"{period.year}-{period.month:02d}" in r.headers["content-disposition"]
    assert owner_client.get(f"/flats/{flats['101']}/statement.pdf").status_code == 403
    assert admin_client.get(f"/flats/{flats['101']}/statement.pdf").status_code == 200

    r = owner_client.get("/owner/statement.pdf")
    assert r.status_code == 200 and "flats-102-" in r.headers["content-disposition"]
    set_password(seeded_db, "manikyamba", OWNER_PASSWORD)
    multi = make_client("manikyamba", OWNER_PASSWORD)
    r = multi.get("/owner/statement.pdf")
    assert r.status_code == 200 and "flats-101-401-" in r.headers["content-disposition"]
    assert "All my flats" in multi.get(f"/flats/{flats['101']}").text


def test_owner_statement_page_only_own_flats(seeded_db, owner_client, admin_client):
    period = seeded_db.scalars(select(LedgerPeriod).order_by(LedgerPeriod.year.desc(), LedgerPeriod.month.desc())).first()
    mine = owner_client.get(f"/statements/{period.id}").text
    assert "<td>102</td>" in mine and "<td>101</td>" not in mine and "<td>501</td>" not in mine
    assert "<td>101</td>" in admin_client.get(f"/statements/{period.id}").text
