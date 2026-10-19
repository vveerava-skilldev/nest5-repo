"""v8 (simplified in v7.3): "Pay now" sheet (plain payee details, PhonePe launch) and the required UTR."""
import html
import re
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import AuditLog, CancelRequest, Flat, LedgerPeriod, MaintenancePayment
from app.services import dashboard_view as dashboard_view_svc
from app.services import upi as upi_svc
from tests.conftest import OWNER_PASSWORD, make_client
from tests.test_v7_simple_dashboard import _app_href, _flat_id, _goal, _project, _unpay

UTR = "412345678901"


def _sheet(client, mp):
    return client.get(f"/?view=simple&period_id={mp.period_id}&pay=m-{mp.period_id}-{mp.flat_id}").text


def _submit(client, mp, **over):
    data = {"flat_id": mp.flat_id, "period_id": mp.period_id, "paid_on": "2026-10-05", "amount": str(mp.amount),
            "payment_mode": "UPI", "payment_note": UTR, "back": f"/?view=simple&period_id={mp.period_id}"}
    data.update(over)
    return client.post("/owner/maintenance", data=data, follow_redirects=False)


# ---- UTR rule (unit) ----------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["412345678901", "ABCD1234", "a" * 30, "HDFC0123456789ABC"])
def test_utr_accepts_8_to_30_alphanumerics(ref):
    assert upi_svc.utr_error("UPI", ref) is None
    assert upi_svc.utr_error("UPI", f"  {ref}  ") is None  # surrounding spaces are trimmed


@pytest.mark.parametrize("ref", ["", "   ", None, "1234567", "a" * 31, "4123 4567 8901", "UTR-123456789", "12345678!"])
def test_utr_rejected_for_upi(ref):
    assert upi_svc.utr_error("UPI", ref)


@pytest.mark.parametrize("mode", ["Cash", "Bank transfer", "Cheque", "Other"])
def test_utr_not_needed_for_other_modes(mode):
    assert upi_svc.utr_error(mode, "") is None and upi_svc.utr_error(mode, "x") is None


# ---- links ----------------------------------------------------------------------------------


def test_no_prefilled_app_link_builders_remain():
    for gone in ("app_links", "APP_LINKS", "phonepe_link", "PHONEPE_PREFIX"):
        assert not hasattr(upi_svc, gone), gone
    assert upi_svc.PHONEPE_LAUNCH == "phonepe://"
    # The QR keeps the full upi:// payload (payee, amount, note); PhonePe's scanner accepts it.
    q = parse_qs(urlsplit(upi_svc.build_link(1250, "Flat 5 & co", vpa="x@okaxis", payee="A & B")).query)
    assert q == {"pa": ["x@okaxis"], "pn": ["A & B"], "am": ["1250.00"], "cu": ["INR"], "tn": ["Flat 5 & co"]}


def test_sheet_shows_payee_plainly_without_copy_or_qr_and_has_utr_field(seeded_db, owner_client):
    mp = _unpay(seeded_db, "102")
    sheet = _sheet(owner_client, mp)
    assert f"₹{mp.amount}" in sheet
    assert settings.upi_vpa == "9940255076@ybl"
    assert '<dd id="pay-vpa">9940255076@ybl</dd>' in sheet and '<dd id="pay-mobile">9940255076</dd>' in sheet
    assert settings.upi_payee_name in sheet
    for gone in ('class="sv-copy"', "data-copy", "Copy</button>", "Show QR", 'id="upi-qr"', "Scan from gallery", "/upi/qr.svg",
                 "pay-amount-top", "copy-amount-text", "PhonePe cannot take the amount from a link"):
        assert gone not in sheet, gone
    assert _app_href(sheet, "phonepe") == "phonepe://" and sheet.count("data-open-app=") == 1
    for gone in ("tez://", "paytmmp://", "phonepe://pay", 'href="upi://', ">Google Pay<", ">Paytm<", "Other UPI app", "data-upi-app"):
        assert gone not in sheet
    assert "UPI reference (UTR)" in sheet
    field = re.search(r'<input type="text" name="payment_note"[^>]*>', sheet).group(0)
    assert " required" in field and 'pattern="[A-Za-z0-9]{8,30}"' in field
    assert re.search(r'<select name="payment_mode" id="pay-mode">\s*<option>UPI</option>', sheet)  # UPI is the default
    assert 'name="paid_on"' in sheet and 'name="amount"' in sheet and "Submit payment" in sheet
    assert 'src="/static/js/paynow.js"' in sheet and 'data-owner="1"' in sheet


