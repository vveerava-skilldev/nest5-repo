"""v9 (simplified in v7.3): editable amount and part-payment hint in the Pay now sheet; PhonePe-only link; version label."""
import html
import itertools
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.config import APP_VERSION, settings
from app.models import CorpusPayment, ProjectPayment
from app.services import upi as upi_svc
from tests.test_v7_simple_dashboard import _app_href, _flat_id, _goal, _project, _unpay

UTR = "412345678901"


_seq = itertools.count(1)


def _project_sheet(admin_client, owner_client, db, total="210000"):
    # The test database persists across tests, so every test gets its own project.
    project = _project(admin_client, db, f"Water softner v9 #{next(_seq)}", total)
    own = _flat_id(db, "201")
    pp = db.scalar(select(ProjectPayment).where(ProjectPayment.project_id == project.id, ProjectPayment.flat_id == own))
    sheet = owner_client.get(f"/?view=simple&pay=p-{pp.id}").text
    return pp, sheet


def _form(sheet):
    return re.search(r'<form class="sv-sheet"[^>]*>', sheet).group(0)


def _form_amount(sheet):
    return re.search(r'<input type="number" name="amount"[^>]*>', sheet).group(0)


def test_project_sheet_has_one_editable_amount_and_hint(seeded_db, admin_client, owner_client):
    pp, sheet = _project_sheet(admin_client, owner_client, seeded_db)
    form = _form_amount(sheet)
    assert f'value="{pp.remaining}"' in form and f'max="{pp.remaining}"' in form and "readonly" not in form
    assert sheet.count('name="amount"') == 1  # the old second (top) amount box and its copy button are gone
    assert 'id="amt-hint"' in sheet and 'id="amt-error"' in sheet and "Pays this off in full" in sheet
    assert "pay-amount-top" not in sheet and "data-copy-target" not in sheet and 'id="copy-amount-text"' not in sheet


def test_partial_hint_data_attributes_for_project(seeded_db, admin_client, owner_client):
    pp, sheet = _project_sheet(admin_client, owner_client, seeded_db)
    form = _form(sheet)
    assert 'data-partial="1"' in form and f'data-remaining="{pp.remaining}"' in form
    assert f'data-total="{pp.amount}"' in form and 'data-paid="0"' in form


def test_hint_data_includes_already_paid_for_project_and_corpus(seeded_db, admin_client, owner_client):
    pp, _ = _project_sheet(admin_client, owner_client, seeded_db)
    r = owner_client.post("/owner/projects/pay", data={"share_id": pp.id, "amount": "200", "paid_on": "2026-10-06",
                                                       "payment_mode": "Cash"}, follow_redirects=False)
    assert "owner_msg=" in r.headers["location"]
    seeded_db.expire_all()
    pp = seeded_db.get(ProjectPayment, pp.id)
    form = _form(owner_client.get(f"/?view=simple&pay=p-{pp.id}").text)
    assert f'data-remaining="{pp.remaining}"' in form and f'data-total="{pp.amount}"' in form and 'data-paid="200"' in form

    own = _flat_id(seeded_db, "201")
    goal = _goal(admin_client, seeded_db, "Lift v9 hint", 50000)
    share = next(s.share_amount for s in goal.shares if s.flat_id == own)
    owner_client.post("/corpus/pay", data={"target": f"{own}:{goal.id}", "amount": "300", "paid_on": "2026-10-06",
                                           "payment_mode": "Cash"}, follow_redirects=False)
    form = _form(owner_client.get(f"/?view=simple&pay=c-{goal.id}-{own}").text)
    assert f'data-total="{share}"' in form and 'data-paid="300"' in form and f'data-remaining="{share - 300}"' in form


def test_maintenance_stays_fixed_and_non_editable(seeded_db, owner_client):
    mp = _unpay(seeded_db, "201")
    sheet = owner_client.get(f"/?view=simple&period_id={mp.period_id}&pay=m-{mp.period_id}-{mp.flat_id}").text
    assert 'id="amt-hint"' not in sheet and "data-partial" not in sheet
    assert f"₹{mp.amount}" in sheet  # the short hint line under the title still states the amount due
    assert "readonly" in _form_amount(sheet)


def test_payee_shown_in_order_as_plain_text_and_launch_only_phonepe(seeded_db, admin_client, owner_client):
    pp, sheet = _project_sheet(admin_client, owner_client, seeded_db)
    assert sheet.count("data-open-app=") == 1 and ">Open PhonePe</a>" in sheet
    assert _app_href(sheet, "phonepe") == "phonepe://"
    for gone in (">Google Pay<", ">Paytm<", "Other UPI app", "tez://", "paytmmp://", "phonepe://pay", 'href="upi://', "data-upi-app"):
        assert gone not in sheet
    vpa, mobile = settings.upi_vpa, settings.upi_mobile
    assert mobile == "9940255076"
    i_vpa = sheet.index(f'<dd id="pay-vpa">{vpa}</dd>')
    i_mobile = sheet.index(f'<dd id="pay-mobile">{mobile}</dd>')
    i_payee = sheet.index(f'id="pay-payee">{settings.upi_payee_name}<')
    assert i_vpa < i_mobile < i_payee
    for gone in ("Copy", "Show QR", "Scan from gallery", "To UPI ID", "PhonePe cannot take the amount"):
        assert gone not in sheet, gone


