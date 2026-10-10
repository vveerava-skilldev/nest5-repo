import re
from datetime import date, datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import dates as dates_util
from app.config import PORTAL_NAME, settings
from app.domain.expense_categories import (
    ADD_CATEGORY_VALUE,
    category_options,
    resolve_expense_category,
)
from app.database import get_db
from app.models import (
    Asset,
    CancelRequest,
    AssetMaintenanceLog,
    AssetMaintenanceSchedule,
    Complaint,
    CorpusPayment,
    Expense,
    ExpenseCategory,
    Flat,
    FundType,
    LedgerPeriod,
    MaintenanceFeeRevision,
    MaintenancePayment,
    Project,
    ProjectPayment,
    ScheduleFrequency,
    Settlement,
)
from app.services import alerts as alerts_svc
from app.services import corpus_campaigns as campaigns_svc
from app.services import audit as audit_svc
from app.services import ledger as ledger_svc
from app.services import pdf_report
from app.services import actions as actions_svc
from app.services import balances as balances_svc
from app.services import expense_categories as expense_cats_svc
from app.services import projects as projects_svc
from app.services import periods as periods_svc
from app.services import maintenance_fee as maintenance_fee_svc
from app.services import schedule as schedule_svc
from app.services import app_settings as app_settings_svc
from app.services import insights as insights_svc
from app.services import owner_submissions as owner_svc
from app.services import reminders as reminders_svc
from app.services import users as users_svc
from app.services.access import can_view_flat, is_admin
from app.services.auth import require_auth

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["format_date"] = dates_util.format_date_ddmmyyyy
templates.env.filters["iso_date"] = dates_util.date_to_iso
templates.env.filters["format_datetime"] = dates_util.format_datetime_ddmmyyyy
templates.env.filters["format_month"] = maintenance_fee_svc.format_month
templates.env.globals["is_admin"] = is_admin
templates.env.globals["can_view_flat"] = can_view_flat
templates.env.globals["payment_modes"] = owner_svc.PAYMENT_MODES
templates.env.globals["portal_name"] = PORTAL_NAME


def _redirect_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def _period_bundle(db: Session, period_id: int | None) -> dict:
    period = periods_svc.resolve_period(db, period_id)
    ctx = periods_svc.period_context(db, period)
    return {**ctx, "period": period}


def _expense_categories(db: Session, *, owner: bool = False) -> list[str]:
    return expense_cats_svc.active_names(db, owner=owner)


def _save_expense_category(db: Session, category: str, category_other: str) -> str:
    name = resolve_expense_category(category, category_other)
    existing = expense_cats_svc.by_name(db, name)
    if existing is not None:
        return existing.name  # includes disabled ones, so an edited expense keeps its category
    cat = expense_cats_svc.add(db, name)
    audit_svc.log_audit(
        db, entity_type="expense_category", entity_id=cat.id, action="create", after=cat
    )
    return cat.name


def _expenses_url(period_id: int | None = None) -> str:
    if period_id:
        return f"/expenses?period_id={period_id}"
    return "/expenses"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request, "login.html", {"request": request, "error": None}
    )


@router.post("/login")
def login_submit(
    request: Request,
    password: str = Form(...),
    username: str = Form(""),
    db: Session = Depends(get_db),
):
    users_svc.ensure_bootstrap_users(db)
    db.commit()
    user = users_svc.authenticate(db, username, password)
    if not user:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": "Invalid username or password", "username": username},
            status_code=401,
        )
    user.last_login_at = datetime.utcnow()
    db.commit()
    request.session.clear()
    request.session["authenticated"] = True
    request.session["user_id"] = user.id
    request.session["username"] = user.username
    request.session["role"] = user.role
    if user.must_change_password:
        return RedirectResponse("/account/password", status_code=303)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def auth_dep(request: Request):
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    period_id: int | None = None,
):
    if not request.session.get("authenticated"):
        return _redirect_login()
    bundle = _period_bundle(db, period_id)
    period = bundle["period"]
    fund_balance = ledger_svc.compute_fund_balance(db, period)
    balances = balances_svc.overview(db)
    available = balances["available"]
    can_settle = balances["state"] in ("all", "some")
    settlement_note = {
        "nothing": "No owner expenses waiting to be settled.",
        "all": "All unsettled owner expenses can be settled now.",
        "some": f"Only some can be settled now: ₹{balances['settle_now']} of ₹{balances['owed']}.",
        "none": "None can be settled yet — the Account Balance is too low.",
    }[balances["state"]]
    pid = period.id if period else None
    own_flat_ids = None if is_admin(request) else [fid for fid, _ in getattr(request.state, "user_flats", [])]
    alerts = alerts_svc.build_dashboard_alerts(db, period, flat_ids=own_flat_ids)
    action_board = ledger_svc.build_dashboard_status(db, period)
    if own_flat_ids is not None:
        # Owners see their own flats' rows; the footer keeps building totals.
        action_board["rows"] = [r for r in action_board["rows"] if r["flat"].id in own_flat_ids]
    if period:
        db.commit()
    reminders = None
    if is_admin(request):
        pending_cancels = len(owner_svc.pending_requests_map(db, "maintenance_payment")) + len(
            owner_svc.pending_requests_map(db, "expense")
        ) + len(owner_svc.pending_requests_map(db, "corpus_payment")) + len(
            owner_svc.pending_requests_map(db, "project_contribution"))
        if pending_cancels:
            alerts.insert(
                0,
                {
                    "severity": "warning",
                    "icon": "↩",
                    "title": "Owner cancel requests waiting",
                    "detail": "Owners asked to cancel entries they submitted — see Admin → Cancel requests.",
                    "count": pending_cancels,
                },
            )
    else:
        reminders = reminders_svc.reminders_for_flats(db, own_flat_ids)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "balances": balances,
            "hub": actions_svc.build(db) if is_admin(request) else None,
            "my_advances": balances_svc.owner_advances(db, own_flat_ids) if own_flat_ids is not None else None,
            "today": date.today(),
            "period": period,
            "nav": bundle["nav"],
            "summary": bundle["summary"],
            "alerts": alerts,
            "action_board": action_board,
            "reminders": reminders,
            "fund_balance": fund_balance,
            "available": available,
            "can_settle": can_settle,
            "settlement_note": settlement_note,
            "collections": ledger_svc.maintenance_collected(db, pid),
            "expenses": ledger_svc.settlements_from_fund(db, pid),
            "maintenance_fee": (
                maintenance_fee_svc.fee_for_period(db, period)
                if period
                else settings.maintenance_fee
            ),
        },
    )