def test_script_has_no_clipboard_code_but_keeps_utr_toggle(owner_client):
    js = owner_client.get("/static/js/paynow.js")
    assert js.status_code == 200
    for gone in ("clipboard", "execCommand", "sv-copy", "upi-qr"):
        assert gone not in js.text, gone
    assert "[A-Za-z0-9]{8,30}" in js.text


def test_admin_page_is_not_in_owner_mode(seeded_db, admin_client):
    assert 'data-owner="1"' not in admin_client.get("/").text


def test_list_row_is_a_pay_now_button_not_a_deep_link(seeded_db, owner_client):
    mp = _unpay(seeded_db, "102")
    rows = owner_client.get(f"/?view=simple&period_id={mp.period_id}&more=1").text
    buttons = re.findall(r'class="sv-upi" href="([^"]+)"[^>]*>Pay now<', rows)
    assert buttons and all(html.unescape(b).startswith("/?view=simple") and "&pay=" in html.unescape(b) for b in buttons)
    assert "upi://" not in rows and ">UPI<" not in rows
    assert f"pay=m-{mp.period_id}-{mp.flat_id}" in html.unescape(" ".join(buttons))


# ---- server-side UTR validation ----------------------------------------------------------------


def test_upi_without_utr_is_refused_and_nothing_is_paid(seeded_db, owner_client):
    mp = _unpay(seeded_db, "102")
    for bad in ("", "  ", "123", "12 34 56 78 90", "x" * 31):
        r = _submit(owner_client, mp, payment_note=bad)
        assert r.status_code == 303 and "owner_error=" in r.headers["location"], bad
        seeded_db.expire_all()
        assert not seeded_db.get(MaintenancePayment, mp.id).is_paid
    r = _submit(owner_client, mp, payment_mode="UPI")
    assert r.status_code == 303 and "owner_msg=" in r.headers["location"]
    seeded_db.expire_all()
    row = seeded_db.get(MaintenancePayment, mp.id)
    assert row.is_paid and row.payment_note == UTR and row.payment_mode == "UPI"
    assert row.submitted_by_user_id is not None  # still goes through the owner-submission flow


def test_missing_mode_defaults_to_upi_and_needs_utr(seeded_db, owner_client):
    mp = _unpay(seeded_db, "102")
    r = owner_client.post("/owner/maintenance", data={
        "flat_id": mp.flat_id, "period_id": mp.period_id, "paid_on": "2026-10-05"}, follow_redirects=False)
    assert "owner_error=" in r.headers["location"]


def test_utr_is_stored_upper_case(seeded_db, owner_client):
    mp = _unpay(seeded_db, "102")
    _submit(owner_client, mp, payment_note="abcd1234efgh")
    seeded_db.expire_all()
    assert seeded_db.get(MaintenancePayment, mp.id).payment_note == "ABCD1234EFGH"


@pytest.mark.parametrize("mode", ["Cash", "Bank transfer", "Cheque", "Other"])
def test_other_modes_need_no_utr(seeded_db, owner_client, mode):
    mp = _unpay(seeded_db, "102")
    r = _submit(owner_client, mp, payment_mode=mode, payment_note="")
    assert "owner_msg=" in r.headers["location"], r.headers["location"]
    seeded_db.expire_all()
    row = seeded_db.get(MaintenancePayment, mp.id)
    assert row.is_paid and row.payment_mode == mode and row.payment_note is None


