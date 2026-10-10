import re
from datetime import date, timedelta

from sqlalchemy import select

from app.models import (
    AuditLog,
    CancelRequest,
    CorpusCampaign,
    CorpusPayment,
    Expense,
    Flat,
    LedgerPeriod,
    MaintenancePayment,
)
from app.services import corpus_campaigns as campaigns_svc
from app.services import ledger as ledger_svc
from app.services import reminders as reminders_svc
from app.services.alerts import build_dashboard_alerts
from tests.conftest import OWNER_PASSWORD, make_client


def _flats(db):
    return {f.number: f.id for f in db.scalars(select(Flat)).all()}


def _oct(db):
    return db.scalars(select(LedgerPeriod).where(LedgerPeriod.year == 2026, LedgerPeriod.month == 10)).one()


def _mp(db, period_id, flat_id):
    db.expire_all()
    return db.scalar(
        select(MaintenancePayment).where(
            MaintenancePayment.period_id == period_id, MaintenancePayment.flat_id == flat_id
        )
    )


def _manikyamba(db, set_password):
    set_password(db, "manikyamba", OWNER_PASSWORD)
    return make_client("manikyamba", OWNER_PASSWORD)


def _create_campaign(admin_client, db, name, target, due, **shares):
    data = {"name": name, "target_amount": str(target), "due_date": due.isoformat()}
    data.update(shares)
    r = admin_client.post("/corpus/campaigns", data=data, follow_redirects=False)
    assert r.status_code == 303, r.text
    db.expire_all()
    return db.scalar(select(CorpusCampaign).where(CorpusCampaign.name == name))


def _close_campaign(admin_client, campaign):
    admin_client.post(
        f"/corpus/campaigns/{campaign.id}/update",
        data={
            "name": campaign.name,
            "target_amount": str(campaign.target_amount),
            "due_date": campaign.due_date.isoformat() if campaign.due_date else "",
            "closed": "1",
        },
    )


def test_equal_split_math():
    assert campaigns_svc.equal_split(100000, 5) == [20000] * 5
    assert campaigns_svc.equal_split(10, 3) == [4, 3, 3]
    assert sum(campaigns_svc.equal_split(99999, 5)) == 99999
    today = date(2026, 10, 9)
    assert campaigns_svc.due_status(today - timedelta(days=1), 100, today) == "overdue"
    assert campaigns_svc.due_status(today + timedelta(days=7), 100, today) == "due_soon"
    assert campaigns_svc.due_status(today + timedelta(days=8), 100, today) == "open"
    assert campaigns_svc.due_status(today - timedelta(days=30), 0, today) == "paid"


def test_owner_cannot_submit_for_other_flats(owner_client, seeded_db):
    flats = _flats(seeded_db)
    oct_p = _oct(seeded_db)
    other = flats["301"]
    today = date.today().isoformat()
    posts = [
        ("/owner/maintenance", {"flat_id": other, "period_id": oct_p.id, "paid_on": today, "amount": "1000"}),
        (
            "/owner/expenses",
            {"flat_id": other, "description": "x", "category": "Common", "amount": "10", "expense_date": "2026-10-05"},
        ),
    ]
    for url, data in posts:
        r = owner_client.post(url, data=data, follow_redirects=False)
        assert r.status_code == 403, (url, r.status_code)

    cp = CorpusPayment(flat_id=other, amount=500, is_paid=False, description="Other flat corpus")
    seeded_db.add(cp)
    seeded_db.commit()
    r = owner_client.post(f"/owner/corpus/{cp.id}/pay", data={"amount": "100", "paid_on": today}, follow_redirects=False)
    assert r.status_code == 403
    seeded_db.refresh(cp)
    assert cp.is_paid is False
    seeded_db.delete(cp)
    seeded_db.commit()

    # Owners cannot create campaigns or touch the admin queue.
    assert owner_client.post("/corpus/campaigns", data={"name": "x", "target_amount": "1"}, follow_redirects=False).status_code == 403
    assert owner_client.get("/admin/cancel-requests", follow_redirects=False).status_code == 403
    assert owner_client.post("/admin/cancel-requests/1/approve", follow_redirects=False).status_code == 403
    assert seeded_db.scalar(select(Expense).where(Expense.description == "x")) is None


