"""v6 features: owner project payments, expense categories, edit/delete projects & goals,
dashboard balances, owner advances, admin Actions hub."""
from datetime import date, timedelta

from sqlalchemy import select

from app.models import (
    AuditLog,
    CancelRequest,
    CorpusCampaign,
    CorpusPayment,
    Expense,
    ExpenseCategory,
    Flat,
    Project,
    ProjectContribution,
    ProjectPayment,
    Settlement,
)
from app.services import balances as balances_svc
from app.services import corpus_campaigns as campaigns_svc
from app.services import expense_categories as expense_cats_svc
from app.services import ledger as ledger_svc
from app.services import reminders as reminders_svc
from tests.conftest import OWNER_PASSWORD, make_client

SEED_BALANCE = 10955


def _flats(db):
    return {f.number: f.id for f in db.scalars(select(Flat)).all()}


def _create_project(admin_client, db, name, total="10000", due=""):
    r = admin_client.post("/projects", data={"name": name, "total_cost": total, "due_date": due}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    return db.scalar(select(Project).where(Project.name == name, Project.deleted_at.is_(None)))


def _share(db, project, flat_id):
    db.expire_all()
    return db.scalar(select(ProjectPayment).where(ProjectPayment.project_id == project.id,
                                                  ProjectPayment.flat_id == flat_id))


def _drop_project(db, project_id):
    db.expire_all()
    for req in db.scalars(select(CancelRequest).where(CancelRequest.record_type == "project_contribution")).all():
        db.delete(req)
    for row in db.scalars(select(ProjectContribution).where(ProjectContribution.project_id == project_id)).all():
        db.delete(row)
    db.flush()
    for row in db.scalars(select(ProjectPayment).where(ProjectPayment.project_id == project_id)).all():
        db.delete(row)
    project = db.get(Project, project_id)
    if project:
        db.delete(project)
    db.commit()


def _pay(client, share_id, amount, back="/payments/projects"):
    return client.post("/owner/projects/pay", data={"share_id": share_id, "amount": str(amount), "paid_on": "2026-10-05",
                                                     "payment_mode": "UPI", "payment_note": "REF123456789", "back": back},
                       follow_redirects=False)


# ---- 3. owners pay project shares ------------------------------------------------------


def test_owner_pays_project_share_in_parts(seeded_db, admin_client, owner_client):
    flats = _flats(seeded_db)
    project = _create_project(admin_client, seeded_db, "Water softener v6", due=(date.today() - timedelta(days=3)).isoformat())
    try:
        pp = _share(seeded_db, project, flats["102"])
        tab = owner_client.get("/payments/projects").text
        assert "Pay a project share" in tab and f'value="{pp.id}"' in tab
        other = _share(seeded_db, project, flats["301"])
        assert f'value="{other.id}"' not in tab
        assert _pay(owner_client, other.id, 100).status_code == 403

        r = _pay(owner_client, pp.id, 500)
        assert "owner_msg" in r.headers["location"]
        pp = _share(seeded_db, project, flats["102"])
        assert (pp.paid, pp.remaining, pp.status, pp.is_paid) == (500, 1500, "part", False)
        flat = seeded_db.get(Flat, flats["102"])
        assert ledger_svc.flat_balances(seeded_db, flat)["owed_projects"] == 1500
        items = reminders_svc.reminders_for_flats(seeded_db, [flats["102"]])["items"]
        proj_item = next(i for i in items if i["kind"] == "Project" and "Water softener v6" in i["label"])
        assert proj_item["amount"] == 1500 and proj_item["status"] == "overdue"
        assert "Part paid" in owner_client.get("/payments/projects").text
        assert "₹500" in admin_client.get("/projects").text
        admin_tab = admin_client.get("/payments/projects").text
        assert "Submitted by owner" in admin_tab and "sastry" in admin_tab
        assert "Part paid" in admin_client.get("/").text

        r = _pay(owner_client, pp.id, 2000)
        assert "owner_error" in r.headers["location"] and "1500" in r.headers["location"]
        assert "owner_error" in _pay(owner_client, pp.id, 0).headers["location"]

        r = _pay(owner_client, pp.id, 1500, back=f"/projects/{project.id}")
        assert r.headers["location"].startswith(f"/projects/{project.id}?owner_msg")
        pp = _share(seeded_db, project, flats["102"])
        assert pp.status == "paid" and pp.is_paid and pp.remaining == 0
        r = _pay(owner_client, pp.id, 1)
        assert "already+fully+paid" in r.headers["location"]

        last = seeded_db.scalar(select(ProjectContribution).where(ProjectContribution.project_payment_id == pp.id,
                                                                  ProjectContribution.amount == 1500))
        assert last.submitted_by_user_id is not None
        r = owner_client.post("/owner/cancel-requests", data={"record_type": "project_contribution", "record_id": last.id,
                                                              "reason": "typo", "back": "/payments/projects"},
                              follow_redirects=False)
        assert "owner_msg" in r.headers["location"]
        assert "Cancel requested" in owner_client.get("/payments/projects").text
        req = seeded_db.scalar(select(CancelRequest).where(CancelRequest.record_type == "project_contribution",
                                                           CancelRequest.status == "pending"))
        assert "Water softener v6" in req.summary
        assert "Project payment" in owner_client.get("/owner/submissions").text
        admin_client.post(f"/admin/cancel-requests/{req.id}/approve", data={"note": "ok"})
        pp = _share(seeded_db, project, flats["102"])
        assert (pp.paid, pp.status) == (500, "part")

        admin_client.post(f"/projects/payments/{pp.id}/pay", data={"paid_on": "2026-10-06"})
        pp = _share(seeded_db, project, flats["102"])
        assert pp.status == "paid" and len(pp.contributions) == 2
        admin_client.post(f"/projects/payments/{pp.id}/revert")
        pp = _share(seeded_db, project, flats["102"])
        assert (pp.paid, pp.status, pp.paid_on) == (0, "pending", None) and not pp.contributions
    finally:
        _drop_project(seeded_db, project.id)
    assert ledger_svc.compute_fund_balance(seeded_db) == SEED_BALANCE


def test_project_payment_in_pdf_and_insights(seeded_db, admin_client, owner_client):
    flats = _flats(seeded_db)
    project = _create_project(admin_client, seeded_db, "Pdf project v6")
    try:
        pp = _share(seeded_db, project, flats["102"])
        _pay(owner_client, pp.id, 700)
        r = owner_client.get(f"/flats/{flats['102']}/statement.pdf?period_id=")
        assert r.status_code == 200 and r.content.startswith(b"%PDF")
        from app.services import insights as insights_svc

        data = insights_svc.flat_insights(seeded_db, [flats["102"]])
        assert data is not None
    finally:
        _drop_project(seeded_db, project.id)


# ---- 4. expense categories -------------------------------------------------------------


def test_admin_manages_expense_categories(seeded_db, admin_client, owner_client):
    flats = _flats(seeded_db)
    page = admin_client.get("/admin/expense-categories").text
    for preset in ("Common", "Watchman Salary", "Common Electricity", "Others"):
        assert preset in page
    assert owner_client.get("/admin/expense-categories").status_code == 403
    assert owner_client.post("/admin/expense-categories", data={"name": "x"}).status_code == 403

    exp = Expense(description="Old salary v6", category="Watchman Salary", amount=10, expense_date=date(2026, 10, 2),
                  is_settled=False, paid_by_flat_id=flats["102"])
    seeded_db.add(exp)
    seeded_db.commit()
    try:
        r = admin_client.post("/admin/expense-categories", data={"name": "Lift AMC"}, follow_redirects=False)
        assert "msg=" in r.headers["location"]
        assert "already+exists" in admin_client.post("/admin/expense-categories", data={"name": "lift amc"},
                                                     follow_redirects=False).headers["location"]
        seeded_db.expire_all()
        lift = expense_cats_svc.by_name(seeded_db, "Lift AMC")
        assert lift.owner_allowed is False
        assert '<option value="Lift AMC">' in admin_client.get("/expenses").text
        owner_form = owner_client.get("/payments/expenses").text
        assert '<option value="Lift AMC">' not in owner_form and '<option value="Common">' in owner_form
        owner_client.post("/owner/expenses", data={"flat_id": flats["102"], "description": "Sneaky v6",
                                                   "category": "Lift AMC", "amount": "5", "expense_date": "2026-10-03"})
        seeded_db.expire_all()
        assert seeded_db.scalar(select(Expense).where(Expense.description == "Sneaky v6")).category == "Others"

        admin_client.post(f"/admin/expense-categories/{lift.id}/set", data={"field": "owner_allowed", "value": "1"})
        assert '<option value="Lift AMC">' in owner_client.get("/payments/expenses").text

        salary = expense_cats_svc.by_name(seeded_db, "Watchman Salary")
        admin_client.post(f"/admin/expense-categories/{salary.id}/set", data={"field": "is_active", "value": "0"})
        page = admin_client.get("/expenses?period_id=" + str(exp.period_id or "")).text if exp.period_id else admin_client.get("/expenses").text
        assert '<option value="Watchman Salary">' not in page
        seeded_db.expire_all()
        assert seeded_db.get(Expense, exp.id).category == "Watchman Salary"
        assert "Watchman Salary" not in expense_cats_svc.active_names(seeded_db)
        assert "Watchman Salary" in [r["category"].name for r in expense_cats_svc.all_rows(seeded_db)]

        r = admin_client.post(f"/admin/expense-categories/{salary.id}/rename", data={"name": "Security salary"},
                              follow_redirects=False)
        assert "keep+the+old+name" in r.headers["location"]
        seeded_db.expire_all()
        assert seeded_db.get(Expense, exp.id).category == "Watchman Salary"
        admin_client.post(f"/admin/expense-categories/{salary.id}/rename",
                          data={"name": "Watchman Salary", "update_existing": ""})
        r = admin_client.post(f"/admin/expense-categories/{salary.id}/rename",
                              data={"name": "Security salary", "update_existing": "1"}, follow_redirects=False)
        assert "existing+expense" in r.headers["location"]
        seeded_db.expire_all()
        assert seeded_db.get(Expense, exp.id).category == "Security salary"
        assert seeded_db.scalar(select(AuditLog).where(AuditLog.entity_type == "expense_category",
                                                       AuditLog.action == "rename")) is not None
    finally:
        seeded_db.expire_all()
        for e in seeded_db.scalars(select(Expense).where(Expense.description.in_(["Old salary v6", "Sneaky v6"]))).all():
            seeded_db.delete(e)
        salary = seeded_db.scalar(select(ExpenseCategory).where(ExpenseCategory.name.in_(["Security salary", "Watchman Salary"])))
        salary.name, salary.is_active = "Watchman Salary", True
        lift = expense_cats_svc.by_name(seeded_db, "Lift AMC")
        if lift:
            seeded_db.delete(lift)
        seeded_db.commit()


# ---- 5. edit / delete projects and corpus goals ----------------------------------------


def test_admin_edits_and_deletes_project(seeded_db, admin_client, owner_client):
    flats = _flats(seeded_db)
    project = _create_project(admin_client, seeded_db, "Edit me v6")
    try:
        r = admin_client.post(f"/projects/{project.id}/edit", data={"name": "Edited v6", "total_cost": "15000",
                                                                    "due_date": "2026-12-31"}, follow_redirects=False)
        assert "msg=" in r.headers["location"]
        seeded_db.expire_all()
        project = seeded_db.get(Project, project.id)
        assert (project.name, project.share_per_flat, project.due_date) == ("Edited v6", 3000, date(2026, 12, 31))
        assert {pp.amount for pp in project.payments} == {3000}

        pp = _share(seeded_db, project, flats["102"])
        _pay(owner_client, pp.id, 2500)
        r = admin_client.post(f"/projects/{project.id}/edit", data={"name": "Edited v6", "total_cost": "10000"},
                              follow_redirects=False)
        assert "error=" in r.headers["location"] and "102" in r.headers["location"]

        detail = admin_client.get(f"/projects/{project.id}").text
        assert "Delete is blocked" in detail
        r = admin_client.post(f"/projects/{project.id}/delete", data={"confirm": "DELETE"}, follow_redirects=False)
        assert "error=" in r.headers["location"] and "payment" in r.headers["location"]
        assert owner_client.post(f"/projects/{project.id}/delete", data={"confirm": "DELETE"}).status_code == 403
        assert owner_client.post(f"/projects/{project.id}/edit", data={"name": "x", "total_cost": "1"}).status_code == 403

        admin_client.post(f"/projects/payments/{pp.id}/revert")
        r = admin_client.post(f"/projects/{project.id}/delete", data={"confirm": "nope"}, follow_redirects=False)
        assert "DELETE" in r.headers["location"]
        r = admin_client.post(f"/projects/{project.id}/delete", data={"confirm": "DELETE"}, follow_redirects=False)
        assert r.headers["location"].startswith("/projects?msg=")
        assert "Edited v6" not in admin_client.get("/projects").text
        assert admin_client.get(f"/projects/{project.id}").status_code == 404
        actions = {a.action for a in seeded_db.scalars(select(AuditLog).where(AuditLog.entity_type == "project",
                                                                               AuditLog.entity_id == project.id)).all()}
        assert {"create", "update", "delete"} <= actions
    finally:
        _drop_project(seeded_db, project.id)


def test_admin_deletes_corpus_goal_with_explicit_confirm(seeded_db, admin_client, owner_client):
    flats = _flats(seeded_db)
    admin_client.post("/corpus/campaigns", data={"name": "Delete goal v6", "target_amount": "5000"})
    seeded_db.expire_all()
    goal = seeded_db.scalar(select(CorpusCampaign).where(CorpusCampaign.name == "Delete goal v6"))
    goal_id = goal.id
    try:
        admin_client.post(f"/corpus/campaigns/{goal_id}/pay", data={"flat_id": flats["102"], "amount": "100",
                                                                    "paid_on": "2026-10-05"})
        seeded_db.expire_all()
        pay = seeded_db.scalar(select(CorpusPayment).where(CorpusPayment.campaign_id == goal_id))
        assert pay is not None
        assert "also remove 1 payment(s)" in admin_client.get(f"/corpus/goals/{goal_id}").text
        r = admin_client.post(f"/corpus/goals/{goal_id}/delete", data={"confirm": "DELETE"}, follow_redirects=False)
        assert "error=" in r.headers["location"] and "payment" in r.headers["location"]
        assert owner_client.post(f"/corpus/goals/{goal_id}/delete", data={"confirm": "DELETE"}).status_code == 403
        admin_client.post(f"/corpus/campaigns/{goal_id}/payments/{pay.id}/delete")
        r = admin_client.post(f"/corpus/goals/{goal_id}/delete", data={"confirm": ""}, follow_redirects=False)
        assert "DELETE" in r.headers["location"]
        r = admin_client.post(f"/corpus/goals/{goal_id}/delete", data={"confirm": "DELETE"}, follow_redirects=False)
        assert r.headers["location"].startswith("/corpus?msg=")
        seeded_db.expire_all()
        assert seeded_db.get(CorpusCampaign, goal_id) is None
        assert seeded_db.scalar(select(AuditLog).where(AuditLog.entity_type == "corpus_campaign",
                                                       AuditLog.entity_id == goal_id, AuditLog.action == "delete"))
    finally:
        seeded_db.expire_all()
        left = seeded_db.get(CorpusCampaign, goal_id)
        if left:
            for row in list(left.payments) + list(left.shares) + list(left.line_items):
                seeded_db.delete(row)
            seeded_db.flush()
            seeded_db.delete(left)
            seeded_db.commit()


# ---- 6/7. balances, settlement card, owner advances -------------------------------------


def _add_advance(db, flat_id, desc, amount, day=2):
    period = campaigns_svc.period_for_date(db, date(2026, 10, day))
    exp = Expense(description=desc, category="Common", amount=amount, expense_date=date(2026, 10, day),
                  is_settled=False, paid_by_flat_id=flat_id, period_id=period.id if period else None)
    db.add(exp)
    db.commit()
    return exp


def test_dashboard_balances_and_settlement_card(seeded_db, admin_client, owner_client):
    flats = _flats(seeded_db)
    held = ledger_svc.compute_fund_balance(seeded_db)
    small = _add_advance(seeded_db, flats["102"], "Small bulb v6", 300, day=1)
    big = _add_advance(seeded_db, flats["301"], "Big pump v6", held + 5000, day=3)
    try:
        bal = balances_svc.overview(seeded_db)
        assert bal["held"] == held and bal["owed"] >= 300 + held + 5000
        assert bal["effective"] == bal["held"] - bal["owed"] and bal["negative_if_all"]
        assert bal["state"] == "some" and bal["settle_now"] < bal["owed"]
        page = admin_client.get("/").text
        for label in ("Account Balance (money held)", "Unsettled owner expenses", "Effective balance after settling",
                      "Available to settle now"):
            assert label in page
        assert f"₹{bal['effective']}" in page and "would take the Account Balance to" in page
        assert "Small bulb v6" in page and "Big pump v6" in page and "Needs ₹" in page
        assert f'action="/settlements/{small.id}/settle"' in page
        assert f'action="/settlements/{big.id}/settle"' not in page

        r = admin_client.post(f"/settlements/{small.id}/settle", data={"settled_on": date.today().isoformat(), "back": "/"},
                              follow_redirects=False)
        assert r.headers["location"].startswith("/?msg=")
        r = admin_client.post(f"/settlements/{big.id}/settle", data={"settled_on": date.today().isoformat(), "back": "/"},
                              follow_redirects=False)
        assert r.headers["location"].startswith("/?error=")
        assert ledger_svc.compute_fund_balance(seeded_db) == held - 300
    finally:
        seeded_db.expire_all()
        for st in seeded_db.scalars(select(Settlement).where(Settlement.expense_id.in_([small.id, big.id]))).all():
            seeded_db.delete(st)
        for e in (small, big):
            seeded_db.delete(seeded_db.get(Expense, e.id))
        seeded_db.commit()
    assert ledger_svc.compute_fund_balance(seeded_db) == SEED_BALANCE


def test_balance_states(seeded_db):
    flats = _flats(seeded_db)
    held = ledger_svc.compute_fund_balance(seeded_db)
    base = balances_svc.overview(seeded_db)
    extra = []
    try:
        if base["state"] == "nothing":
            extra.append(_add_advance(seeded_db, flats["102"], "Fits v6", 100))
            assert balances_svc.overview(seeded_db)["state"] == "all"
        extra.append(_add_advance(seeded_db, flats["102"], "Too big v6", held + 1, day=1))
        state = balances_svc.overview(seeded_db)["state"]
        assert state in ("none", "some")
    finally:
        for e in extra:
            seeded_db.delete(seeded_db.get(Expense, e.id))
        seeded_db.commit()


def test_owner_sees_own_unsettled_expenses(seeded_db, admin_client, owner_client):
    flats = _flats(seeded_db)
    mine = _add_advance(seeded_db, flats["102"], "Mine v6", 450)
    theirs = _add_advance(seeded_db, flats["301"], "Theirs v6", 999)
    try:
        expected = balances_svc.owner_advances(seeded_db, [flats["102"]])["owed"]
        for url in ("/", f"/flats/{flats['102']}"):
            page = owner_client.get(url).text
            assert "owed back to you" in page and f"₹{expected}" in page
            assert "Mine v6" in page and "Waiting to be settled" in page
            assert "Theirs v6" not in page
        admin_client.post(f"/settlements/{mine.id}/settle", data={"settled_on": date.today().isoformat()})
        page = owner_client.get(f"/flats/{flats['102']}").text
        assert "Mine v6" in page and "Settled" in page
    finally:
        seeded_db.expire_all()
        for st in seeded_db.scalars(select(Settlement).where(Settlement.expense_id == mine.id)).all():
            seeded_db.delete(st)
        for e in (mine, theirs):
            seeded_db.delete(seeded_db.get(Expense, e.id))
        seeded_db.commit()
    assert ledger_svc.compute_fund_balance(seeded_db) == SEED_BALANCE


# ---- 8. Actions hub --------------------------------------------------------------------


def test_actions_hub(seeded_db, admin_client, owner_client, set_password):
    flats = _flats(seeded_db)
    project = _create_project(admin_client, seeded_db, "Overdue v6", due=(date.today() - timedelta(days=10)).isoformat())
    exp = _add_advance(seeded_db, flats["102"], "Hub advance v6", 120)
    set_password(seeded_db, "pavan.kumar", "temp-pass-123", must_change=True)
    try:
        pp = _share(seeded_db, project, flats["102"])
        _pay(owner_client, pp.id, 100)
        contrib = seeded_db.scalar(select(ProjectContribution).where(ProjectContribution.project_payment_id == pp.id))
        owner_client.post("/owner/cancel-requests", data={"record_type": "project_contribution", "record_id": contrib.id})

        assert owner_client.get("/admin/actions").status_code == 403
        page = admin_client.get("/admin/actions").text
        for title in ("Cancel requests waiting", "Unsettled owner expenses to settle", "Overdue maintenance",
                      "Overdue corpus", "Overdue project shares", "Overdue asset tasks",
                      "New owner submissions to review", "Users needing a password reset"):
            assert title in page
        assert 'href="/admin/cancel-requests"' in page and "Overdue v6" in page
        assert f'href="/projects/{project.id}"' in page and 'href="/settlements"' in page
        assert "Hub advance v6" in page and "pavan.kumar" in page and "temporary password" in page
        assert "Project payment by sastry" in page and 'href="/payments/projects"' in page

        dash = admin_client.get("/").text
        assert 'href="/admin/actions"' in dash and "Open the full list" in dash
        assert "Open the full list" not in owner_client.get("/").text

        r = admin_client.post("/admin/actions/submissions-reviewed", follow_redirects=False)
        assert r.headers["location"].startswith("/admin/actions?msg=")
        assert "Project payment by sastry" not in admin_client.get("/admin/actions").text
    finally:
        _drop_project(seeded_db, project.id)
        seeded_db.delete(seeded_db.get(Expense, exp.id))
        seeded_db.commit()
        set_password(seeded_db, "pavan.kumar", OWNER_PASSWORD)
