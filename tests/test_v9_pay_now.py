"""v9: editable, synced amount in the Pay now sheet; part-payment hint data; PhonePe-only link; version label."""
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
    own = _flat_id(db, "102")
    pp = db.scalar(select(ProjectPayment).where(ProjectPayment.project_id == project.id, ProjectPayment.flat_id == own))
    sheet = owner_client.get(f"/?view=simple&pay=p-{pp.id}").text
    return pp, sheet


def _section(sheet):
    return re.search(r'<section class="sv-paynow"[^>]*>', sheet).group(0)


def _form_amount(sheet):
    return re.search(r'<input type="number" name="amount"[^>]*>', sheet).group(0)


def test_project_sheet_has_editable_top_amount_synced_with_form_field(seeded_db, admin_client, owner_client):
    pp, sheet = _project_sheet(admin_client, owner_client, seeded_db)
    top = re.search(r'<input type="text" id="pay-amount-top"[^>]*>', sheet).group(0)
    assert f'value="{pp.remaining}"' in top and 'inputmode="decimal"' in top and "name=" not in top  # never submitted twice
    form = _form_amount(sheet)
    assert f'value="{pp.remaining}"' in form and f'max="{pp.remaining}"' in form and "readonly" not in form
    assert 'id="amt-hint"' in sheet and 'id="amt-error"' in sheet and "Pays this off in full" in sheet
    assert 'data-copy-target="amount"' in sheet
    assert 'id="copy-amount-text"' not in sheet


def test_partial_hint_data_attributes_for_project(seeded_db, admin_client, owner_client):
    pp, sheet = _project_sheet(admin_client, owner_client, seeded_db)
    section = _section(sheet)
    assert 'data-partial="1"' in section and f'data-remaining="{pp.remaining}"' in section
    assert f'data-total="{pp.amount}"' in section and 'data-paid="0"' in section


def test_hint_data_includes_already_paid_for_project_and_corpus(seeded_db, admin_client, owner_client):
    pp, _ = _project_sheet(admin_client, owner_client, seeded_db)
    r = owner_client.post("/owner/projects/pay", data={"share_id": pp.id, "amount": "200", "paid_on": "2026-10-06",
                                                       "payment_mode": "Cash"}, follow_redirects=False)
    assert "owner_msg=" in r.headers["location"]
    seeded_db.expire_all()
    pp = seeded_db.get(ProjectPayment, pp.id)
    section = _section(owner_client.get(f"/?view=simple&pay=p-{pp.id}").text)
    assert f'data-remaining="{pp.remaining}"' in section and f'data-total="{pp.amount}"' in section and 'data-paid="200"' in section

    own = _flat_id(seeded_db, "102")
    goal = _goal(admin_client, seeded_db, "Lift v9 hint", 50000)
    share = next(s.share_amount for s in goal.shares if s.flat_id == own)
    owner_client.post("/corpus/pay", data={"target": f"{own}:{goal.id}", "amount": "300", "paid_on": "2026-10-06",
                                           "payment_mode": "Cash"}, follow_redirects=False)
    section = _section(owner_client.get(f"/?view=simple&pay=c-{goal.id}-{own}").text)
    assert f'data-total="{share}"' in section and 'data-paid="300"' in section and f'data-remaining="{share - 300}"' in section


def test_maintenance_stays_fixed_and_non_editable(seeded_db, owner_client):
    mp = _unpay(seeded_db, "102")
    sheet = owner_client.get(f"/?view=simple&period_id={mp.period_id}&pay=m-{mp.period_id}-{mp.flat_id}").text
    assert "pay-amount-top" not in sheet and 'id="amt-hint"' not in sheet and "data-partial" not in sheet
    assert f'<strong id="copy-amount-text">₹{mp.amount}</strong>' in sheet
    assert "readonly" in _form_amount(sheet)


def test_three_reliable_ways_in_order_and_launch_only_phonepe(seeded_db, admin_client, owner_client):
    pp, sheet = _project_sheet(admin_client, owner_client, seeded_db)
    assert sheet.count("data-open-app=") == 1 and ">Open PhonePe</a>" in sheet
    assert _app_href(sheet, "phonepe") == "phonepe://"
    for gone in (">Google Pay<", ">Paytm<", "Other UPI app", "tez://", "paytmmp://", "phonepe://pay", 'href="upi://', "data-upi-app"):
        assert gone not in sheet
    vpa, mobile = settings.upi_vpa, settings.upi_mobile
    assert mobile == "9940255076"
    i_vpa = sheet.index(f'data-copy-text="{vpa}"')
    i_mobile = sheet.index(f'data-copy-text="{mobile}"')
    i_qr = sheet.index("<summary>Show QR</summary>")
    assert i_vpa < i_mobile < i_qr
    assert "or send to mobile number" in sheet and f'id="copy-mobile-text">{mobile}<' in sheet
    assert "PhonePe cannot take the amount from a link for this account" in sheet
    assert "Open PhonePe, choose" in sheet and "enter the amount shown above" in sheet
    assert "Scan from gallery" in sheet
    assert sheet.count('class="sv-copy"') == 3  # amount, UPI ID, mobile number


def test_upi_mobile_setting_is_configurable(seeded_db, owner_client, monkeypatch):
    monkeypatch.setattr(settings, "upi_mobile", "9876543210")
    mp = _unpay(seeded_db, "102")
    sheet = owner_client.get(f"/?view=simple&period_id={mp.period_id}&pay=m-{mp.period_id}-{mp.flat_id}").text
    assert 'data-copy-text="9876543210"' in sheet and "9940255076</strong>" not in sheet


def test_script_syncs_amounts_updates_qr_and_copies_plain_number(owner_client):
    js = owner_client.get("/static/js/paynow.js").text
    for needle in ("pay-amount-top", 'amount.addEventListener("input"', 'topAmt.addEventListener("input"',
                   "upi-qr", "Pays this off in full", "Remaining after this payment",
                   "submit.disabled", "en-IN", "String(res.value)"):
        assert needle in js, needle


def test_qr_payload_follows_partial_amount(seeded_db, owner_client):
    own = _flat_id(seeded_db, "102")
    r = owner_client.get(f"/upi/qr.svg?flat_id={own}&amount=5000&note=Flat%20102%20Project%20X")
    assert r.status_code == 200 and "<svg" in r.text
    assert "am=5000.00" in upi_svc.build_link(5000, "Flat 102 Project X")


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

    own = _flat_id(seeded_db, "102")
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
    assert APP_VERSION == "nest5-v7.0"
    for client in (owner_client, admin_client):
        page = client.get("/").text
        assert f'<p class="app-version">version: {APP_VERSION}</p>' in page
    assert "version:" not in html.unescape(owner_client.get("/login").text)
