"""v9.1: an owner expense the admin rejects must stop counting everywhere (but stay as a record)."""
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import AuditLog, CancelRequest, Expense, Flat, LedgerPeriod, Settlement
from app.services import actions as actions_svc
from app.services import alerts as alerts_svc
from app.services import balances as balances_svc
from app.services import expense_review as review_svc
from app.services import ledger as ledger_svc
from app.services import pdf_report
from tests.conftest import OWNER_PASSWORD, make_client
from tests.test_cancel_everywhere import totals

ROOT = Path(__file__).resolve().parents[1]


def _flats(db):
    return {f.number: f.id for f in db.scalars(select(Flat)).all()}


def _without_panel(page: str) -> str:
    """The admin's collapsed 'Rejected expenses' clean-up panel is the only place a rejected row may appear."""
    page = re.sub(r'<details class="card no-print rejected-panel">.*?</details>', "", page, flags=re.S)
    return re.sub(r'<details class="card dashboard-activity.*?</details>', "", page, flags=re.S)


def _oct(db):
    return db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10))


@pytest.fixture
def cleanup(seeded_db):
    """Expenses created by a test are removed afterwards (the test DB is shared)."""
    ids: list[int] = []
    baseline = ledger_svc.compute_fund_balance(seeded_db)
    yield ids
    seeded_db.rollback()
    seeded_db.expire_all()
    for st in seeded_db.scalars(select(Settlement).where(Settlement.expense_id.in_(ids or [0]))).all():
        seeded_db.delete(st)
    for eid in ids:
        exp = seeded_db.get(Expense, eid)
        if exp is not None:
            seeded_db.delete(exp)
    for req in seeded_db.scalars(select(CancelRequest).where(CancelRequest.record_type == "expense",
                                                             CancelRequest.record_id.in_(ids or [0]))).all():
        seeded_db.delete(req)
    seeded_db.commit()
    assert ledger_svc.compute_fund_balance(seeded_db) == baseline


def _owner_submit(client, db, flat_id, desc, amount, cleanup, day="2026-10-12"):
    r = client.post("/owner/expenses", data={"flat_id": flat_id, "description": desc, "category": "Common",
                                             "amount": str(amount), "expense_date": day}, follow_redirects=False)
    assert "owner_msg" in r.headers["location"], r.headers["location"]
    db.expire_all()
    exp = db.scalar(select(Expense).where(Expense.description == desc))
    cleanup.append(exp.id)
    return exp


def _pdf_tables(db):
    """Every table the all-time summary PDF and the flat statements draw, as plain rows."""
    captured, original = [], pdf_report._table

    def spy(rows, *a, **kw):
        captured.append([[str(c) if not hasattr(c, "text") else c.text for c in row] for row in rows])
        return original(rows, *a, **kw)

    pdf_report._table = spy
    try:
        pdf_report.build_summary_pdf(db, None)
        pdf_report.build_flat_statement_pdf(db, db.scalars(select(Flat).order_by(Flat.number)).all(), None)
    finally:
        pdf_report._table = original
    return captured


def _extra_totals(db):
    bal = balances_svc.overview(db)
    hub = actions_svc.build(db)
    settle = next(g for g in hub["groups"] if g["key"] == "settle")
    return {
        "bal": {k: v for k, v in bal.items() if k not in ("by_flat",)},
        "by_flat": [(r["flat"].number, r["total"]) for r in bal["by_flat"]],
        "available": ledger_svc.available_for_settlement(db),
        "status_by_flat": [(r["flat"].number, r["pending_amount"], r["settled_amount"]) for r in ledger_svc.settlement_status_by_flat(db)],
        "hub_settle": [r["text"] for r in settle["rows"]],
        "hub_total": hub["total"],
        "pdf_tables": _pdf_tables(db),
    }


def _all_totals(db, flats):
    out = totals(db, list(flats.values()))
    out["extra"] = _extra_totals(db)
    return out