def _maintenance_view(request: Request, db: Session, period_id: int | None):
    bundle = _period_bundle(db, period_id)
    period = bundle["period"]
    if not period:
        period = ledger_svc.get_or_create_period(db, 2026, 9)
        db.commit()
        bundle = _period_bundle(db, period.id)
        period = bundle["period"]
    ledger_svc.ensure_maintenance_rows(db, period)
    db.commit()
    payments = db.scalars(
        select(MaintenancePayment)
        .where(MaintenancePayment.period_id == period.id)
        .join(Flat, MaintenancePayment.flat_id == Flat.id)
        .options(selectinload(MaintenancePayment.flat))
        .order_by(Flat.number)
    ).all()
    if not is_admin(request):
        visible = set(_my_flat_ids(request, db))
        payments = [mp for mp in payments if mp.flat_id in visible]
    owner_paid: dict[str, str] = {}
    my_flat_ids = [fid for fid, _ in getattr(request.state, "user_flats", None) or []]
    if my_flat_ids and not is_admin(request):
        for mp in db.scalars(
            select(MaintenancePayment).where(
                MaintenancePayment.flat_id.in_(my_flat_ids), MaintenancePayment.is_paid.is_(True)
            )
        ).all():
            owner_paid[f"{mp.flat_id}:{mp.period_id}"] = dates_util.format_date_ddmmyyyy(mp.paid_on) if mp.paid_on else ""
    return templates.TemplateResponse(
        request,
        "maintenance.html",
        {
            "request": request,
            "period": period,
            "nav": bundle["nav"],
            "summary": bundle["summary"],
            "payments": payments,
            "payments_tab": "maintenance",
            "maintenance_fee": maintenance_fee_svc.fee_for_period(db, period),
            "cancel_pending": owner_svc.pending_requests_map(db, "maintenance_payment"),
            "today": date.today(),
            "owner_paid": owner_paid,
            "owner_fees": {
                p.id: maintenance_fee_svc.fee_for_period(db, p) for p in bundle["nav"]["periods"]
            },
        },
    )


@router.get("/payments/maintenance", response_class=HTMLResponse)
def payments_maintenance_page(
    request: Request, db: Session = Depends(get_db), period_id: int | None = None
):
    if not request.session.get("authenticated"):
        return _redirect_login()
    return _maintenance_view(request, db, period_id)


@router.get("/maintenance", response_class=HTMLResponse)
def maintenance_page(
    request: Request, db: Session = Depends(get_db), period_id: int | None = None
):
    if not request.session.get("authenticated"):
        return _redirect_login()
    q = f"?period_id={period_id}" if period_id else ""
    return RedirectResponse(f"/payments/maintenance{q}", status_code=303)


@router.post("/maintenance/{payment_id}/pay")
def maintenance_pay(
    request: Request,
    payment_id: int,
    paid_on: str = Form(...),
    payment_note: str = Form(""),
    db: Session = Depends(get_db),
):
    require_auth(request)
    mp = db.get(MaintenancePayment, payment_id)
    if not mp:
        raise HTTPException(404)
    before = mp
    mp.is_paid = True
    mp.paid_on = date.fromisoformat(paid_on)
    mp.payment_note = payment_note or None
    mp.reverted_at = None
    mp.submitted_by_user_id = None
    audit_svc.log_audit(db, entity_type="maintenance_payment", entity_id=mp.id, action="pay", before=before, after=mp)
    db.commit()
    return RedirectResponse(f"/payments/maintenance?period_id={mp.period_id}", status_code=303)


@router.post("/maintenance/{payment_id}/revert")
def maintenance_revert(request: Request, payment_id: int, db: Session = Depends(get_db)):
    require_auth(request)
    mp = db.get(MaintenancePayment, payment_id)
    if not mp:
        raise HTTPException(404)
    before = audit_svc.snapshot(mp)
    mp.is_paid = False
    mp.paid_on = None
    mp.reverted_at = datetime.utcnow()
    if mp.submitted_by_user_id and mp.period is not None:
        mp.amount = maintenance_fee_svc.fee_for_period(db, mp.period)
    audit_svc.log_audit(db, entity_type="maintenance_payment", entity_id=mp.id, action="revert", before=before, after=mp)
    db.commit()
    return RedirectResponse(f"/payments/maintenance?period_id={mp.period_id}", status_code=303)


@router.get("/expenses", response_class=HTMLResponse)
def expenses_page(
    request: Request, db: Session = Depends(get_db), period_id: int | None = None
):
    if not request.session.get("authenticated"):
        return _redirect_login()
    bundle = _period_bundle(db, period_id)
    period = bundle["period"]
    q = select(Expense).where(Expense.deleted_at.is_(None))
    if period:
        q = q.where(Expense.period_id == period.id)
    expenses = db.scalars(
        q.options(selectinload(Expense.paid_by_flat)).order_by(Expense.expense_date.desc())
    ).all()
    flats = db.scalars(select(Flat).order_by(Flat.number)).all()
    all_expenses = db.scalars(select(Expense).where(Expense.deleted_at.is_(None))).all()
    totals = {
        "month_total": sum(e.amount for e in expenses),
        "month_settled": ledger_svc.settlements_from_fund(db, period.id) if period else ledger_svc.settlements_from_fund(db),
        "month_pending": sum(e.amount for e in expenses if e.paid_by_flat_id and not e.is_settled),
        "all_total": sum(e.amount for e in all_expenses),
        "all_pending": sum(e.amount for e in all_expenses if e.paid_by_flat_id and not e.is_settled),
    }
    return templates.TemplateResponse(
        request,
        "expenses.html",
        {
            "request": request,
            "totals": totals,
            "expenses": expenses,
            "flats": flats,
            "period": period,
            "nav": bundle["nav"],
            "summary": bundle["summary"],
            "available": ledger_svc.available_for_settlement(db),
            "expense_categories": _expense_categories(db, owner=not is_admin(request)),
            "add_category_value": ADD_CATEGORY_VALUE,
            "cancel_pending": owner_svc.pending_requests_map(db, "expense"),
            "today": date.today(),
        },
    )


