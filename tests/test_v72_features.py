"""v7.2: consolidated "Expenses you paid" metric for owners, and the IST clock under the user line."""
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app import dates
from app.models import Expense, Flat, User
from app.services import balances as balances_svc
from tests.conftest import OWNER_PASSWORD, make_client
from tests.test_v93_features import TENANT_PASSWORD, tnt_flat101  # noqa: F401


def _flats(db):
    return {f.number: f.id for f in db.scalars(select(Flat)).all()}


@pytest.fixture
def mark(seeded_db):
    """Create counted / rejected / deleted expenses paid by a flat; remove them afterwards."""
    made = []

    def add(flat_id, amount, *, settled=False, status="approved", deleted=False, desc="v72 item"):
        e = Expense(description=desc, category="Common", amount=amount, expense_date=date(2026, 10, 3),
                    paid_by_flat_id=flat_id, is_settled=settled, status=status,
                    deleted_at=datetime.utcnow() if deleted else None)
        seeded_db.add(e)
        seeded_db.commit()
        made.append(e.id)
        return e

    yield add
    seeded_db.rollback()
    for eid in made:
        e = seeded_db.get(Expense, eid)
        if e is not None:
            seeded_db.delete(e)
    seeded_db.commit()


def _owner(db, set_password, username):
    set_password(db, username, OWNER_PASSWORD)
    return make_client(username, OWNER_PASSWORD)


def _base(db, ids):
    return balances_svc.owner_advances(db, ids)


# ---- consolidated metric ----------------------------------------------------------------------


def test_owner_advances_sums_across_flats_and_counts_only_counted(seeded_db, mark):
    f = _flats(seeded_db)
    ids = [f["101"], f["401"]]
    b = _base(seeded_db, ids)
    mark(f["101"], 1000, settled=True)
    mark(f["401"], 300)
    mark(f["101"], 200)
    mark(f["101"], 999, status="rejected")
    mark(f["401"], 888, deleted=True)
    mark(f["102"], 777)  # someone else's flat
    a = _base(seeded_db, ids)
    assert a["paid_total"] - b["paid_total"] == 1500
    assert a["settled_total"] - b["settled_total"] == 1000
    assert a["owed"] - b["owed"] == 500
    assert a["count"] - b["count"] == 3 and a["has_any"]
    assert a["paid_total"] == a["settled_total"] + a["owed"]
    per = {r["flat"].number: r for r in a["per_flat"]}
    assert per["101"]["paid"] - next(r for r in b["per_flat"] if r["flat"].number == "101")["paid"] == 1200
    assert per["401"]["owed"] - next(r for r in b["per_flat"] if r["flat"].number == "401")["owed"] == 300


def test_two_flat_owner_sees_the_sum_on_both_dashboard_views(seeded_db, set_password, mark):
    f = _flats(seeded_db)
    owner = _owner(seeded_db, set_password, "manikyamba")
    b = _base(seeded_db, [f["101"], f["401"]])
    mark(f["101"], 4000, settled=True, desc="Settled thing")
    mark(f["401"], 1500, desc="Waiting thing")
    a = _base(seeded_db, [f["101"], f["401"]])
    for url in ("/?view=detailed", "/?view=simple"):
        page = owner.get(url).text
        assert 'class="adv-summary"' in page, url
        assert f"<span>Paid by you</span><strong>₹{a['paid_total']}</strong>" in page
        assert f"<span>Settled</span><strong>₹{a['settled_total']}</strong>" in page
        assert f"<span>Owed back</span><strong>₹{a['owed']}</strong>" in page
        assert f"{a['count']} expenses" in page
        assert "Waiting thing" in page
    assert "Flat 101: paid" in owner.get("/?view=detailed").text  # per-flat split stays on the detailed card