def test_sheet_values_come_from_the_database_not_the_config(seeded_db, owner_client, monkeypatch):
    monkeypatch.setattr(settings, "upi_mobile", "9876543210")  # the config is only the initial seed
    mp = _unpay(seeded_db, "201")
    sheet = owner_client.get(f"/?view=simple&period_id={mp.period_id}&pay=m-{mp.period_id}-{mp.flat_id}").text
    assert '<dd id="pay-mobile">9940255076</dd>' in sheet and "9876543210" not in sheet


def test_script_validates_amount_and_hint_without_copy_or_qr_code(owner_client):
    js = owner_client.get("/static/js/paynow.js").text
    for needle in ('amount.addEventListener("input"', "Pays this off in full", "Remaining after this payment",
                   "submit.disabled", "en-IN"):
        assert needle in js, needle
    for gone in ("pay-amount-top", "upi-qr", "clipboard", "sv-copy"):
        assert gone not in js, gone


def test_qr_payload_follows_partial_amount(seeded_db, owner_client):
    own = _flat_id(seeded_db, "201")
    r = owner_client.get(f"/upi/qr.svg?flat_id={own}&amount=5000&note=Flat%20201%20Project%20X")
    assert r.status_code == 200 and "<svg" in r.text
    assert "am=5000.00" in upi_svc.build_link(5000, "Flat 201 Project X")


@pytest.mark.parametrize("bad", ["0", "-5", "", "abc", "5000.5"])
def test_server_rejects_non_positive_or_bad_amounts(seeded_db, admin_client, owner_client, bad):
    pp, _ = _project_sheet(admin_client, owner_client, seeded_db)
    r = owner_client.post("/owner/projects/pay", data={"share_id": pp.id, "amount": bad, "paid_on": "2026-10-06",
                                                       "payment_mode": "UPI", "payment_note": UTR}, follow_redirects=False)
    assert "owner_error=" in r.headers["location"]
    seeded_db.expire_all()
    assert seeded_db.get(ProjectPayment, pp.id).paid == 0


def test_server_rejects_amount_above_remaining_for_project_and_corpus(seeded_db, admin_client, owner_client):
    pp, _ = _project_sheet(admin_client, owner_client, seeded_db)
    r = owner_client.post("/owner/projects/pay", data={"share_id": pp.id, "amount": str(pp.remaining + 1), "paid_on": "2026-10-06",
                                                       "payment_mode": "UPI", "payment_note": UTR}, follow_redirects=False)
    assert "owner_error=" in r.headers["location"]
    seeded_db.expire_all()
    assert seeded_db.get(ProjectPayment, pp.id).paid == 0

    own = _flat_id(seeded_db, "201")
    goal = _goal(admin_client, seeded_db, "Lift v9 over", 50000)
    share = next(s.share_amount for s in goal.shares if s.flat_id == own)
    for amount in (share + 1, 0, -1):
        r = owner_client.post("/corpus/pay", data={"target": f"{own}:{goal.id}", "amount": str(amount), "paid_on": "2026-10-06",
                                                   "payment_mode": "UPI", "payment_note": UTR}, follow_redirects=False)
        assert "error=" in r.headers["location"]
    assert seeded_db.scalar(select(CorpusPayment).where(CorpusPayment.campaign_id == goal.id)) is None


def test_partial_then_exact_remaining_pays_off(seeded_db, admin_client, owner_client):
    pp, _ = _project_sheet(admin_client, owner_client, seeded_db)
    total = pp.remaining
    for amount in (5000, total - 5000):
        r = owner_client.post("/owner/projects/pay", data={"share_id": pp.id, "amount": str(amount), "paid_on": "2026-10-06",
                                                           "payment_mode": "UPI", "payment_note": UTR}, follow_redirects=False)
        assert "owner_msg=" in r.headers["location"], r.headers["location"]
    seeded_db.expire_all()
    assert seeded_db.get(ProjectPayment, pp.id).remaining == 0


def test_version_label_in_side_menu(seeded_db, owner_client, admin_client):
    assert APP_VERSION == "nest5-v7.5"
    for client in (owner_client, admin_client):
        page = client.get("/").text
        assert f'<p class="app-version">version: {APP_VERSION}</p>' in page
    assert "version:" not in html.unescape(owner_client.get("/login").text)