def test_rejected_owner_expense_leaves_every_total(seeded_db, admin_client, owner_client, cleanup):
    flats = _flats(seeded_db)
    before = _all_totals(seeded_db, flats)
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Reject me pump", 777, cleanup)
    during = _all_totals(seeded_db, flats)
    assert during != before
    assert during["extra"]["bal"]["owed"] == before["extra"]["bal"]["owed"] + 777  # counted until rejected
    assert "Reject me pump" in admin_client.get("/settlements").text

    r = admin_client.post(f"/admin/expenses/{exp.id}/reject", data={"reason": "Not a common expense", "back": "/payments/expenses"},
                          follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"], r.headers["location"]
    seeded_db.expire_all()
    exp = seeded_db.get(Expense, exp.id)
    assert exp.status == "rejected" and exp.rejection_reason == "Not a common expense"
    assert exp.rejected_by == "admin" and exp.rejected_at is not None and exp.deleted_at is None

    after = _all_totals(seeded_db, flats)
    assert after == before  # identical to never having been submitted: balances, alerts, hub, flat totals, PDF rows

    # Pages
    for url in ("/", "/?view=simple", "/settlements", "/admin/actions", "/expenses", "/payments/expenses"):
        page = _without_panel(admin_client.get(url).text)
        assert "₹777" not in page, url
    assert "Reject me pump" not in admin_client.get("/settlements").text
    assert "Reject me pump" not in admin_client.get("/admin/actions").text
    assert "Reject me pump" not in _without_panel(admin_client.get("/").text)
    assert admin_client.get("/admin/export/pdf?period_id=").content.startswith(b"%PDF")
    stmt = owner_client.get(f"/flats/{flats['201']}/statement.pdf?period_id=")
    assert stmt.status_code == 200 and stmt.content.startswith(b"%PDF")
    seeded_db.expire_all()
    assert ledger_svc.expense_total(seeded_db) == before["expenses_all"]

    # Cannot be settled, not even by a forged POST
    r = admin_client.post(f"/settlements/{exp.id}/settle", data={"settled_on": "2026-10-13"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    seeded_db.expire_all()
    assert not seeded_db.get(Expense, exp.id).is_settled
    assert seeded_db.scalar(select(Settlement).where(Settlement.expense_id == exp.id)) is None
    # nor edited
    r = admin_client.post(f"/expenses/{exp.id}/edit", data={"description": "x", "category": "Common", "amount": "5",
                                                           "expense_date": "2026-10-12", "paid_by_flat_id": str(flats["201"])},
                          follow_redirects=False)
    assert "error=" in r.headers["location"]

    # Audit trail
    assert seeded_db.scalar(select(AuditLog).where(AuditLog.entity_type == "expense", AuditLog.entity_id == exp.id,
                                                   AuditLog.action == "reject"))

    # Reinstate brings it back everywhere
    r = admin_client.post(f"/admin/expenses/{exp.id}/reinstate", data={"back": "/expenses"}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert _all_totals(seeded_db, flats) == during


def test_rejected_expense_is_hidden_everywhere_except_the_admin_cleanup_panel(seeded_db, admin_client, owner_client, set_password, cleanup):
    flats = _flats(seeded_db)
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Hidden rejected bulb", 321, cleanup)
    admin_client.post(f"/admin/expenses/{exp.id}/reject", data={"reason": "Duplicate bill"})

    # Admin: only the collapsed clean-up panel on Expenses lists it (with Reinstate / Delete / Clear all)
    page = admin_client.get("/expenses").text
    assert "rejected-panel" in page and "Hidden rejected bulb" in page and "Duplicate bill" in page
    assert "Clear all rejected" in page and "/reinstate" in page and "/purge" in page
    assert "Hidden rejected bulb" not in _without_panel(page) and "Rejected</span>" not in page
    for url in ("/payments/expenses", "/", "/?view=simple", "/settlements", "/admin/actions", f"/flats/{flats['201']}"):
        text = _without_panel(admin_client.get(url).text)
        assert "Hidden rejected bulb" not in text and "Duplicate bill" not in text, url
        assert "Clear all rejected" not in text, url

    # The submitting owner sees nothing of it in any advance / expense list
    for url in ("/payments/expenses", "/expenses", "/", f"/flats/{flats['201']}"):
        text = owner_client.get(url).text
        assert "Hidden rejected bulb" not in text and "Duplicate bill" not in text, url
        assert "Clear all rejected" not in text and "/purge" not in text, url

    seeded_db.expire_all()
    owed = balances_svc.owner_advances(seeded_db, [flats["201"]])
    assert exp.id not in [e.id for e in owed["unsettled"] + owed["settled"]] and "rejected" not in owed

    # Another owner never sees it either
    set_password(seeded_db, "pavan.kumar", OWNER_PASSWORD)
    other = make_client("pavan.kumar", OWNER_PASSWORD)
    for url in ("/expenses", "/payments/expenses", "/", f"/flats/{flats['501']}", "/owner/submissions"):
        page = other.get(url).text
        assert "Hidden rejected bulb" not in page and "Duplicate bill" not in page, url
    assert other.get(f"/flats/{flats['201']}").status_code == 403


def test_excel_and_csv_exports_leave_out_rejected_but_json_backup_keeps_them(seeded_db, admin_client, owner_client, cleanup):
    import io
    import zipfile

    from openpyxl import load_workbook

    flats = _flats(seeded_db)
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Export rejected pump", 4321, cleanup)
    admin_client.post(f"/admin/expenses/{exp.id}/reject", data={"reason": "no"})
    from app.services import export as export_svc

    xlsx = load_workbook(io.BytesIO(export_svc.build_xlsx(seeded_db)))
    cells = [str(c.value) for row in xlsx["Expenses"].iter_rows() for c in row if c.value is not None]
    assert "Export rejected pump" not in cells and "Common expense 1" in cells
    with zipfile.ZipFile(io.BytesIO(export_svc.build_csv_zip(seeded_db))) as z:
        name = next(n for n in z.namelist() if n.startswith("expenses"))
        text = z.read(name).decode("utf-8", "ignore")
        assert "Export rejected pump" not in text and "Common expense 1" in text  # the audit-log sheet keeps history
    dump = export_svc.build_json_dump(seeded_db)
    assert any(r["description"] == "Export rejected pump" and r["status"] == "rejected"
               for r in dump["tables"]["expenses"]["rows"])


def test_only_admin_can_reject_reinstate_purge_or_clear(seeded_db, admin_client, owner_client, cleanup):
    flats = _flats(seeded_db)
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Owner cannot reject", 111, cleanup)
    for path in (f"/admin/expenses/{exp.id}/reject", f"/admin/expenses/{exp.id}/reinstate",
                 f"/admin/expenses/{exp.id}/purge", "/admin/expenses/clear-rejected"):
        assert owner_client.post(path, data={}, follow_redirects=False).status_code == 403, path
    seeded_db.expire_all()
    assert seeded_db.get(Expense, exp.id).status in (None, "approved")
    # the owner's delete route is admin-only too
    assert owner_client.post(f"/expenses/{exp.id}/delete", follow_redirects=False).status_code == 403
    # admin cannot purge an expense that was not rejected
    r = admin_client.post(f"/admin/expenses/{exp.id}/purge", data={"back": "/expenses"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    seeded_db.expire_all()
    assert seeded_db.get(Expense, exp.id) is not None


def test_purge_one_and_clear_all_rejected(seeded_db, admin_client, owner_client, cleanup):
    flats = _flats(seeded_db)
    a = _owner_submit(owner_client, seeded_db, flats["201"], "Purge A", 100, cleanup)
    b = _owner_submit(owner_client, seeded_db, flats["201"], "Purge B", 200, cleanup)
    c = _owner_submit(owner_client, seeded_db, flats["201"], "Keep C", 300, cleanup)
    base = ledger_svc.compute_fund_balance(seeded_db)
    for e in (a, b):
        admin_client.post(f"/admin/expenses/{e.id}/reject", data={"reason": "no"})
    a_id, b_id, c_id = a.id, b.id, c.id
    r = admin_client.post(f"/admin/expenses/{a_id}/purge", data={"back": "/expenses"}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    seeded_db.expire_all()
    assert seeded_db.get(Expense, a_id) is None and seeded_db.get(Expense, b_id) is not None
    assert seeded_db.scalar(select(AuditLog).where(AuditLog.entity_type == "expense", AuditLog.entity_id == a_id,
                                                   AuditLog.action == "purge"))
    page = admin_client.get("/expenses").text
    assert "Clear all rejected (1)" in page and 'onsubmit="return confirm(' in page and "Purge A" not in page

    r = admin_client.post("/admin/expenses/clear-rejected", data={"back": "/expenses"}, follow_redirects=False)
    assert "Cleared+1" in r.headers["location"] or "Cleared%201" in r.headers["location"]
    seeded_db.expire_all()
    assert seeded_db.get(Expense, b_id) is None
    kept = seeded_db.get(Expense, c_id)
    assert kept is not None and kept.status in (None, "approved") and not kept.is_settled  # approved ones untouched
    clear_log = seeded_db.scalar(select(AuditLog).where(AuditLog.action == "clear_rejected").order_by(AuditLog.id.desc()))
    assert clear_log and json.loads(clear_log.after_json)["purged"] == 1
    assert "Clear all rejected" not in admin_client.get("/expenses").text
    assert ledger_svc.compute_fund_balance(seeded_db) == base
    r = admin_client.post("/admin/expenses/clear-rejected", data={"back": "/expenses"}, follow_redirects=False)
    assert "No+rejected" in r.headers["location"]


def test_settled_expense_cannot_be_rejected_and_pending_cancel_request_is_closed(seeded_db, admin_client, owner_client, cleanup):
    flats = _flats(seeded_db)
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Settled then reject", 150, cleanup)
    admin_client.post(f"/settlements/{exp.id}/settle", data={"settled_on": "2026-10-13"})
    r = admin_client.post(f"/admin/expenses/{exp.id}/reject", data={}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    seeded_db.expire_all()
    assert seeded_db.get(Expense, exp.id).status in (None, "approved")

    other = _owner_submit(owner_client, seeded_db, flats["201"], "Cancel asked then rejected", 90, cleanup)
    owner_client.post("/owner/cancel-requests", data={"record_type": "expense", "record_id": other.id, "reason": "oops"})
    assert admin_client.post(f"/admin/expenses/{other.id}/reject", data={}).status_code in (200, 303)
    seeded_db.expire_all()
    req = seeded_db.scalar(select(CancelRequest).where(CancelRequest.record_type == "expense", CancelRequest.record_id == other.id))
    assert req.status == "approved"
    hub = {g["key"]: g for g in actions_svc.build(seeded_db)["groups"]}
    assert all("Cancel asked then rejected" not in r["text"] for r in hub["cancel_requests"]["rows"])


def test_cancel_request_keep_entry_keeps_expense_counted(seeded_db, admin_client, owner_client, cleanup):
    """'Keep entry' on an owner's cancel request keeps the expense counted (v9.3: plain Reject now rejects the expense)."""
    flats = _flats(seeded_db)
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Cancel refused", 60, cleanup)
    before = balances_svc.overview(seeded_db)["owed"]
    owner_client.post("/owner/cancel-requests", data={"record_type": "expense", "record_id": exp.id, "reason": "x"})
    seeded_db.expire_all()
    req = seeded_db.scalar(select(CancelRequest).where(CancelRequest.record_type == "expense", CancelRequest.record_id == exp.id))
    admin_client.post(f"/admin/cancel-requests/{req.id}/keep", data={"note": "keep"})
    seeded_db.expire_all()
    assert seeded_db.get(Expense, exp.id).status in (None, "approved")
    assert balances_svc.overview(seeded_db)["owed"] == before
    assert "Keep entry" in admin_client.get("/admin/cancel-requests").text


def test_approved_cancel_still_removes_expense(seeded_db, admin_client, owner_client, cleanup):
    flats = _flats(seeded_db)
    before = balances_svc.overview(seeded_db)["owed"]
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Cancel approved", 60, cleanup)
    owner_client.post("/owner/cancel-requests", data={"record_type": "expense", "record_id": exp.id, "reason": "x"})
    seeded_db.expire_all()
    req = seeded_db.scalar(select(CancelRequest).where(CancelRequest.record_type == "expense", CancelRequest.record_id == exp.id))
    admin_client.post(f"/admin/cancel-requests/{req.id}/approve", data={"note": ""})
    seeded_db.expire_all()
    assert balances_svc.overview(seeded_db)["owed"] == before


def test_rejected_in_closed_month_does_not_move_account_balance(seeded_db, admin_client, owner_client, cleanup):
    """A rejected (never settled) expense cannot change any month's closing balance."""
    flats = _flats(seeded_db)
    sept = seeded_db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 9))
    before = (sept.closing_fund_balance, ledger_svc.compute_fund_balance(seeded_db))
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Sept late bill", 400, cleanup, day="2026-09-20")
    admin_client.post(f"/admin/expenses/{exp.id}/reject", data={})
    seeded_db.expire_all()
    sept = seeded_db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 9))
    assert (sept.closing_fund_balance, ledger_svc.compute_fund_balance(seeded_db)) == before
    assert ledger_svc.expense_total(seeded_db, sept.id) == 5045 + 0  # only the seeded September expenses


def test_other_submission_types_keep_counting_rules(seeded_db, admin_client, owner_client):
    """Maintenance / corpus / project payments have no 'reject' of the entry itself: the only rejection is of a
    cancel request, which keeps the (counted) entry. Approving a cancel is covered in test_cancel_everywhere."""
    from app.models import MaintenancePayment

    flats = _flats(seeded_db)
    period = _oct(seeded_db)
    mp = seeded_db.scalar(select(MaintenancePayment).where(MaintenancePayment.period_id == period.id,
                                                           MaintenancePayment.flat_id == flats["201"]))
    saved = (mp.is_paid, mp.paid_on, mp.amount, mp.payment_note, mp.payment_mode, mp.submitted_by_user_id)
    mp.is_paid, mp.paid_on, mp.submitted_by_user_id = False, None, None
    seeded_db.commit()
    mp_id = mp.id
    try:
        owner_client.post("/owner/maintenance", data={"flat_id": flats["201"], "period_id": period.id, "paid_on": "2026-10-06",
                                                      "amount": "2000", "payment_mode": "UPI", "payment_note": "UTR123456789"})
        counted = ledger_svc.compute_fund_balance(seeded_db)
        owner_client.post("/owner/cancel-requests", data={"record_type": "maintenance_payment", "record_id": mp_id})
        seeded_db.expire_all()
        req = seeded_db.scalar(select(CancelRequest).where(CancelRequest.record_type == "maintenance_payment",
                                                           CancelRequest.record_id == mp_id, CancelRequest.status == "pending"))
        admin_client.post(f"/admin/cancel-requests/{req.id}/reject", data={"note": "no"})
        seeded_db.expire_all()
        assert ledger_svc.compute_fund_balance(seeded_db) == counted
        assert seeded_db.get(MaintenancePayment, mp_id).is_paid
    finally:
        seeded_db.expire_all()
        row = seeded_db.get(MaintenancePayment, mp_id)
        row.is_paid, row.paid_on, row.amount, row.payment_note, row.payment_mode, row.submitted_by_user_id = saved
        row.reverted_at = None
        for r in seeded_db.scalars(select(CancelRequest).where(CancelRequest.record_id == mp_id,
                                                               CancelRequest.record_type == "maintenance_payment")).all():
            seeded_db.delete(r)
        seeded_db.commit()


# ---- data repair ---------------------------------------------------------------------------------


def test_repair_fixes_rejected_rows_that_still_count(seeded_db, cleanup):
    flats = _flats(seeded_db)
    base = _all_totals(seeded_db, flats)
    # a rejected row that is (wrongly) still flagged settled with a live settlement and one unsettled
    period = _oct(seeded_db)
    settled = Expense(description="Repair settled", category="Common", amount=500, expense_date=date(2026, 10, 3),
                      paid_by_flat_id=flats["201"], period_id=period.id, is_settled=True, status="rejected",
                      submitted_by_user_id=1)
    unsettled = Expense(description="Repair unsettled", category="Common", amount=250, expense_date=date(2026, 10, 4),
                        paid_by_flat_id=flats["201"], period_id=period.id, is_settled=False, status="rejected",
                        submitted_by_user_id=1)
    seeded_db.add_all([settled, unsettled])
    seeded_db.flush()
    seeded_db.add(Settlement(expense_id=settled.id, amount=500, settled_on=date(2026, 10, 5)))
    seeded_db.commit()
    cleanup.extend([settled.id, unsettled.id])
    # the settlement row is still live and flagged, but the counting queries already ignore the rejected row
    now = _all_totals(seeded_db, flats)
    for k in base["extra"]:
        assert now["extra"][k] == base["extra"][k], k
    assert now == base

    assert review_svc.repair_rejected_expenses(seeded_db) == 1
    seeded_db.commit()
    seeded_db.expire_all()
    row = seeded_db.get(Expense, settled.id)
    assert row.is_settled is False and row.settlement.reverted_at is not None and row.status == "rejected"
    assert seeded_db.get(Expense, unsettled.id) is not None  # nothing deleted
    assert review_svc.repair_rejected_expenses(seeded_db) == 0  # idempotent
    assert _all_totals(seeded_db, flats) == base


def _alembic(db_url: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": db_url}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)


def test_migration_from_scratch_and_repair_on_existing_rows(tmp_path):
    db_file = tmp_path / "scratch.db"
    url = f"sqlite:///{db_file}"
    r = _alembic(url, "upgrade", "b8e3f5a1d726")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db_file)
    cols = {row[1] for row in con.execute("PRAGMA table_info(expenses)")}
    assert "status" not in cols
    con.close()
    r = _alembic(url, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db_file)
    cols = {row[1] for row in con.execute("PRAGMA table_info(expenses)")}
    assert {"status", "rejection_reason", "rejected_at", "rejected_by"} <= cols
    assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "d6a3f9b1c428"
    con.close()
    # Down and up again is clean
    assert _alembic(url, "downgrade", "b8e3f5a1d726").returncode == 0
    assert _alembic(url, "upgrade", "head").returncode == 0


def test_migration_repairs_rejected_but_counted_rows(tmp_path):
    """A DB where the columns were added early (stamped / safety-net) and a rejected expense still counts."""
    db_file = tmp_path / "counted.db"
    url = f"sqlite:///{db_file}"
    assert _alembic(url, "upgrade", "b8e3f5a1d726").returncode == 0
    con = sqlite3.connect(db_file)
    con.execute("ALTER TABLE expenses ADD COLUMN status VARCHAR(20)")
    con.execute("INSERT INTO flats (number) VALUES ('201')")
    con.execute("INSERT INTO expenses (description, category, amount, expense_date, paid_by_flat_id, is_settled, status) "
                "VALUES ('Rejected settled', 'Common', 500, '2026-10-03', 1, 1, 'rejected')")
    con.execute("INSERT INTO expenses (description, category, amount, expense_date, paid_by_flat_id, is_settled, status) "
                "VALUES ('Rejected open', 'Common', 250, '2026-10-04', 1, 0, 'rejected')")
    con.execute("INSERT INTO expenses (description, category, amount, expense_date, paid_by_flat_id, is_settled) "
                "VALUES ('Normal old', 'Common', 100, '2026-10-05', 1, 0)")
    con.execute("INSERT INTO settlements (expense_id, fund_type, amount, settled_on) VALUES (1, 'MAINTENANCE', 500, '2026-10-06')")
    con.commit()
    con.close()
    r = _alembic(url, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db_file)
    rows = {r[0]: r[1:] for r in con.execute("SELECT description, status, is_settled, deleted_at FROM expenses")}
    assert rows["Rejected settled"] == ("rejected", 0, None)  # flag corrected, row kept
    assert rows["Rejected open"] == ("rejected", 0, None)
    assert rows["Normal old"] == ("approved", 0, None)
    assert con.execute("SELECT reverted_at FROM settlements WHERE expense_id = 1").fetchone()[0] is not None
    con.close()
    # Re-running the repair is a no-op
    assert _alembic(url, "downgrade", "b8e3f5a1d726").returncode == 0
    con = sqlite3.connect(db_file)
    # downgrade soft-deletes rejected rows so they cannot count again without the column
    assert con.execute("SELECT count(*) FROM expenses WHERE deleted_at IS NOT NULL").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM expenses").fetchone()[0] == 3
    con.close()


def test_rejected_big_expense_does_not_block_settlement(seeded_db, admin_client, owner_client, cleanup):
    """The 'Settlement blocked' alert and settlement-block calculation ignore a rejected expense."""
    flats = _flats(seeded_db)
    held = ledger_svc.compute_fund_balance(seeded_db)
    base = balances_svc.overview(seeded_db)
    exp = _owner_submit(owner_client, seeded_db, flats["201"], "Huge rejected bill", held + 9000, cleanup)
    seeded_db.expire_all()
    titles = [a["title"] for a in alerts_svc.build_dashboard_alerts(seeded_db, None)]
    assert "Settlement blocked" in titles
    bal = balances_svc.overview(seeded_db)
    assert bal["negative_if_all"] and bal["state"] in ("none", "some")
    assert "Blocked" in admin_client.get("/settlements").text and "Huge rejected bill" in admin_client.get("/settlements").text

    admin_client.post(f"/admin/expenses/{exp.id}/reject", data={"reason": "Too large"})
    seeded_db.expire_all()
    titles = [a["title"] for a in alerts_svc.build_dashboard_alerts(seeded_db, None)]
    assert "Settlement blocked" not in titles
    bal = balances_svc.overview(seeded_db)
    assert bal["owed"] == base["owed"] and bal["count"] == base["count"] and bal["effective"] == base["effective"]
    assert bal["settle_now"] == base["settle_now"] and bal["negative_if_all"] == base["negative_if_all"]
    page = _without_panel(admin_client.get("/").text)
    assert "Huge rejected bill" not in page and "would take the Account Balance" not in page
    assert "Huge rejected bill" not in admin_client.get("/settlements").text