def test_utr_required_on_corpus_and_project_endpoints_too(seeded_db, admin_client, owner_client):
    from app.models import CorpusPayment, ProjectPayment

    own = _flat_id(seeded_db, "102")
    goal = _goal(admin_client, seeded_db, "Pay now goal", 50000)
    r = owner_client.post("/corpus/pay", data={"target": f"{own}:{goal.id}", "amount": "1000", "paid_on": "2026-10-06",
                                               "payment_mode": "UPI", "back": "/?view=simple"}, follow_redirects=False)
    assert "error=" in r.headers["location"] and "msg=" not in r.headers["location"].replace("error=", "")
    assert seeded_db.scalar(select(CorpusPayment).where(CorpusPayment.campaign_id == goal.id)) is None
    r = owner_client.post(f"/corpus/campaigns/{goal.id}/pay", data={"flat_id": own, "amount": "1000", "paid_on": "2026-10-06",
                                                                    "payment_mode": "UPI", "payment_note": "short"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    r = owner_client.post("/corpus/pay", data={"target": f"{own}:{goal.id}", "amount": "1000", "paid_on": "2026-10-06",
                                               "payment_mode": "UPI", "payment_note": UTR, "back": "/?view=simple"}, follow_redirects=False)
    assert "msg=" in r.headers["location"] and "error=" not in r.headers["location"]

    project = _project(admin_client, seeded_db, "Pay now project", "10000")
    pp = seeded_db.scalar(select(ProjectPayment).where(ProjectPayment.project_id == project.id, ProjectPayment.flat_id == own))
    r = owner_client.post("/owner/projects/pay", data={"share_id": pp.id, "amount": "500", "paid_on": "2026-10-06",
                                                       "payment_mode": "UPI", "back": "/?view=simple"}, follow_redirects=False)
    assert "owner_error=" in r.headers["location"]
    seeded_db.expire_all()
    assert seeded_db.get(ProjectPayment, pp.id).paid == 0
    r = owner_client.post("/owner/projects/pay", data={"share_id": pp.id, "amount": "500", "paid_on": "2026-10-06",
                                                       "payment_mode": "Cash", "back": "/?view=simple"}, follow_redirects=False)
    assert "owner_msg=" in r.headers["location"]


def test_admin_entries_stay_as_free_as_before(seeded_db, admin_client):
    mp = _unpay(seeded_db, "102")
    r = admin_client.post("/owner/maintenance", data={
        "flat_id": mp.flat_id, "period_id": mp.period_id, "paid_on": "2026-10-05", "payment_mode": "UPI"},
        follow_redirects=False)
    assert "owner_msg=" in r.headers["location"]


# ---- owner-only access unchanged -----------------------------------------------------------------


def test_owner_cannot_open_another_flats_pay_sheet_or_qr(seeded_db, owner_client):
    other = _unpay(seeded_db, "301")
    page = owner_client.get(f"/?view=simple&period_id={other.period_id}&pay=m-{other.period_id}-{other.flat_id}").text
    assert 'class="sv-sheet"' not in page and "9940255076@ybl" not in page
    assert owner_client.get(f"/upi/qr.svg?flat_id={other.flat_id}&amount=100&note=x").status_code == 403
    r = _submit(owner_client, other)
    assert r.status_code == 403
    seeded_db.expire_all()
    assert not seeded_db.get(MaintenancePayment, other.id).is_paid


def test_sheet_requires_login(seeded_db):
    from fastapi.testclient import TestClient
    from app.main import app

    anon = TestClient(app)
    assert anon.get("/?view=simple&pay=m-1-1", follow_redirects=False).status_code == 303
    assert anon.get("/upi/qr.svg?flat_id=1&amount=100", follow_redirects=False).status_code == 303


# ---- admin sees the UTR to match the statement ----------------------------------------------------


def test_admin_sees_utr_in_payments_view_and_cancel_queue(seeded_db, admin_client, owner_client):
    mp = _unpay(seeded_db, "102")
    _submit(owner_client, mp)
    page = admin_client.get(f"/payments/maintenance?period_id={mp.period_id}").text
    assert UTR in page and "Submitted by owner" in page
    owner_client.post("/owner/cancel-requests", data={"record_type": "maintenance_payment", "record_id": mp.id})
    queue = admin_client.get("/admin/cancel-requests").text
    assert f"UTR {UTR}" in queue
    assert UTR in owner_client.get("/owner/submissions").text  # the owner sees their own reference
    audit = seeded_db.scalars(select(AuditLog).where(AuditLog.entity_type == "maintenance_payment",
                                                     AuditLog.action == "owner_submit")).all()[-1]
    assert UTR in (audit.after_json or "")


def test_owner_building_view_does_not_leak_other_flats_utr(seeded_db, owner_client, set_password):
    mp = _unpay(seeded_db, "102")
    _submit(owner_client, mp, payment_note="SECRETUTR12345")
    other = make_client_for(seeded_db, set_password, "pavan.kumar")
    for url in ("/payments/building", "/payments/maintenance", "/corpus", "/owner/submissions"):
        assert "SECRETUTR12345" not in other.get(url).text, url


def make_client_for(db, set_password, username):
    set_password(db, username, OWNER_PASSWORD)
    return make_client(username, OWNER_PASSWORD)