@router.post("/expenses")
def expense_create(
    request: Request,
    description: str = Form(...),
    category: str = Form("Common"),
    category_other: str = Form(""),
    amount: int = Form(...),
    expense_date: str = Form(...),
    paid_by_flat_id: int | None = Form(None),
    period_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    require_auth(request)
    exp = Expense(
        description=description,
        category=_save_expense_category(db, category, category_other),
        amount=amount,
        expense_date=date.fromisoformat(expense_date),
        paid_by_flat_id=paid_by_flat_id or None,
        period_id=period_id or None,
    )
    db.add(exp)
    db.flush()
    audit_svc.log_audit(db, entity_type="expense", entity_id=exp.id, action="create", after=exp)
    db.commit()
    return RedirectResponse(_expenses_url(period_id), status_code=303)


@router.post("/expenses/{expense_id}/edit")
def expense_edit(
    request: Request,
    expense_id: int,
    description: str = Form(...),
    category: str = Form(...),
    category_other: str = Form(""),
    amount: int = Form(...),
    expense_date: str = Form(...),
    paid_by_flat_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    require_auth(request)
    exp = db.get(Expense, expense_id)
    if not exp or exp.deleted_at:
        raise HTTPException(404)
    before = exp
    exp.description = description
    exp.category = _save_expense_category(db, category, category_other)
    exp.amount = amount
    exp.expense_date = date.fromisoformat(expense_date)
    exp.paid_by_flat_id = paid_by_flat_id or None
    audit_svc.log_audit(db, entity_type="expense", entity_id=exp.id, action="update", before=before, after=exp)
    db.commit()
    return RedirectResponse(_expenses_url(exp.period_id), status_code=303)


@router.post("/expenses/{expense_id}/delete")
def expense_delete(request: Request, expense_id: int, db: Session = Depends(get_db)):
    require_auth(request)
    exp = db.get(Expense, expense_id)
    if not exp:
        raise HTTPException(404)
    before = exp
    exp.deleted_at = datetime.utcnow()
    audit_svc.log_audit(db, entity_type="expense", entity_id=exp.id, action="delete", before=before, after=exp)
    db.commit()
    return RedirectResponse(_expenses_url(exp.period_id), status_code=303)


@router.get("/admin/maintenance-fee", response_class=HTMLResponse)
def admin_maintenance_fee_page(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("authenticated"):
        return _redirect_login()
    history = db.scalars(
        select(MaintenanceFeeRevision).order_by(
            MaintenanceFeeRevision.effective_date.desc(),
            MaintenanceFeeRevision.id.desc(),
        )
    ).all()
    current = maintenance_fee_svc.current_maintenance_fee(db)
    previous = {row.id: maintenance_fee_svc.previous_fee(db, row) for row in history}
    return templates.TemplateResponse(
        request,
        "admin_maintenance_fee.html",
        {
            "request": request,
            "history": history,
            "previous": previous,
            "current_fee": current,
            "default_fee": settings.maintenance_fee,
            "this_month": date.today().strftime("%Y-%m"),
        },
    )


@router.get("/admin/maintenance-fee/preview")
def admin_maintenance_fee_preview(request: Request, month: str, db: Session = Depends(get_db)):
    require_auth(request)
    try:
        eff = maintenance_fee_svc.parse_effective_month(month)
    except ValueError:
        raise HTTPException(400, "Use month as mm-yyyy")
    return {
        "effective_month": maintenance_fee_svc.format_month(eff),
        "affected": maintenance_fee_svc.preview_revision(db, eff),
    }


@router.post("/admin/maintenance-fee")
def admin_maintenance_fee_submit(
    request: Request,
    effective_date: str = Form(...),
    amount: int = Form(...),
    note: str = Form(...),
    db: Session = Depends(get_db),
):
    require_auth(request)
    note = note.strip()
    if not note:
        raise HTTPException(400, "Note is required")
    try:
        eff = maintenance_fee_svc.parse_effective_month(effective_date)
    except ValueError:
        raise HTTPException(400, "Use month as mm-yyyy")
    old_amount = maintenance_fee_svc.current_maintenance_fee(db, eff)
    rev = MaintenanceFeeRevision(
        effective_date=eff,
        amount=amount,
        note=note,
        created_by=audit_svc.current_user_label.get() or "admin",
    )
    db.add(rev)
    db.flush()
    maintenance_fee_svc.reprice_unpaid_from(db, eff)
    audit_svc.log_audit(
        db,
        entity_type="maintenance_fee_revision",
        entity_id=rev.id,
        action="create",
        before={"amount": old_amount, "effective_date": eff},
        after=rev,
    )
    db.commit()
    return RedirectResponse("/admin/maintenance-fee", status_code=303)


@router.get("/settlements", response_class=HTMLResponse)
def settlements_page(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("authenticated"):
        return _redirect_login()
    available = ledger_svc.available_for_settlement(db)
    unsettled = ledger_svc.unsettled_expenses(db)
    by_flat = ledger_svc.settlement_status_by_flat(db)
    settled_rows = db.scalars(
        select(Settlement)
        .where(Settlement.reverted_at.is_(None))
        .options(selectinload(Settlement.expense))
        .order_by(Settlement.id.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "settlements.html",
        {
            "request": request,
            "available": available,
            "unsettled": unsettled,
            "by_flat": by_flat,
            "settled_rows": settled_rows,
            "can_settle": available > 0,
        },
    )


def _settlements_back(**params) -> RedirectResponse:
    params = {k: v for k, v in params.items() if v}
    return RedirectResponse(f"/settlements?{urlencode(params)}" if params else "/settlements", status_code=303)


@router.get("/settlements/{expense_id}/settle")
@router.get("/settlements/{settlement_id}/revert")
def settlement_action_get(request: Request):
    require_auth(request)
    return _settlements_back(error="Use the buttons on this page to settle or revert.")


@router.post("/settlements/{expense_id}/settle")
def settle_expense(
    request: Request,
    expense_id: int,
    settled_on: str = Form(...),
    note: str = Form(""),
    back: str = Form(""),
    db: Session = Depends(get_db),
):
    require_auth(request)
    def fail(message: str) -> RedirectResponse:
        if back == "/":
            return RedirectResponse("/?" + urlencode({"error": message}), status_code=303)
        return _settlements_back(error=message)

    exp = db.get(Expense, expense_id)
    if not exp or exp.deleted_at:
        return fail("That expense no longer exists.")
    if exp.is_settled:
        return fail(f"'{exp.description}' is already settled.")
    available = ledger_svc.available_for_settlement(db)
    if available < exp.amount:
        return fail(
            f"Insufficient Account Balance (₹{available}) to settle ₹{exp.amount} — settlement blocked."
        )
    settled_date = date.fromisoformat(settled_on)
    # settlements.expense_id is unique: a reverted settlement row is reused instead of inserting a second one.
    settlement = exp.settlement
    if settlement is not None:
        before = audit_svc.snapshot(settlement)
        settlement.fund_type = FundType.MAINTENANCE
        settlement.amount = exp.amount
        settlement.settled_on = settled_date
        settlement.note = note or None
        settlement.reverted_at = None
        action = "update"
    else:
        before = None
        settlement = Settlement(
            expense_id=exp.id,
            fund_type=FundType.MAINTENANCE,
            amount=exp.amount,
            settled_on=settled_date,
            note=note or None,
        )
        db.add(settlement)
        action = "create"
    exp.is_settled = True
    db.flush()
    audit_svc.log_audit(
        db, entity_type="settlement", entity_id=settlement.id, action=action, before=before, after=settlement
    )
    audit_svc.log_audit(db, entity_type="expense", entity_id=exp.id, action="settle", after=exp)
    db.commit()
    if back == "/":
        return RedirectResponse("/?" + urlencode({"msg": f"Settled {exp.description} (₹{exp.amount})."}), status_code=303)
    return RedirectResponse("/settlements", status_code=303)


@router.post("/settlements/{settlement_id}/revert")
def revert_settlement(request: Request, settlement_id: int, db: Session = Depends(get_db)):
    require_auth(request)
    st = db.get(Settlement, settlement_id)
    if not st or st.reverted_at:
        return _settlements_back(error="That settlement was already reverted.")
    exp = st.expense
    before_st, before_exp = st, exp
    st.reverted_at = datetime.utcnow()
    exp.is_settled = False
    audit_svc.log_audit(
        db, entity_type="settlement", entity_id=st.id, action="revert", before=before_st, after=st
    )
    audit_svc.log_audit(
        db, entity_type="expense", entity_id=exp.id, action="unsettle", before=before_exp, after=exp
    )
    db.commit()
    return RedirectResponse("/settlements", status_code=303)


@router.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("authenticated"):
        return _redirect_login()
    projects = db.scalars(
        select(Project).where(Project.deleted_at.is_(None)).order_by(Project.id.desc())
    ).all()
    project_rows = []
    for proj in projects:
        pays = db.scalars(select(ProjectPayment).where(ProjectPayment.project_id == proj.id)).all()
        project_rows.append({"project": proj, "totals": _project_totals(pays)})
    collected = sum(r["totals"]["collected"] for r in project_rows)
    share_total = sum(r["totals"]["share_total"] for r in project_rows)
    return templates.TemplateResponse(
        request,
        "projects.html",
        {
            "request": request,
            "projects": projects,
            "project_rows": project_rows,
            "building": {
                "cost": sum(p.total_cost for p in projects),
                "collected": collected,
                "pending": share_total - collected,
                "progress_pct": int(collected * 100 / share_total) if share_total else 0,
            },
        },
    )


@router.post("/projects")
def project_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    total_cost: int = Form(...),
    start_date: str = Form(""),
    due_date: str = Form(""),
    db: Session = Depends(get_db),
):
    require_auth(request)
    share = total_cost // len(settings.flat_numbers)
    proj = Project(
        name=name,
        description=description or None,
        total_cost=total_cost,
        share_per_flat=share,
        start_date=date.fromisoformat(start_date) if start_date else None,
        due_date=_opt_date(due_date),
    )
    db.add(proj)
    db.flush()
    flats = db.scalars(select(Flat).order_by(Flat.number)).all()
    for flat in flats:
        db.add(
            ProjectPayment(
                project_id=proj.id,
                flat_id=flat.id,
                amount=share,
                paid_amount=0,
            )
        )
    audit_svc.log_audit(db, entity_type="project", entity_id=proj.id, action="create", after=proj)
    db.commit()
    return RedirectResponse("/projects", status_code=303)


@router.post("/projects/payments/{payment_id}/pay")
def project_pay(
    request: Request,
    payment_id: int,
    paid_on: str = Form(...),
    payment_note: str = Form(""),
    back: str = Form(""),
    db: Session = Depends(get_db),
):
    require_auth(request)
    pp = db.get(ProjectPayment, payment_id)
    if not pp:
        raise HTTPException(404)
    before = audit_svc.snapshot(pp)
    projects_svc.mark_paid(db, pp, date.fromisoformat(paid_on), payment_note.strip() or None)
    audit_svc.log_audit(db, entity_type="project_payment", entity_id=pp.id, action="pay", before=before, after=pp)
    db.commit()
    return RedirectResponse(_project_back(back, pp.project_id), status_code=303)


@router.post("/projects/payments/{payment_id}/revert")
def project_revert(
    request: Request, payment_id: int, back: str = Form(""), db: Session = Depends(get_db)
):
    require_auth(request)
    pp = db.get(ProjectPayment, payment_id)
    if not pp:
        raise HTTPException(404)
    before = audit_svc.snapshot(pp)
    for c in pp.contributions:
        pending = db.scalar(select(CancelRequest).where(
            CancelRequest.record_type == "project_contribution", CancelRequest.record_id == c.id,
            CancelRequest.status == "pending"))
        if pending is not None:
            pending.status = "approved"
            pending.decided_at = datetime.utcnow()
            pending.decided_by = getattr(request.state, "username", "admin")
            pending.decision_note = "Payment reverted by admin"
    projects_svc.revert(db, pp)
    audit_svc.log_audit(
        db, entity_type="project_payment", entity_id=pp.id, action="revert", before=before, after=pp
    )
    db.commit()
    return RedirectResponse(_project_back(back, pp.project_id), status_code=303)


def _live_project(db: Session, project_id: int) -> Project:
    proj = db.get(Project, project_id)
    if not proj or proj.deleted_at:
        raise HTTPException(404)
    return proj


def _project_msg(project_id: int, **params) -> RedirectResponse:
    return RedirectResponse(f"/projects/{project_id}?" + urlencode(params), status_code=303)


@router.post("/projects/{project_id}/edit")
def project_update(
    request: Request,
    project_id: int,
    name: str = Form(...),
    description: str = Form(""),
    total_cost: str = Form(...),
    start_date: str = Form(""),
    due_date: str = Form(""),
    db: Session = Depends(get_db),
):
    require_auth(request)
    proj = _live_project(db, project_id)
    cost = _opt_amount(total_cost)
    if not name.strip() or not cost:
        return _project_msg(proj.id, error="Name and a positive total cost are required.")
    share = cost // len(settings.flat_numbers)
    shares = db.scalars(select(ProjectPayment).where(ProjectPayment.project_id == proj.id)).all()
    over = sorted(pp.flat.number for pp in shares if pp.paid > share)
    if over:
        return _project_msg(proj.id, error=(
            f"Not saved: the new share ₹{share}/flat is less than what Flat {', '.join(over)} already paid. "
            "Revert or cancel those payments first."))
    before = audit_svc.snapshot(proj)
    proj.name = name.strip()
    proj.description = description.strip() or None
    proj.total_cost = cost
    proj.share_per_flat = share
    proj.start_date = _opt_date(start_date)
    proj.due_date = _opt_date(due_date)
    for pp in shares:
        pp.amount = share
        projects_svc.sync(db, pp)
    audit_svc.log_audit(db, entity_type="project", entity_id=proj.id, action="update", before=before, after=proj)
    db.commit()
    return _project_msg(proj.id, msg="Project saved.")


@router.post("/projects/{project_id}/delete")
def project_delete(request: Request, project_id: int, confirm: str = Form(""), db: Session = Depends(get_db)):
    """Projects with payments are blocked: revert every payment on the project first."""
    require_auth(request)
    proj = _live_project(db, project_id)
    count = projects_svc.has_payments(db, proj)
    if count:
        return _project_msg(proj.id, error=(
            f"Not deleted: {proj.name} has {count} payment(s) recorded. Revert each flat's payments below first."))
    if confirm.strip().upper() != "DELETE":
        return _project_msg(proj.id, error="Type DELETE to confirm.")
    before = audit_svc.snapshot(proj)
    proj.deleted_at = datetime.utcnow()
    audit_svc.log_audit(db, entity_type="project", entity_id=proj.id, action="delete", before=before, after=proj)
    db.commit()
    return RedirectResponse("/projects?" + urlencode({"msg": f"Deleted project {proj.name}."}), status_code=303)


def _project_back(back: str, project_id: int) -> str:
    return "/payments/projects" if back == "/payments/projects" else f"/projects/{project_id}"


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_detail(request: Request, project_id: int, db: Session = Depends(get_db)):
    if not request.session.get("authenticated"):
        return _redirect_login()
    proj = db.get(Project, project_id)
    if not proj or proj.deleted_at:
        raise HTTPException(404)
    payments = db.scalars(
        select(ProjectPayment)
        .where(ProjectPayment.project_id == project_id)
        .join(Flat)
        .order_by(Flat.number)
    ).all()
    visible = set(_my_flat_ids(request, db))
    contributions = [c for c in projects_svc.contributions_for_flats(db, sorted(visible)) if c.project_id == proj.id]
    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            "request": request,
            "project": proj,
            "payments": [pp for pp in payments if pp.flat_id in visible],
            "totals": _project_totals(payments),
            "contributions": contributions,
            "payment_count": projects_svc.has_payments(db, proj),
            "cancel_pending": owner_svc.pending_requests_map(db, "project_contribution"),
            "today": date.today(),
        },
    )


def _project_totals(payments: list[ProjectPayment]) -> dict:
    return projects_svc.totals(payments)


@router.get("/payments/projects", response_class=HTMLResponse)
def payments_projects_page(request: Request, db: Session = Depends(get_db)):
    flat_ids = _my_flat_ids(request, db)
    rows = projects_svc.shares_for_flats(db, flat_ids)
    return templates.TemplateResponse(
        request,
        "payments_projects.html",
        {
            "request": request,
            "rows": rows,
            "open_rows": [pp for pp in rows if pp.remaining > 0],
            "contributions": projects_svc.contributions_for_flats(db, flat_ids),
            "cancel_pending": owner_svc.pending_requests_map(db, "project_contribution"),
            "payment_modes": owner_svc.PAYMENT_MODES,
            "totals": {
                "share": sum(pp.amount for pp in rows),
                "paid": sum(min(pp.paid, pp.amount) for pp in rows),
                "pending": sum(pp.remaining for pp in rows),
            },
            "today": date.today(),
            "payments_tab": "projects",
            "period": None,
        },
    )


@router.get("/payments/expenses", response_class=HTMLResponse)
def payments_expenses_page(request: Request, db: Session = Depends(get_db)):
    """Owner: the expense form + their own submitted expenses. Admin: every owner-submitted expense."""
    q = (
        select(Expense)
        .where(Expense.deleted_at.is_(None), Expense.submitted_by_user_id.isnot(None))
        .options(selectinload(Expense.paid_by_flat), selectinload(Expense.submitted_by))
        .order_by(Expense.expense_date.desc(), Expense.id.desc())
    )
    if not is_admin(request):
        q = q.where(Expense.paid_by_flat_id.in_(_my_flat_ids(request, db)))
    items = db.scalars(q).all()
    return templates.TemplateResponse(
        request,
        "payments_expenses.html",
        {
            "request": request,
            "items": items,
            "expense_categories": _expense_categories(db, owner=not is_admin(request)),
            "cancel_pending": owner_svc.pending_requests_map(db, "expense"),
            "today": date.today(),
            "payments_tab": "expenses",
            "period": None,
        },
    )


@router.get("/payments", response_class=HTMLResponse)
def payments_hub(request: Request, period_id: int | None = None):
    q = f"?period_id={period_id}" if period_id else ""
    return RedirectResponse(f"/payments/maintenance{q}", status_code=303)


def _my_flat_ids(request: Request, db: Session) -> list[int]:
    """Admin: every flat. Owner: only their linked flats (other flats' rows stay admin-only)."""
    if is_admin(request):
        return list(db.scalars(select(Flat.id).order_by(Flat.number)).all())
    return [fid for fid, _ in getattr(request.state, "user_flats", None) or []]


def _corpus_view(request: Request, db: Session, period_id: int | None):
    flat_ids = _my_flat_ids(request, db)
    flats = db.scalars(select(Flat).where(Flat.id.in_(flat_ids)).order_by(Flat.number)).all()
    status_rows = campaigns_svc.open_campaigns_for_flats(db, flat_ids)
    return templates.TemplateResponse(
        request,
        "corpus.html",
        {
            "request": request,
            "flats": flats,
            "status_rows": status_rows,
            "status_totals": {
                "share": sum(r["share"] for r in status_rows),
                "paid": sum(r["paid"] for r in status_rows),
                "remaining": sum(r["remaining"] for r in status_rows),
            },
            "payments": campaigns_svc.payments_for_flats(db, flat_ids),
            "general": campaigns_svc.general_campaign(db),
            "building": campaigns_svc.building_totals(db),
            "period": None,
            "payments_tab": "corpus",
            "back": "/payments/corpus",
            "cancel_pending": owner_svc.pending_requests_map(db, "corpus_payment"),
            "today": date.today(),
        },
    )


@router.get("/payments/corpus", response_class=HTMLResponse)
def payments_corpus_page(
    request: Request, db: Session = Depends(get_db), period_id: int | None = None
):
    if not request.session.get("authenticated"):
        return _redirect_login()
    return _corpus_view(request, db, period_id)


def _corpus_back(path: str = "/payments/corpus", **params) -> RedirectResponse:
    params = {k: v for k, v in params.items() if v}
    return RedirectResponse(f"{path}?{urlencode(params)}" if params else path, status_code=303)


@router.post("/corpus")
def corpus_create(
    request: Request,
    flat_id: int = Form(...),
    amount: int = Form(...),
    description: str = Form(""),
    period_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    """Add a one-off corpus amount for a flat: raises its General corpus share."""
    require_auth(request)
    flat = db.get(Flat, flat_id)
    if not flat or amount <= 0:
        return _corpus_back("/corpus", error="Choose a flat and a positive amount.")
    general = campaigns_svc.general_campaign(db)
    existing = campaigns_svc.flat_share(db, general.id, flat_id) if general else None
    before = existing.share_amount if existing else 0
    general = campaigns_svc.add_to_general_share(db, flat_id, amount, created_by=getattr(request.state, "username", "admin"))
    audit_svc.log_audit(
        db, entity_type="corpus_campaign_share", entity_id=general.id, action="update",
        before={"collection": general.name, "flat": flat.number, "share_amount": before},
        after={"collection": general.name, "flat": flat.number, "share_amount": before + amount,
               "added": amount, "note": description or None},
    )
    db.commit()
    return _corpus_back("/corpus", msg=f"Added ₹{amount} to Flat {flat.number}'s General corpus share.")


@router.post("/corpus/{item_id}/pay")
def corpus_pay(
    request: Request,
    item_id: int,
    paid_on: str = Form(...),
    payment_note: str = Form(""),
    db: Session = Depends(get_db),
):
    """Older 'Mark paid' button on a line item: pays the full amount against General corpus."""
    require_auth(request)
    cp = db.get(CorpusPayment, item_id)
    if not cp:
        raise HTTPException(404)
    if cp.is_paid:
        return _corpus_back(error="That corpus entry is already paid.")
    flat_id, amount = cp.flat_id, cp.amount
    campaigns_svc.absorb_line_items(db)
    general = campaigns_svc.general_campaign(db)
    paid_date = date.fromisoformat(paid_on)
    period = campaigns_svc.period_for_date(db, paid_date)
    payment = CorpusPayment(
        flat_id=flat_id, period_id=period.id if period else None, amount=amount, is_paid=True,
        paid_on=paid_date, payment_note=payment_note or None, description=general.name, campaign_id=general.id,
    )
    db.add(payment)
    db.flush()
    audit_svc.log_audit(db, entity_type="corpus_payment", entity_id=payment.id, action="create", after=payment)
    db.commit()
    return _corpus_back(msg=f"Recorded ₹{amount} General corpus payment.")


@router.post("/corpus/{item_id}/revert")
def corpus_revert(request: Request, item_id: int, db: Session = Depends(get_db)):
    """Undo a corpus payment (it is removed; the flat's share stays, so remaining goes back up)."""
    require_auth(request)
    cp = db.get(CorpusPayment, item_id)
    if not cp:
        raise HTTPException(404)
    if cp.campaign_id is None:
        before = audit_svc.snapshot(cp)
        cp.is_paid = False
        cp.paid_on = None
        cp.reverted_at = datetime.utcnow()
        audit_svc.log_audit(db, entity_type="corpus_payment", entity_id=cp.id, action="revert", before=before, after=cp)
        campaigns_svc.absorb_line_items(db)
        db.commit()
        return _corpus_back(msg="Payment reverted.")
    before = audit_svc.snapshot(cp)
    pending = owner_svc.pending_requests_map(db, "corpus_payment").get(cp.id)
    if pending:
        pending.status = "approved"
        pending.decided_at = datetime.utcnow()
        pending.decided_by = getattr(request.state, "username", "admin")
        pending.decision_note = "Payment reverted by admin"
    db.delete(cp)
    audit_svc.log_audit(db, entity_type="corpus_payment", entity_id=item_id, action="delete", before=before)
    db.commit()
    return _corpus_back(msg="Payment reverted.")


def _opt_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, detail="Invalid date.")


def _opt_amount(value: str) -> int | None:
    value = (value or "").replace(",", "").replace("₹", "").strip()
    if not value:
        return None
    try:
        amount = int(float(value))
    except ValueError:
        raise HTTPException(400, detail="Cost must be a number.")
    if amount < 0:
        raise HTTPException(400, detail="Cost cannot be negative.")
    return amount


def _frequency(value: str) -> ScheduleFrequency:
    try:
        return ScheduleFrequency((value or "monthly").lower())
    except ValueError:
        raise HTTPException(400, detail="Unknown frequency.")


def _schedule_back(back: str, msg: str | None = None) -> RedirectResponse:
    target = back if back == "/schedule" or re.fullmatch(r"/assets/\d+", back or "") else "/schedule"
    if msg:
        target += "?" + urlencode({"msg": msg})
    return RedirectResponse(target, status_code=303)


def _live_asset(db: Session, asset_id: int) -> Asset:
    asset = db.get(Asset, asset_id)
    if not asset or asset.deleted_at:
        raise HTTPException(404)
    return asset


def _live_task(db: Session, item_id: int) -> AssetMaintenanceSchedule:
    item = db.get(AssetMaintenanceSchedule, item_id)
    if not item or item.deleted_at:
        raise HTTPException(404)
    if item.asset_id is None:
        item.asset_id = schedule_svc.get_or_create_asset(db, item.asset).id
    return item


@router.get("/schedule", response_class=HTMLResponse)
def schedule_page(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("authenticated"):
        return _redirect_login()
    if schedule_svc.ensure_assets(db):
        db.commit()
    return templates.TemplateResponse(
        request,
        "schedule.html",
        {"request": request, "board": schedule_svc.board(db), "frequencies": list(ScheduleFrequency),
         "asset_types": schedule_svc.ASSET_TYPES, "back": "/schedule"},
    )


@router.get("/assets")
def assets_redirect():
    return RedirectResponse("/schedule", status_code=303)


@router.get("/assets/{asset_id}", response_class=HTMLResponse)
def asset_detail(request: Request, asset_id: int, db: Session = Depends(get_db)):
    asset = _live_asset(db, asset_id)
    today = date.today()
    tasks = [(t, schedule_svc.schedule_status(t, today)) for t in schedule_svc.tasks_for(db, asset)]
    logs = schedule_svc.history(db, asset)
    return templates.TemplateResponse(
        request,
        "asset_detail.html",
        {"request": request, "asset": asset, "tasks": tasks, "logs": logs, "today": today,
         "total_cost": sum(log.cost or 0 for log in logs), "frequencies": list(ScheduleFrequency),
         "asset_types": schedule_svc.ASSET_TYPES, "back": f"/assets/{asset.id}"},
    )


def _asset_fields(asset: Asset, name: str, asset_type: str, location: str, vendor_contact: str,
                  install_date: str, warranty_expiry: str, cost: str, notes: str) -> None:
    if not name.strip():
        raise HTTPException(400, detail="Asset name is required.")
    asset.name = name.strip()[:100]
    asset.asset_type = asset_type.strip() or schedule_svc.guess_type(name)
    asset.location = location.strip() or None
    asset.vendor_contact = vendor_contact.strip() or None
    asset.install_date = _opt_date(install_date)
    asset.warranty_expiry = _opt_date(warranty_expiry)
    asset.cost = _opt_amount(cost)
    asset.notes = notes.strip() or None


@router.post("/assets")
def asset_create(
    request: Request,
    name: str = Form(...),
    asset_type: str = Form(""),
    location: str = Form(""),
    vendor_contact: str = Form(""),
    install_date: str = Form(""),
    warranty_expiry: str = Form(""),
    cost: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    require_auth(request)
    if schedule_svc.asset_by_name(db, name):
        return _schedule_back("/schedule", f"An asset named {name.strip()} already exists.")
    asset = Asset()
    _asset_fields(asset, name, asset_type, location, vendor_contact, install_date, warranty_expiry, cost, notes)
    db.add(asset)
    db.flush()
    audit_svc.log_audit(db, entity_type="asset", entity_id=asset.id, action="create", after=asset)
    db.commit()
    return _schedule_back(f"/assets/{asset.id}", f"Asset {asset.name} added. Add its recurring tasks below.")


@router.post("/assets/{asset_id}/edit")
def asset_update(
    request: Request,
    asset_id: int,
    name: str = Form(...),
    asset_type: str = Form(""),
    location: str = Form(""),
    vendor_contact: str = Form(""),
    install_date: str = Form(""),
    warranty_expiry: str = Form(""),
    cost: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    require_auth(request)
    asset = _live_asset(db, asset_id)
    clash = schedule_svc.asset_by_name(db, name)
    if clash is not None and clash.id != asset.id:
        return _schedule_back(f"/assets/{asset.id}", f"An asset named {name.strip()} already exists.")
    before = audit_svc.snapshot(asset)
    _asset_fields(asset, name, asset_type, location, vendor_contact, install_date, warranty_expiry, cost, notes)
    for task in schedule_svc.tasks_for(db, asset):
        task.asset = asset.name
    audit_svc.log_audit(db, entity_type="asset", entity_id=asset.id, action="update", before=before, after=asset)
    db.commit()
    return _schedule_back(f"/assets/{asset.id}", "Asset updated.")


@router.post("/assets/{asset_id}/delete")
def asset_delete(request: Request, asset_id: int, db: Session = Depends(get_db)):
    require_auth(request)
    asset = _live_asset(db, asset_id)
    before = audit_svc.snapshot(asset)
    schedule_svc.soft_delete_asset(db, asset)
    audit_svc.log_audit(db, entity_type="asset", entity_id=asset.id, action="delete", before=before, after=asset)
    db.commit()
    return _schedule_back("/schedule", f"Asset {asset.name} and its tasks removed (history kept in the audit log).")


@router.post("/assets/{asset_id}/log")
def asset_log_create(
    request: Request,
    asset_id: int,
    done_on: str = Form(...),
    schedule_id: str = Form(""),
    done_by: str = Form(""),
    notes: str = Form(""),
    cost: str = Form(""),
    back: str = Form(""),
    db: Session = Depends(get_db),
):
    require_auth(request)
    asset = _live_asset(db, asset_id)
    task = None
    if schedule_id.strip():
        task = _live_task(db, int(schedule_id)) if schedule_id.strip().isdigit() else None
        if task is None or task.asset_id != asset.id:
            raise HTTPException(400, detail="That task does not belong to this asset.")
    if not task and not notes.strip():
        return _schedule_back(back or f"/assets/{asset.id}", "Add a note when the entry is not for a task.")
    log = schedule_svc.record_log(db, asset, task, _opt_date(done_on) or date.today(), done_by=done_by.strip(),
                                  notes=notes.strip(), cost=_opt_amount(cost),
                                  created_by=getattr(request.state, "username", None))
    audit_svc.log_audit(db, entity_type="asset_log", entity_id=log.id, action="create", after=log)
    db.commit()
    msg = f"Logged for {asset.name}." + (f" Next due {dates_util.format_date_ddmmyyyy(log.next_due_on)}." if log.next_due_on else "")
    return _schedule_back(back or f"/assets/{asset.id}", msg)


@router.post("/assets/logs/{log_id}/delete")
def asset_log_delete(request: Request, log_id: int, db: Session = Depends(get_db)):
    require_auth(request)
    log = db.get(AssetMaintenanceLog, log_id)
    if not log:
        raise HTTPException(404)
    asset_id = log.asset_id
    before = audit_svc.snapshot(log)
    schedule_svc.remove_log(db, log)
    audit_svc.log_audit(db, entity_type="asset_log", entity_id=log_id, action="delete", before=before)
    db.commit()
    return _schedule_back(f"/assets/{asset_id}", "Log entry removed.")


@router.post("/schedule")
def schedule_create(
    request: Request,
    asset: str = Form(""),
    asset_id: str = Form(""),
    task: str = Form(...),
    frequency: str = Form("monthly"),
    custom_interval_days: int | None = Form(None),
    last_done_on: str = Form(""),
    next_due_on: str = Form(""),
    vendor: str = Form(""),
    notes: str = Form(""),
    back: str = Form("/schedule"),
    db: Session = Depends(get_db),
):
    require_auth(request)
    freq = _frequency(frequency)
    if asset_id.strip().isdigit():
        register = _live_asset(db, int(asset_id))
    elif asset.strip():
        register = schedule_svc.get_or_create_asset(db, asset)
    else:
        raise HTTPException(400, detail="Choose an asset.")
    if freq == ScheduleFrequency.CUSTOM and not custom_interval_days:
        raise HTTPException(400, detail="Enter the number of days for a custom frequency.")
    last_d = _opt_date(last_done_on)
    next_d = _opt_date(next_due_on)
    computed = schedule_svc.compute_next_due(last_d, freq, custom_interval_days, next_d)
    item = AssetMaintenanceSchedule(
        asset_id=register.id,
        asset=register.name,
        task=task.strip(),
        frequency=freq,
        custom_interval_days=custom_interval_days if freq == ScheduleFrequency.CUSTOM else None,
        last_done_on=last_d,
        next_due_on=computed,
        vendor=vendor or None,
        notes=notes or None,
    )
    db.add(item)
    db.flush()
    audit_svc.log_audit(db, entity_type="asset_schedule", entity_id=item.id, action="create", after=item)
    db.commit()
    return _schedule_back(back, f"Task added for {register.name}.")


@router.post("/schedule/{item_id}/edit")
def schedule_update(
    request: Request,
    item_id: int,
    task: str = Form(...),
    frequency: str = Form("monthly"),
    custom_interval_days: int | None = Form(None),
    next_due_on: str = Form(""),
    vendor: str = Form(""),
    notes: str = Form(""),
    back: str = Form("/schedule"),
    db: Session = Depends(get_db),
):
    require_auth(request)
    item = _live_task(db, item_id)
    before = audit_svc.snapshot(item)
    freq = _frequency(frequency)
    if freq == ScheduleFrequency.CUSTOM and not custom_interval_days:
        raise HTTPException(400, detail="Enter the number of days for a custom frequency.")
    changed = freq != item.frequency or (custom_interval_days or None) != item.custom_interval_days
    item.task = task.strip() or item.task
    item.frequency = freq
    item.custom_interval_days = custom_interval_days if freq == ScheduleFrequency.CUSTOM else None
    item.vendor = vendor.strip() or None
    item.notes = notes.strip() or None
    explicit = _opt_date(next_due_on)
    if explicit:
        item.next_due_on = explicit
    elif changed and item.last_done_on:
        item.next_due_on = schedule_svc.add_interval(item.last_done_on, freq, item.custom_interval_days)
    audit_svc.log_audit(db, entity_type="asset_schedule", entity_id=item.id, action="update", before=before, after=item)
    db.commit()
    return _schedule_back(back, "Task updated.")


@router.post("/schedule/{item_id}/done")
def schedule_mark_done(
    request: Request,
    item_id: int,
    done_on: str = Form(...),
    done_by: str = Form(""),
    notes: str = Form(""),
    cost: str = Form(""),
    back: str = Form("/schedule"),
    db: Session = Depends(get_db),
):
    require_auth(request)
    item = _live_task(db, item_id)
    before = audit_svc.snapshot(item)
    done = _opt_date(done_on) or date.today()
    log = schedule_svc.record_log(db, item.asset_ref or _live_asset(db, item.asset_id), item, done,
                                  done_by=done_by.strip(), notes=notes.strip(), cost=_opt_amount(cost),
                                  created_by=getattr(request.state, "username", None))
    audit_svc.log_audit(
        db, entity_type="asset_schedule", entity_id=item.id, action="done", before=before, after=item
    )
    db.commit()
    return _schedule_back(back, f"{item.task} marked done. Next due {dates_util.format_date_ddmmyyyy(log.next_due_on)}.")


@router.post("/schedule/{item_id}/delete")
def schedule_delete(request: Request, item_id: int, back: str = Form("/schedule"), db: Session = Depends(get_db)):
    require_auth(request)
    item = db.get(AssetMaintenanceSchedule, item_id)
    if not item:
        raise HTTPException(404)
    before = audit_svc.snapshot(item)
    item.deleted_at = datetime.utcnow()
    audit_svc.log_audit(
        db, entity_type="asset_schedule", entity_id=item.id, action="delete", before=before, after=item
    )
    db.commit()
    return _schedule_back(back, "Task removed.")


@router.get("/flats", response_class=HTMLResponse)
def flats_list(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("authenticated"):
        return _redirect_login()
    flats = db.scalars(select(Flat).order_by(Flat.number)).all()
    return templates.TemplateResponse(
        request, "flats.html", {"request": request, "flats": flats}
    )


@router.get("/flats/{flat_id}", response_class=HTMLResponse)
def flat_detail(request: Request, flat_id: int, db: Session = Depends(get_db)):
    if not request.session.get("authenticated"):
        return _redirect_login()
    flat = db.get(Flat, flat_id)
    if not flat:
        raise HTTPException(404)
    summary = ledger_svc.flat_balances(db, flat)
    maintenance = db.scalars(
        select(MaintenancePayment).where(MaintenancePayment.flat_id == flat_id)
    ).all()
    expenses = db.scalars(
        select(Expense).where(Expense.paid_by_flat_id == flat_id, Expense.deleted_at.is_(None))
    ).all()
    return templates.TemplateResponse(
        request,
        "flat_detail.html",
        {
            "request": request,
            "flat": flat,
            "summary": summary,
            "maintenance": maintenance,
            "expenses": expenses,
            "reminders": reminders_svc.reminders_for_flats(db, [flat.id]),
            "corpus": campaigns_svc.flat_corpus_totals(db, flat.id),
            "corpus_payments": campaigns_svc.payments_for_flats(db, [flat.id]),
            "insights": insights_svc.flat_insights(db, [flat.id]),
            "my_advances": balances_svc.owner_advances(db, [flat.id]),
            "periods": periods_svc.list_periods_chronological(db),
        },
    )


def _statement_period(db: Session, period_id: str | int | None) -> LedgerPeriod | None:
    """Timeline picker: "" / "all" (or no value) = all time, otherwise one month's id."""
    raw = str(period_id if period_id is not None else "").strip().lower()
    if raw in ("", "all", "none"):
        return None
    if not raw.isdigit():
        raise HTTPException(400, detail="Unknown period.")
    period = db.get(LedgerPeriod, int(raw))
    if period is None:
        raise HTTPException(404)
    return period


def _pdf_response(content: bytes, filename: str) -> Response:
    return Response(content, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/flats/{flat_id}/statement.pdf")
def flat_statement_pdf(request: Request, flat_id: int, period_id: str = "", db: Session = Depends(get_db)):
    """My flat → Export to PDF (owners: own flats only, checked by the access guard)."""
    flat = db.get(Flat, flat_id)
    if not flat:
        raise HTTPException(404)
    period = _statement_period(db, period_id)
    scope = f"{period.year}-{period.month:02d}" if period else "all-time"
    return _pdf_response(pdf_report.build_flat_statement_pdf(db, [flat], period), f"flat-{flat.number}-{scope}.pdf")


@router.get("/owner/statement.pdf")
def my_flats_statement_pdf(request: Request, period_id: str = "", db: Session = Depends(get_db)):
    """All of the signed-in owner's flats in one PDF (admin: every flat)."""
    flat_ids = _my_flat_ids(request, db)
    flats = db.scalars(select(Flat).where(Flat.id.in_(flat_ids)).order_by(Flat.number)).all()
    if not flats:
        raise HTTPException(404)
    period = _statement_period(db, period_id)
    scope = f"{period.year}-{period.month:02d}" if period else "all-time"
    names = "-".join(f.number for f in flats) if len(flats) <= 3 else "all"
    return _pdf_response(pdf_report.build_flat_statement_pdf(db, list(flats), period), f"flats-{names}-{scope}.pdf")


def owners_see_building(db: Session) -> bool:
    return app_settings_svc.get_bool(db, app_settings_svc.OWNER_BUILDING_VIEW)


@router.get("/payments/building", response_class=HTMLResponse)
def building_payments_page(request: Request, period_id: int | None = None, db: Session = Depends(get_db)):
    """Read-only, building-wide payment status per flat (owners only when the admin allows it)."""
    if not is_admin(request) and not owners_see_building(db):
        raise HTTPException(403, detail="The building-wide payments view is turned off by the admin.")
    period = periods_svc.resolve_period(db, period_id)
    ctx = periods_svc.period_context(db, period)
    status = ledger_svc.build_dashboard_status(db, period)
    return templates.TemplateResponse(
        request,
        "payments_building.html",
        {**ctx, "request": request, "status": status, "base_path": "/payments/building",
         "owners_can_view": owners_see_building(db)},
    )


@router.post("/admin/settings/owner-building-view")
def set_owner_building_view(request: Request, enabled: str = Form("0"), db: Session = Depends(get_db)):
    on = enabled == "1"
    before = app_settings_svc.set_bool(db, app_settings_svc.OWNER_BUILDING_VIEW, on,
                                       updated_by=getattr(request.state, "username", "admin"))
    audit_svc.log_audit(db, entity_type="app_setting", entity_id=None, action="update",
                        before={"owner_building_view": before}, after={"owner_building_view": on})
    db.commit()
    state = "on" if on else "off"
    return RedirectResponse(f"/payments/building?msg=Owner+building-wide+view+turned+{state}.", status_code=303)


@router.get("/complaints", response_class=HTMLResponse)
def complaints_page(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("authenticated"):
        return _redirect_login()
    items = db.scalars(
        select(Complaint)
        .where(Complaint.deleted_at.is_(None))
        .order_by(Complaint.reported_on.desc())
    ).all()
    return templates.TemplateResponse(
        request, "complaints.html", {"request": request, "complaints": items}
    )


@router.post("/complaints")
def complaint_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    reported_on: str = Form(...),
    status: str = Form("open"),
    db: Session = Depends(get_db),
):
    require_auth(request)
    c = Complaint(
        title=title,
        description=description or None,
        reported_on=date.fromisoformat(reported_on),
        status=status if is_admin(request) else "open",
    )
    db.add(c)
    db.flush()
    audit_svc.log_audit(db, entity_type="complaint", entity_id=c.id, action="create", after=c)
    db.commit()
    return RedirectResponse("/complaints", status_code=303)


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("authenticated"):
        return _redirect_login()
    from app.models import AuditLog

    logs = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200)).all()
    return templates.TemplateResponse(
        request, "audit.html", {"request": request, "logs": logs}
    )


@router.get("/periods/rollover", response_class=HTMLResponse)
def rollover_page(request: Request, db: Session = Depends(get_db)):
    if not request.session.get("authenticated"):
        return _redirect_login()
    periods = db.scalars(select(LedgerPeriod).order_by(LedgerPeriod.year, LedgerPeriod.month)).all()
    return templates.TemplateResponse(
        request, "rollover.html", {"request": request, "periods": periods}
    )


@router.post("/periods/{period_id}/close")
def close_period(request: Request, period_id: int, db: Session = Depends(get_db)):
    require_auth(request)
    period = db.get(LedgerPeriod, period_id)
    if not period or period.is_closed:
        raise HTTPException(400)
    before = period
    closing = ledger_svc.close_period(db, period)
    audit_svc.log_audit(
        db, entity_type="ledger_period", entity_id=period.id, action="close", before=before, after=period
    )
    db.commit()
    return RedirectResponse("/periods/rollover", status_code=303)


@router.get("/statements/{period_id}", response_class=HTMLResponse)
def statement_export(request: Request, period_id: int, db: Session = Depends(get_db)):
    if not request.session.get("authenticated"):
        return _redirect_login()
    period = db.get(LedgerPeriod, period_id)
    if not period:
        raise HTTPException(404)
    collections = ledger_svc.maintenance_collected(db, period.id)
    expenses = ledger_svc.settlements_from_fund(db, period.id)
    balance = period.opening_fund_balance + collections - expenses
    payments = db.scalars(
        select(MaintenancePayment)
        .where(MaintenancePayment.period_id == period.id)
        .join(Flat, MaintenancePayment.flat_id == Flat.id)
        .options(selectinload(MaintenancePayment.flat))
        .order_by(Flat.number)
    ).all()
    visible = set(_my_flat_ids(request, db))
    payments = [mp for mp in payments if mp.flat_id in visible]
    return templates.TemplateResponse(
        request,
        "statement.html",
        {
            "request": request,
            "period": period,
            "collections": collections,
            "expenses": expenses,
            "balance": balance,
            "payments": payments,
            "label": ledger_svc.month_label(period),
        },
    )