def test_owner_maintenance_submission_counts_immediately(admin_client, owner_client, seeded_db):
    flats = _flats(seeded_db)
    oct_p = _oct(seeded_db)
    mp = _mp(seeded_db, oct_p.id, flats["102"])
    admin_client.post(f"/maintenance/{mp.id}/revert")
    mp = _mp(seeded_db, oct_p.id, flats["102"])
    assert mp.is_paid is False
    fee = mp.amount
    collected_before = ledger_svc.maintenance_collected(seeded_db, oct_p.id)
    owed_before = ledger_svc.flat_balances(seeded_db, seeded_db.get(Flat, flats["102"]))["owed_maintenance"]
    assert owed_before >= fee

    page = owner_client.get(f"/payments/maintenance?period_id={oct_p.id}")
    assert page.status_code == 200
    assert 'action="/owner/maintenance"' in page.text

    r = owner_client.post(
        "/owner/maintenance",
        data={
            "flat_id": flats["102"],
            "period_id": oct_p.id,
            "paid_on": "2026-10-12",
            "amount": "",
            "payment_mode": "UPI",
            "payment_note": "GPay ref 123",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "owner_msg" in r.headers["location"]
    mp = _mp(seeded_db, oct_p.id, flats["102"])
    assert mp.is_paid and mp.amount == fee and mp.payment_mode == "UPI"
    assert mp.submitted_by.username == "sastry"
    assert ledger_svc.maintenance_collected(seeded_db, oct_p.id) == collected_before + fee
    flat = seeded_db.get(Flat, flats["102"])
    assert ledger_svc.flat_balances(seeded_db, flat)["owed_maintenance"] == owed_before - fee

    # Second submission for the same month is refused.
    r = owner_client.post(
        "/owner/maintenance",
        data={"flat_id": flats["102"], "period_id": oct_p.id, "paid_on": "2026-10-12"},
        follow_redirects=False,
    )
    assert "owner_error" in r.headers["location"]

    admin_page = admin_client.get(f"/payments/maintenance?period_id={oct_p.id}")
    assert "Submitted by owner" in admin_page.text
    log = seeded_db.scalars(
        select(AuditLog).where(AuditLog.entity_type == "maintenance_payment", AuditLog.action == "owner_submit")
    ).all()[-1]
    assert log.user_label == "sastry"


def test_multi_flat_owner_submits_for_both_flats(admin_client, seeded_db, set_password):
    client = _manikyamba(seeded_db, set_password)
    flats = _flats(seeded_db)
    oct_p = _oct(seeded_db)
    page = client.get(f"/payments/maintenance?period_id={oct_p.id}")
    assert re.search(r'<select[^>]*name="flat_id"', page.text)
    for number in ("101", "401"):
        mp = _mp(seeded_db, oct_p.id, flats[number])
        fee = mp.amount
        admin_client.post(f"/maintenance/{mp.id}/revert")
        r = client.post(
            "/owner/maintenance",
            data={"flat_id": flats[number], "period_id": oct_p.id, "paid_on": "2026-10-11", "amount": f"{fee:,}", "payment_mode": "Cash"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "owner_msg" in r.headers["location"], r.headers["location"]
        mp = _mp(seeded_db, oct_p.id, flats[number])
        assert mp.is_paid and mp.amount == fee and mp.payment_mode == "Cash"
    r = client.post(
        "/owner/maintenance",
        data={"flat_id": flats["102"], "period_id": oct_p.id, "paid_on": "2026-10-11"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_owner_expense_lands_as_unsettled_advance(owner_client, admin_client, seeded_db):
    flats = _flats(seeded_db)
    flat = seeded_db.get(Flat, flats["102"])
    adv_before = ledger_svc.flat_balances(seeded_db, flat)["reimbursement_due"]
    r = owner_client.post(
        "/owner/expenses",
        data={
            "flat_id": flats["102"],
            "description": "Bulbs for stairs",
            "category": "Not a real category",
            "amount": "640",
            "expense_date": "2026-10-06",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and "owner_msg" in r.headers["location"]
    seeded_db.expire_all()
    exp = seeded_db.scalar(select(Expense).where(Expense.description == "Bulbs for stairs"))
    assert exp.is_settled is False and exp.paid_by_flat_id == flats["102"]
    assert exp.category == "Others"
    assert exp.submitted_by.username == "sastry"
    assert exp in ledger_svc.unsettled_expenses(seeded_db)
    assert ledger_svc.flat_balances(seeded_db, flat)["reimbursement_due"] == adv_before + 640
    assert "Submitted by owner" in admin_client.get(f"/expenses?period_id={exp.period_id}").text

    r = owner_client.post(
        "/owner/expenses",
        data={"flat_id": flats["102"], "description": "Far future", "amount": "10", "expense_date": "2031-01-01"},
        follow_redirects=False,
    )
    assert "owner_error" in r.headers["location"]


def test_legacy_corpus_item_pay_goes_to_general_corpus(owner_client, seeded_db):
    flats = _flats(seeded_db)
    oct_id = _oct(seeded_db).id
    general_before = campaigns_svc.general_campaign(seeded_db)
    share_before = (
        campaigns_svc.flat_share(seeded_db, general_before.id, flats["102"]).share_amount if general_before else 0
    )
    cp = CorpusPayment(flat_id=flats["102"], period_id=oct_id, amount=5000, is_paid=False, description="Lift fund")
    seeded_db.add(cp)
    seeded_db.commit()
    cp_id = cp.id

    too_much = owner_client.post(f"/owner/corpus/{cp_id}/pay", data={"amount": "6000", "paid_on": "2026-10-09"}, follow_redirects=False)
    assert "owner_error" in too_much.headers["location"]
    seeded_db.expire_all()
    assert seeded_db.get(CorpusPayment, cp_id) is None, "line item is folded into the General corpus share"

    general = campaigns_svc.general_campaign(seeded_db)
    page = owner_client.get("/payments/corpus")
    assert f'value="{flats["102"]}:{general.id}"' in page.text

    r = owner_client.post(
        "/corpus/pay",
        data={"target": f"{flats['102']}:{general.id}", "amount": "2000", "paid_on": "2026-10-09", "payment_mode": "UPI"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    seeded_db.expire_all()
    share = campaigns_svc.flat_share(seeded_db, general.id, flats["102"])
    assert share.share_amount == share_before + 5000
    assert campaigns_svc.remaining_for(seeded_db, general, flats["102"]) == share_before + 3000

    reminders = reminders_svc.reminders_for_flats(seeded_db, [flats["102"]])
    assert sum(i["amount"] for i in reminders["items"] if i["kind"] == "Corpus" and "General" in i["label"]) == share_before + 3000

    for p in seeded_db.scalars(select(CorpusPayment).where(CorpusPayment.campaign_id == general.id)).all():
        seeded_db.delete(p)
    if general_before is None:
        for sh in list(general.shares):
            seeded_db.delete(sh)
        seeded_db.delete(general)
    else:
        share.share_amount = share_before
    seeded_db.commit()


def test_campaign_partial_payments_and_totals(admin_client, owner_client, seeded_db):
    flats = _flats(seeded_db)
    due = date.today() + timedelta(days=30)
    campaign = _create_campaign(admin_client, seeded_db, "Painting 2026", 100000, due)
    summary = campaigns_svc.campaign_summary(seeded_db, campaign)
    assert [r["share"] for r in summary["rows"]] == [20000] * 5
    assert summary["share_total"] == 100000 and summary["target_gap"] == 0

    flat = seeded_db.get(Flat, flats["102"])
    owed_before = ledger_svc.flat_balances(seeded_db, flat)["owed_corpus"]

    def pay(amount, flat_id=flats["102"]):
        return owner_client.post(
            f"/corpus/campaigns/{campaign.id}/pay",
            data={"flat_id": flat_id, "amount": str(amount), "paid_on": date.today().isoformat(), "payment_mode": "UPI"},
            follow_redirects=False,
        )

    assert "msg=" in pay(10000).headers["location"]
    assert campaigns_svc.remaining_for(seeded_db, campaign, flats["102"]) == 10000
    assert ledger_svc.flat_balances(seeded_db, flat)["owed_corpus"] == owed_before - 10000

    assert "error=" in pay(10001).headers["location"]
    assert campaigns_svc.remaining_for(seeded_db, campaign, flats["102"]) == 10000
    assert pay(500, flat_id=flats["301"]).status_code == 403

    page = owner_client.get(f"/corpus/campaigns/{campaign.id}")
    assert page.status_code == 200 and "Painting 2026" in page.text

    assert "msg=" in pay(10000).headers["location"]
    assert campaigns_svc.remaining_for(seeded_db, campaign, flats["102"]) == 0
    assert "error=" in pay(1).headers["location"]

    seeded_db.expire_all()
    summary = campaigns_svc.campaign_summary(seeded_db, campaign)
    assert summary["paid_total"] == 20000 and summary["remaining_total"] == 80000
    assert summary["progress_pct"] == 20

    status = ledger_svc.build_dashboard_status(seeded_db, _oct(seeded_db))
    assert status["totals"]["campaigns_pending"][campaign.id] == 80000

    alerts = build_dashboard_alerts(seeded_db, _oct(seeded_db))
    corpus_alert = next(a for a in alerts if "Painting 2026" in a["title"])
    assert "301" in corpus_alert["detail"] and "102" not in corpus_alert["detail"]

    # Admin edits a share; custom shares at creation are honoured too.
    custom = _create_campaign(
        admin_client, seeded_db, "Borewell", 30000, due, **{f"share_{flats['101']}": "10000", f"share_{flats['401']}": "0"}
    )
    shares = {r["flat"].number: r["share"] for r in campaigns_svc.campaign_summary(seeded_db, custom)["rows"]}
    assert shares["101"] == 10000 and shares["401"] == 0 and shares["102"] == 6000

    _close_campaign(admin_client, campaign)
    _close_campaign(admin_client, custom)
    seeded_db.expire_all()
    # Closed collections stop counting as dues.
    assert ledger_svc.flat_balances(seeded_db, flat)["owed_corpus"] == owed_before - 20000
    assert "error=" in pay(1).headers["location"]


def test_reminders_highlight_overdue_and_due_soon(admin_client, owner_client, seeded_db):
    flats = _flats(seeded_db)
    overdue = _create_campaign(admin_client, seeded_db, "Overdue drive", 5000, date.today() - timedelta(days=2))
    soon = _create_campaign(admin_client, seeded_db, "Soon drive", 5000, date.today() + timedelta(days=3))

    rem = reminders_svc.reminders_for_flats(seeded_db, [flats["102"]])
    statuses = {i["label"]: i["status"] for i in rem["items"] if i["kind"] == "Corpus"}
    assert any("Overdue drive" in k and v == "overdue" for k, v in statuses.items())
    assert any("Soon drive" in k and v == "due_soon" for k, v in statuses.items())
    assert rem["overdue"] >= 1 and rem["due_soon"] >= 1

    dash = owner_client.get("/")
    assert dash.status_code == 200
    assert "Overdue drive" in dash.text and "reminder-overdue" in dash.text
    flat_page = owner_client.get(f"/flats/{flats['102']}")
    assert flat_page.status_code == 200 and "Soon drive" in flat_page.text
    assert owner_client.get(f"/flats/{flats['301']}", follow_redirects=False).status_code == 403

    admin_dash = admin_client.get("/")
    assert "Corpus due" in admin_dash.text and "Overdue drive" in admin_dash.text

    _close_campaign(admin_client, overdue)
    _close_campaign(admin_client, soon)


def test_cancel_request_flow(admin_client, owner_client, seeded_db, set_password):
    flats = _flats(seeded_db)
    flat = seeded_db.get(Flat, flats["102"])
    r = owner_client.post(
        "/owner/expenses",
        data={"flat_id": flats["102"], "description": "Wrong entry", "category": "Common", "amount": "999", "expense_date": "2026-10-07"},
        follow_redirects=False,
    )
    assert "owner_msg" in r.headers["location"]
    seeded_db.expire_all()
    exp = seeded_db.scalar(select(Expense).where(Expense.description == "Wrong entry"))
    adv = ledger_svc.flat_balances(seeded_db, flat)["reimbursement_due"]

    # Another owner cannot cancel it; admin-created records cannot be cancelled by owners.
    other = _manikyamba(seeded_db, set_password)
    r = other.post("/owner/cancel-requests", data={"record_type": "expense", "record_id": exp.id}, follow_redirects=False)
    assert "owner_error" in r.headers["location"]
    salary = seeded_db.scalar(select(Expense).where(Expense.description == "Watchman salary"))
    r = owner_client.post("/owner/cancel-requests", data={"record_type": "expense", "record_id": salary.id}, follow_redirects=False)
    assert "owner_error" in r.headers["location"]

    r = owner_client.post(
        "/owner/cancel-requests",
        data={"record_type": "expense", "record_id": exp.id, "reason": "Typed wrong amount", "back": "/expenses"},
        follow_redirects=False,
    )
    assert r.headers["location"].startswith("/expenses") and "owner_msg" in r.headers["location"]
    dup = owner_client.post("/owner/cancel-requests", data={"record_type": "expense", "record_id": exp.id}, follow_redirects=False)
    assert "owner_error" in dup.headers["location"]

    seeded_db.expire_all()
    req = seeded_db.scalar(select(CancelRequest).where(CancelRequest.record_id == exp.id, CancelRequest.record_type == "expense"))
    assert req.status == "pending"
    assert ledger_svc.flat_balances(seeded_db, flat)["reimbursement_due"] == adv
    assert "Cancel requested" in admin_client.get(f"/expenses?period_id={exp.period_id}").text
    subs = owner_client.get("/owner/submissions")
    assert subs.status_code == 200 and "Wrong entry" in subs.text and "Cancel requested" in subs.text
    queue = admin_client.get("/admin/cancel-requests")
    assert queue.status_code == 200 and "Typed wrong amount" in queue.text
    assert "cancel requests waiting" in admin_client.get("/").text.lower()

    r = admin_client.post(f"/admin/cancel-requests/{req.id}/approve", data={"note": "ok"}, follow_redirects=False)
    assert r.status_code == 303
    seeded_db.expire_all()
    assert seeded_db.get(Expense, exp.id).deleted_at is not None
    assert seeded_db.get(CancelRequest, req.id).status == "approved"
    assert ledger_svc.flat_balances(seeded_db, flat)["reimbursement_due"] == adv - 999
    assert seeded_db.scalar(
        select(AuditLog).where(AuditLog.entity_type == "expense", AuditLog.entity_id == exp.id, AuditLog.action == "delete")
    )
    assert "approved" in owner_client.get("/owner/submissions").text.lower()
    again = admin_client.post(f"/admin/cancel-requests/{req.id}/reject", follow_redirects=False)
    assert "error=" in again.headers["location"]

    # Reject keeps the record; approve on a campaign payment removes it.
    campaign = _create_campaign(admin_client, seeded_db, "Cancel drive", 5000, date.today() + timedelta(days=20))
    owner_client.post(
        f"/corpus/campaigns/{campaign.id}/pay",
        data={"flat_id": flats["102"], "amount": "400", "paid_on": date.today().isoformat()},
    )
    seeded_db.expire_all()
    cp = seeded_db.scalar(select(CorpusPayment).where(CorpusPayment.campaign_id == campaign.id))
    cp_id = cp.id
    owner_client.post("/owner/cancel-requests", data={"record_type": "corpus_payment", "record_id": cp.id})
    seeded_db.expire_all()
    req2 = seeded_db.scalar(select(CancelRequest).where(CancelRequest.record_type == "corpus_payment", CancelRequest.record_id == cp.id))
    admin_client.post(f"/admin/cancel-requests/{req2.id}/reject", data={"note": "Payment confirmed"})
    seeded_db.expire_all()
    assert seeded_db.get(CancelRequest, req2.id).status == "rejected"
    assert campaigns_svc.remaining_for(seeded_db, campaign, flats["102"]) == 600
    assert "rejected" in owner_client.get("/owner/submissions").text.lower()

    owner_client.post("/owner/cancel-requests", data={"record_type": "corpus_payment", "record_id": cp.id})
    seeded_db.expire_all()
    req3 = seeded_db.scalar(
        select(CancelRequest).where(
            CancelRequest.record_type == "corpus_payment", CancelRequest.record_id == cp.id, CancelRequest.status == "pending"
        )
    )
    admin_client.post(f"/admin/cancel-requests/{req3.id}/approve")
    seeded_db.expire_all()
    assert seeded_db.scalar(select(CorpusPayment).where(CorpusPayment.id == cp_id)) is None
    assert campaigns_svc.remaining_for(seeded_db, campaign, flats["102"]) == 1000
    _close_campaign(admin_client, campaign)


def test_cancel_maintenance_reverts_to_unpaid(admin_client, owner_client, seeded_db):
    flats = _flats(seeded_db)
    oct_p = _oct(seeded_db)
    mp = _mp(seeded_db, oct_p.id, flats["102"])
    if mp.is_paid:
        admin_client.post(f"/maintenance/{mp.id}/revert")
    owner_client.post(
        "/owner/maintenance",
        data={"flat_id": flats["102"], "period_id": oct_p.id, "paid_on": "2026-10-12", "payment_mode": "UPI"},
    )
    mp = _mp(seeded_db, oct_p.id, flats["102"])
    assert mp.is_paid
    collected = ledger_svc.maintenance_collected(seeded_db, oct_p.id)
    owner_client.post("/owner/cancel-requests", data={"record_type": "maintenance_payment", "record_id": mp.id})
    seeded_db.expire_all()
    req = seeded_db.scalar(
        select(CancelRequest).where(
            CancelRequest.record_type == "maintenance_payment", CancelRequest.record_id == mp.id, CancelRequest.status == "pending"
        )
    )
    assert ledger_svc.maintenance_collected(seeded_db, oct_p.id) == collected
    admin_client.post(f"/admin/cancel-requests/{req.id}/approve")
    mp = _mp(seeded_db, oct_p.id, flats["102"])
    assert mp.is_paid is False and mp.submitted_by_user_id is None
    assert ledger_svc.maintenance_collected(seeded_db, oct_p.id) == collected - mp.amount
    assert "approved" in owner_client.get("/owner/submissions").text.lower()

    rem = reminders_svc.reminders_for_flats(seeded_db, [flats["102"]])
    assert any(i["kind"] == "Maintenance" for i in rem["items"])
    # Leave the shared test DB the way the seed had it.
    admin_client.post(f"/maintenance/{mp.id}/pay", data={"paid_on": "2026-10-10"})
