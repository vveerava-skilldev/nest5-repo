"""An approved cancel (or admin revert) must take the amount out of every total, everywhere."""
import re
from datetime import date, timedelta

from sqlalchemy import select

from app.models import (
    CancelRequest,
    CorpusCampaign,
    CorpusPayment,
    Expense,
    Flat,
    LedgerPeriod,
    MaintenancePayment,
    Project,
    ProjectPayment,
)
from app.services import alerts as alerts_svc
from app.services import corpus_campaigns as campaigns_svc
from app.services import ledger as ledger_svc
from app.services import pdf_report
from app.services import reminders as reminders_svc
from tests.conftest import OWNER_PASSWORD, make_client

PAGES = ["/", "/payments/maintenance", "/payments/corpus", "/payments/projects", "/payments/expenses",
         "/expenses", "/corpus", "/projects", "/settlements"]


def _flats(db):
    return {f.number: f.id for f in db.scalars(select(Flat)).all()}


def _periods(db):
    return db.scalars(select(LedgerPeriod).order_by(LedgerPeriod.year, LedgerPeriod.month)).all()


def _oct(db):
    return db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10))


def totals(db, flat_ids):
    """Every computed total the app shows, as plain data."""
    db.expire_all()
    out = {"fund_balance": ledger_svc.compute_fund_balance(db)}
    for p in _periods(db):
        status = ledger_svc.build_dashboard_status(db, p)
        out[f"dash{p.id}"] = (
            status["totals"]["maintenance_pending"], status["totals"]["corpus_pending"],
            status["totals"]["grand_pending"],
            [(r["flat"].number, r["row_pending"], r["corpus"].get("paid")) for r in status["rows"]],
        )
        out[f"fund{p.id}"] = ledger_svc.period_fund_summary(db, p)["closing"]
        out[f"collected{p.id}"] = ledger_svc.maintenance_collected(db, p.id)
        out[f"expenses{p.id}"] = ledger_svc.expense_total(db, p.id)
        out[f"alerts{p.id}"] = [(a["title"], a.get("detail")) for a in alerts_svc.build_dashboard_alerts(db, p)]
        out[f"pdf{p.id}"] = pdf_report._flat_rows(db, p)
    out["pdf_all"] = pdf_report._flat_rows(db, None)
    out["expenses_all"] = ledger_svc.expense_total(db)
    out["unsettled"] = sorted(e.id for e in ledger_svc.unsettled_expenses(db))
    out["building_corpus"] = campaigns_svc.building_totals(db)
    for fid in flat_ids:
        flat = db.get(Flat, fid)
        bal = ledger_svc.flat_balances(db, flat)
        out[f"flat{fid}"] = {k: v for k, v in bal.items() if k != "flat"}
        out[f"corpus{fid}"] = {k: v for k, v in campaigns_svc.flat_corpus_totals(db, fid).items() if k != "rows"}
        out[f"reminders{fid}"] = [(i["kind"], i["label"], i["amount"]) for i in reminders_svc.reminders_for_flats(db, [fid])["items"]]
    return out


def _pages_without(clients, needle, extra=()):
    for client in clients:
        for url in list(PAGES) + list(extra):
            text = re.sub(r'<section class="card dashboard-activity.*?</section>', "", client.get(url).text, flags=re.S)
            assert needle not in text, f"{needle} still shown on {url}"


def _cancel(owner, admin, db, record_type, record_id):
    r = owner.post("/owner/cancel-requests", data={"record_type": record_type, "record_id": record_id, "reason": "test"},
                   follow_redirects=False)
    assert "owner_msg" in r.headers["location"], r.headers["location"]
    db.expire_all()
    req = db.scalar(select(CancelRequest).where(CancelRequest.record_type == record_type, CancelRequest.record_id == record_id,
                                                CancelRequest.status == "pending"))
    r = admin.post(f"/admin/cancel-requests/{req.id}/approve", data={"note": ""}, follow_redirects=False)
    assert "msg=" in r.headers["location"], r.headers["location"]


def _owner(db, set_password):
    set_password(db, "manikyamba", OWNER_PASSWORD)
    return make_client("manikyamba", OWNER_PASSWORD)