def test_fully_settled_owner_still_sees_the_section_with_zero_pending(seeded_db, set_password, mark):
    f = _flats(seeded_db)
    owner = _owner(seeded_db, set_password, "pavan.kumar")
    assert not _base(seeded_db, [f["501"]])["has_any"]
    mark(f["501"], 640, settled=True)
    for url in ("/?view=detailed", "/?view=simple"):
        page = owner.get(url).text
        assert "<span>Paid by you</span><strong>₹640</strong>" in page and "<span>Settled</span><strong>₹640</strong>" in page
        assert "<span>Owed back</span><strong>₹0</strong>" in page and 'class="adv-owed clear"' in page
        assert "1 expense " in page or "1 expense<" in page or "1 expense ·" in page
    assert "All settled" in owner.get("/?view=simple").text


def test_section_hidden_when_owner_has_no_counted_expenses(seeded_db, set_password, mark):
    f = _flats(seeded_db)
    owner = _owner(seeded_db, set_password, "pavan.kumar")
    mark(f["501"], 500, status="rejected")
    mark(f["501"], 400, deleted=True)
    assert not _base(seeded_db, [f["501"]])["has_any"]
    for url in ("/?view=detailed", "/?view=simple"):
        page = owner.get(url).text
        assert "adv-summary" not in page and "Expenses you paid" not in page, url


def test_admin_dashboard_has_no_owner_summary_and_tenant_is_unchanged(seeded_db, admin_client, tnt_flat101, mark):
    f = _flats(seeded_db)
    mark(f["101"], 321)
    assert "adv-summary" not in admin_client.get("/").text
    r = tnt_flat101.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/payments/maintenance"
    assert "adv-summary" not in tnt_flat101.get("/payments/maintenance").text


# ---- IST clock ------------------------------------------------------------------------------


@pytest.mark.parametrize("utc, expected", [
    (datetime(2026, 10, 19, 11, 7, tzinfo=timezone.utc), "Mon 19 Oct 2026, 4:37 PM IST"),
    (datetime(2026, 10, 19, 18, 30, tzinfo=timezone.utc), "Tue 20 Oct 2026, 12:00 AM IST"),
    (datetime(2026, 10, 19, 6, 30, tzinfo=timezone.utc), "Mon 19 Oct 2026, 12:00 PM IST"),
    (datetime(2026, 12, 31, 20, 0, tzinfo=timezone.utc), "Fri 1 Jan 2027, 1:30 AM IST"),
    (datetime(2026, 3, 1, 0, 5), "Sun 1 Mar 2026, 5:35 AM IST"),  # naive means UTC
])
def test_format_ist(utc, expected):
    assert dates.format_ist(utc) == expected


def test_ist_now_is_a_fixed_plus_0530_offset():
    t = dates.ist_now(datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc))
    assert t.utcoffset().total_seconds() == 19800 and t.hour == 5 and t.minute == 30


def test_clock_under_logged_in_line_for_every_role(seeded_db, admin_client, owner_client, tnt_flat101):
    import re
    pat = re.compile(r'Logged in as <strong>[^<]+</strong> \((admin|owner|tenant)\)<div class="session-time" id="ist-clock"[^>]*>'
                     r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} [A-Z][a-z]{2} \d{4}, \d{1,2}:\d\d (AM|PM) IST</div></div>')
    for client, url, role in ((admin_client, "/", "admin"), (owner_client, "/", "owner"), (tnt_flat101, "/payments/maintenance", "tenant")):
        page = client.get(url).text
        m = pat.search(page)
        assert m and m.group(1) == role, (role, url)
        assert "/static/js/ist-clock.js" in page


def test_login_page_has_no_clock_and_script_does_ist_math(admin_client):
    from fastapi.testclient import TestClient

    from app.main import app
    assert "ist-clock" not in TestClient(app).get("/login").text
    js = admin_client.get("/static/js/ist-clock.js")
    assert js.status_code == 200 and "330 * 60000" in js.text and "IST" in js.text


def test_clock_is_a_single_non_wrapping_line_in_css():
    from pathlib import Path
    css = (Path(__file__).resolve().parents[1] / "app/static/css/app.css").read_text()
    block = css.split(".session-time {", 1)[1].split("}", 1)[0]
    assert "white-space: nowrap" in block and "overflow: hidden" in block