def _free_maintenance_row(db, flat_id):
    mp = db.scalar(select(MaintenancePayment).where(MaintenancePayment.period_id == _oct(db).id,
                                                    MaintenancePayment.flat_id == flat_id))
    saved = (mp.is_paid, mp.paid_on, mp.amount, mp.payment_note, mp.payment_mode, mp.submitted_by_user_id)
    mp.is_paid, mp.paid_on, mp.submitted_by_user_id = False, None, None
    db.commit()
    return mp, saved


def _restore_maintenance_row(db, mp_id, saved):
    db.expire_all()
    mp = db.get(MaintenancePayment, mp_id)
    mp.is_paid, mp.paid_on, mp.amount, mp.payment_note, mp.payment_mode, mp.submitted_by_user_id = saved
    mp.reverted_at = None
    db.commit()


def test_cancel_maintenance_removed_everywhere(admin_client, seeded_db, set_password):
    owner = _owner(seeded_db, set_password)
    flats = _flats(seeded_db)
    mp, saved = _free_maintenance_row(seeded_db, flats["401"])
    mp_id, period_id = mp.id, mp.period_id
    try:
        before = totals(seeded_db, [flats["401"], flats["101"]])
        r = owner.post("/owner/maintenance", data={"flat_id": flats["401"], "period_id": period_id, "paid_on": "2026-10-06",
                                                   "amount": "2345", "payment_mode": "UPI", "payment_note": "UTR123456789"}, follow_redirects=False)
        assert "owner_msg" in r.headers["location"]
        during = totals(seeded_db, [flats["401"], flats["101"]])
        assert during["fund_balance"] == before["fund_balance"] + 2345

        _cancel(owner, admin_client, seeded_db, "maintenance_payment", mp_id)
        after = totals(seeded_db, [flats["401"], flats["101"]])
        assert after == before
        _pages_without([admin_client, owner], "₹2345", extra=[f"/flats/{flats['401']}", f"/statements/{period_id}"])
    finally:
        _restore_maintenance_row(seeded_db, mp_id, saved)


def test_cancel_in_closed_month_updates_account_balance(admin_client, seeded_db, set_password):
    owner = _owner(seeded_db, set_password)
    flats = _flats(seeded_db)
    mp, saved = _free_maintenance_row(seeded_db, flats["401"])
    mp_id, oct_ = mp.id, _oct(seeded_db)
    oct_id = oct_.id
    periods_before = {p.id for p in _periods(seeded_db)}
    try:
        base = ledger_svc.compute_fund_balance(seeded_db)
        owner.post("/owner/maintenance", data={"flat_id": flats["401"], "period_id": oct_id, "paid_on": "2026-10-06",
                                               "amount": "2345", "payment_mode": "UPI", "payment_note": "UTR123456789"})
        assert admin_client.post(f"/periods/{oct_id}/close", follow_redirects=False).status_code == 303
        seeded_db.expire_all()
        assert seeded_db.get(LedgerPeriod, oct_id).closing_fund_balance == base + 2345
        assert ledger_svc.compute_fund_balance(seeded_db) == base + 2345

        _cancel(owner, admin_client, seeded_db, "maintenance_payment", mp_id)
        seeded_db.expire_all()
        oct_ = seeded_db.get(LedgerPeriod, oct_id)
        nxt = next(p for p in _periods(seeded_db) if p.id not in periods_before)
        assert oct_.closing_fund_balance == base
        assert nxt.opening_fund_balance == base
        assert ledger_svc.compute_fund_balance(seeded_db) == base
        assert ledger_svc.period_fund_summary(seeded_db, oct_)["closing"] == base
        assert f"Closing Account Balance: ₹{base}" in admin_client.get(f"/statements/{oct_id}").text
    finally:
        seeded_db.expire_all()
        for p in _periods(seeded_db):
            if p.id not in periods_before:
                for row in seeded_db.scalars(select(MaintenancePayment).where(MaintenancePayment.period_id == p.id)).all():
                    seeded_db.delete(row)
                seeded_db.delete(p)
        oct_ = seeded_db.get(LedgerPeriod, oct_id)
        oct_.is_closed, oct_.closing_fund_balance = False, None
        seeded_db.commit()
        _restore_maintenance_row(seeded_db, mp_id, saved)


def test_cancel_expense_removed_everywhere(admin_client, seeded_db, set_password):
    owner = _owner(seeded_db, set_password)
    flats = _flats(seeded_db)
    before = totals(seeded_db, [flats["101"]])
    r = owner.post("/owner/expenses", data={"flat_id": flats["101"], "description": "Bulbs Z", "category": "Common",
                                            "amount": "777", "expense_date": "2026-10-04"}, follow_redirects=False)
    assert "owner_msg" in r.headers["location"]
    seeded_db.expire_all()
    exp = seeded_db.scalar(select(Expense).where(Expense.description == "Bulbs Z"))
    during = totals(seeded_db, [flats["101"]])
    assert during["expenses_all"] == before["expenses_all"] + 777
    assert during[f"flat{flats['101']}"]["reimbursement_due"] == before[f"flat{flats['101']}"]["reimbursement_due"] + 777

    _cancel(owner, admin_client, seeded_db, "expense", exp.id)
    assert totals(seeded_db, [flats["101"]]) == before
    _pages_without([admin_client, owner], "Bulbs Z", extra=[f"/flats/{flats['101']}"] + [f"/statements/{p.id}" for p in _periods(seeded_db)])

    seeded_db.delete(seeded_db.get(Expense, exp.id))
    seeded_db.commit()


def test_cancel_corpus_payment_removed_everywhere(admin_client, seeded_db, set_password):
    owner = _owner(seeded_db, set_password)
    flats = _flats(seeded_db)
    r = admin_client.post("/corpus/campaigns", data={"name": "Cancel drive", "target_amount": "50000",
                                                     "due_date": (date.today() + timedelta(days=20)).isoformat()},
                          follow_redirects=False)
    assert r.status_code == 303
    seeded_db.expire_all()
    campaign = seeded_db.scalar(select(CorpusCampaign).where(CorpusCampaign.name == "Cancel drive"))
    try:
        before = totals(seeded_db, [flats["101"]])
        r = owner.post("/corpus/pay", data={"target": f"{flats['101']}:{campaign.id}", "amount": "1234",
                                            "paid_on": date.today().isoformat(), "payment_mode": "UPI", "payment_note": "UTR123456789"}, follow_redirects=False)
        assert "msg=" in r.headers["location"]
        seeded_db.expire_all()
        cp = seeded_db.scalar(select(CorpusPayment).where(CorpusPayment.campaign_id == campaign.id))
        during = totals(seeded_db, [flats["101"]])
        assert during["building_corpus"]["collected"] == before["building_corpus"]["collected"] + 1234

        _cancel(owner, admin_client, seeded_db, "corpus_payment", cp.id)
        assert totals(seeded_db, [flats["101"]]) == before
        _pages_without([admin_client, owner], "₹1234",
                       extra=[f"/flats/{flats['101']}", f"/corpus/campaigns/{campaign.id}"])
    finally:
        seeded_db.expire_all()
        for p in seeded_db.scalars(select(CorpusPayment).where(CorpusPayment.campaign_id == campaign.id)).all():
            seeded_db.delete(p)
        for sh in list(campaign.shares):
            seeded_db.delete(sh)
        seeded_db.delete(campaign)
        seeded_db.commit()


def test_project_payment_revert_removed_everywhere(admin_client, seeded_db, set_password):
    """Owners cannot submit project payments (so no cancel request); the admin revert is the undo path."""
    owner = _owner(seeded_db, set_password)
    flats = _flats(seeded_db)
    r = admin_client.post("/projects", data={"name": "Cancel project", "total_cost": "6170"}, follow_redirects=False)
    assert r.status_code == 303
    seeded_db.expire_all()
    project = seeded_db.scalar(select(Project).where(Project.name == "Cancel project"))
    pp = seeded_db.scalar(select(ProjectPayment).where(ProjectPayment.project_id == project.id, ProjectPayment.flat_id == flats["101"]))
    try:
        before = totals(seeded_db, [flats["101"]])
        admin_client.post(f"/projects/payments/{pp.id}/pay", data={"paid_on": "2026-10-05"})
        assert totals(seeded_db, [flats["101"]]) != before
        admin_client.post(f"/projects/payments/{pp.id}/revert")
        assert totals(seeded_db, [flats["101"]]) == before
        assert "Paid" not in owner.get("/payments/projects").text.split("Cancel project")[1].split("</tr>")[0]
    finally:
        seeded_db.expire_all()
        for row in seeded_db.scalars(select(ProjectPayment).where(ProjectPayment.project_id == project.id)).all():
            seeded_db.delete(row)
        seeded_db.delete(seeded_db.get(Project, project.id))
        seeded_db.commit()
