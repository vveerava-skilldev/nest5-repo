"""Owner self-service (payments, expenses, corpus pay, cancel requests), corpus campaigns,
and the admin cancel-request queue."""
from datetime import date, datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.domain.expense_categories import ADD_CATEGORY_VALUE
from app.models import (
    CancelRequest,
    CorpusCampaign,
    CorpusCampaignShare,
    CorpusLineItem,
    CorpusPayment,
    Expense,
    Flat,
    LedgerPeriod,
    MaintenancePayment,
    ProjectPayment,
)
from app.routers.web import _expense_categories, templates
from app.services import audit as audit_svc
from app.services import corpus_campaigns as campaigns_svc
from app.services import ledger as ledger_svc
from app.services import owner_submissions as owner_svc
from app.services import projects as projects_svc
from app.services.access import can_view_flat, is_admin

router = APIRouter()


def _back(url: str, **params) -> RedirectResponse:
    params = {k: v for k, v in params.items() if v}
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}{urlencode(params)}" if params else url, status_code=303)


def _require_flat(request: Request, flat_id: int) -> None:
    if not can_view_flat(request, flat_id):
        raise HTTPException(status_code=403, detail="You can only record entries for your own flat.")


def _owner_user_id(request: Request) -> int | None:
    return None if is_admin(request) else request.state.user_id


def _parse_amount(raw: str) -> int | None:
    try:
        value = int(str(raw).replace(",", "").strip())
    except ValueError:
        return None
    return value if value > 0 else None


# ---- Owner: maintenance payment ------------------------------------------------------


@router.post("/owner/maintenance")
def owner_maintenance_submit(
    request: Request,
    flat_id: int = Form(...),
    period_id: int = Form(...),
    paid_on: str = Form(...),
    amount: str = Form(""),
    payment_mode: str = Form("UPI"),
    payment_note: str = Form(""),
    db: Session = Depends(get_db),
):
    _require_flat(request, flat_id)
    back = "/payments/maintenance"
    period = db.get(LedgerPeriod, period_id)
    if not period:
        raise HTTPException(404)
    back = f"/payments/maintenance?period_id={period.id}"
    ledger_svc.ensure_maintenance_rows(db, period)
    mp = db.scalar(
        select(MaintenancePayment).where(
            MaintenancePayment.period_id == period.id, MaintenancePayment.flat_id == flat_id
        )
    )
    if not mp:
        raise HTTPException(404)
    if mp.is_paid:
        db.commit()
        return _back(back, owner_error=f"{ledger_svc.month_label(period)} is already recorded as paid for this flat.")
    value = _parse_amount(amount) if amount.strip() else mp.amount
    if value is None:
        return _back(back, owner_error="Enter a valid amount.")
    before = audit_svc.snapshot(mp)
    mp.is_paid = True
    mp.paid_on = date.fromisoformat(paid_on)
    mp.amount = value
    mp.payment_mode = payment_mode if payment_mode in owner_svc.PAYMENT_MODES else "Other"
    mp.payment_note = payment_note.strip() or None
    mp.reverted_at = None
    mp.submitted_by_user_id = _owner_user_id(request)
    audit_svc.log_audit(db, entity_type="maintenance_payment", entity_id=mp.id, action="owner_submit", before=before, after=mp)
    db.commit()
    return _back(back, owner_msg=f"Recorded maintenance for {ledger_svc.month_label(period)} (₹{value}).")


# ---- Owner: common expense they paid ---------------------------------------------------


@router.post("/owner/expenses")
def owner_expense_submit(
    request: Request,
    flat_id: int = Form(...),
    description: str = Form(...),
    category: str = Form("Common"),
    amount: str = Form(...),
    expense_date: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_flat(request, flat_id)
    exp_date = date.fromisoformat(expense_date)
    period = campaigns_svc.period_for_date(db, exp_date)
    back = "/payments/expenses"
    if period is None:
        return _back(back, owner_error=f"{exp_date:%m-%Y} is not open in the ledger yet — ask the admin to add that month.")
    value = _parse_amount(amount)
    if value is None:
        return _back(back, owner_error="Enter a valid amount.")
    allowed = _expense_categories(db, owner=not is_admin(request))
    if category == ADD_CATEGORY_VALUE or category not in allowed:
        if not allowed:
            return _back(back, owner_error="No expense categories are open to owners — ask the admin.")
        category = "Others" if "Others" in allowed else allowed[-1]
    exp = Expense(
        description=description.strip(),
        category=category,
        amount=value,
        expense_date=exp_date,
        paid_by_flat_id=flat_id,
        period_id=period.id,
        is_settled=False,
        submitted_by_user_id=_owner_user_id(request),
    )
    db.add(exp)
    db.flush()
    audit_svc.log_audit(db, entity_type="expense", entity_id=exp.id, action="owner_submit", after=exp)
    db.commit()
    return _back(back, owner_msg=f"Recorded your expense of ₹{value}; it is pending settlement.")


# ---- Owner: pay a legacy corpus line item (partial allowed) ---------------------------


@router.post("/owner/corpus/{item_id}/pay")
def owner_corpus_item_pay(
    request: Request,
    item_id: int,
    amount: str = Form(...),
    paid_on: str = Form(...),
    payment_mode: str = Form("UPI"),
    payment_note: str = Form(""),
    db: Session = Depends(get_db),
):
    """Older per-item Pay button: line items now live in General corpus, so pay against that."""
    cp = db.get(CorpusPayment, item_id)
    if not cp:
        raise HTTPException(404)
    _require_flat(request, cp.flat_id)
    flat_id, was_paid = cp.flat_id, cp.is_paid
    if cp.campaign_id is None:
        campaigns_svc.absorb_line_items(db)
        db.commit()
    general = campaigns_svc.general_campaign(db)
    if was_paid or general is None:
        db.commit()
        return _back("/payments/corpus", owner_error="That corpus item is already paid.")
    ok, message = _record_corpus_payment(request, db, general, flat_id, amount, paid_on, payment_mode, payment_note)
    return _back("/payments/corpus", owner_msg=message) if ok else _back("/payments/corpus", owner_error=message)


# ---- Owner: pay a project share (partial allowed) ------------------------------------


@router.post("/owner/projects/pay")
def owner_project_pay(
    request: Request,
    share_id: int = Form(...),
    amount: str = Form(...),
    paid_on: str = Form(...),
    payment_mode: str = Form("UPI"),
    payment_note: str = Form(""),
    back: str = Form("/payments/projects"),
    db: Session = Depends(get_db),
):
    if back not in ("/payments/projects",) and not back.startswith("/projects/"):
        back = "/payments/projects"
    pp = db.get(ProjectPayment, share_id)
    if not pp:
        raise HTTPException(404)
    _require_flat(request, pp.flat_id)
    value = _parse_amount(amount)
    try:
        if value is None:
            raise projects_svc.ProjectPaymentError("Enter a valid amount.")
        row = projects_svc.record(
            db, pp, value, date.fromisoformat(paid_on),
            payment_mode=payment_mode if payment_mode in owner_svc.PAYMENT_MODES else "Other",
            note=payment_note.strip() or None, user_id=_owner_user_id(request),
        )
    except projects_svc.ProjectPaymentError as exc:
        db.rollback()
        return _back(back, owner_error=str(exc))
    audit_svc.log_audit(db, entity_type="project_contribution", entity_id=row.id, action="owner_submit", after=row)
    db.commit()
    left = f" ₹{pp.remaining} left." if pp.remaining else " Share fully paid."
    return _back(back, owner_msg=f"Recorded ₹{value} for {pp.project.name} (Flat {pp.flat.number}).{left}")


# ---- Corpus campaigns ---------------------------------------------------------------


def _campaign_or_404(db: Session, campaign_id: int) -> CorpusCampaign:
    campaign = db.scalar(
        select(CorpusCampaign)
        .where(CorpusCampaign.id == campaign_id)
        .options(selectinload(CorpusCampaign.shares))
    )
    if not campaign:
        raise HTTPException(404)
    return campaign


def _visible_flat_ids(request: Request, db: Session) -> list[int]:
    if is_admin(request):
        return list(db.scalars(select(Flat.id)).all())
    return [fid for fid, _ in getattr(request.state, "user_flats", None) or []]


def _goal_url(campaign_id: int) -> str:
    return f"/corpus/goals/{campaign_id}"


@router.get("/corpus", response_class=HTMLResponse)
def corpus_goals_page(request: Request, db: Session = Depends(get_db), category: str = ""):
    """The one Corpus area: every goal with progress, the pay form, per-flat status."""
    campaigns = db.scalars(
        select(CorpusCampaign)
        .options(selectinload(CorpusCampaign.shares), selectinload(CorpusCampaign.line_items))
        .order_by(CorpusCampaign.closed_at.isnot(None), CorpusCampaign.due_date, CorpusCampaign.id.desc())
    ).all()
    all_categories = campaigns_svc.categories(db)
    if category:
        campaigns = [c for c in campaigns if campaigns_svc.goal_category(c) == category]
    flats = db.scalars(select(Flat).order_by(Flat.number)).all()
    visible = _visible_flat_ids(request, db)
    status_rows = campaigns_svc.open_campaigns_for_flats(db, visible)
    return templates.TemplateResponse(
        request,
        "corpus_campaigns.html",
        {
            "request": request,
            "summaries": [campaigns_svc.campaign_summary(db, c) for c in campaigns],
            "flats": flats,
            "building": campaigns_svc.building_totals(db),
            "flat_status": [
                {"flat": f, "totals": campaigns_svc.flat_corpus_totals(db, f.id)} for f in flats if f.id in visible
            ],
            "status_rows": status_rows,
            "categories": all_categories,
            "category": category,
            "back": "/corpus",
            "period": None,
            "today": date.today(),
        },
    )


@router.get("/corpus/campaigns", response_class=HTMLResponse)
def campaigns_page(request: Request):
    return RedirectResponse("/corpus", status_code=303)


@router.post("/corpus/categories")
def corpus_category_add(request: Request, name: str = Form(...), db: Session = Depends(get_db)):
    added = campaigns_svc.add_category(db, name)
    if not added:
        return _back("/corpus", error="Enter a category name.")
    audit_svc.log_audit(db, entity_type="corpus_category", entity_id=None, action="create", after={"name": added})
    db.commit()
    return _back("/corpus", msg=f"Category '{added}' is available for goals.")


@router.post("/corpus/campaigns")
async def campaign_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    target = _parse_amount(str(form.get("target_amount", "")))
    due_raw = str(form.get("due_date", "")).strip()
    if not name or target is None:
        return _back("/corpus", error="Title and a positive target amount are required.")
    flats = db.scalars(select(Flat).order_by(Flat.number)).all()
    equal = campaigns_svc.equal_split(target, len(flats))
    campaign = CorpusCampaign(
        name=name,
        description=str(form.get("description", "")).strip() or None,
        target_amount=target,
        due_date=date.fromisoformat(due_raw) if due_raw else None,
        created_by=getattr(request.state, "username", "admin"),
        category=campaigns_svc.add_category(db, str(form.get("category", ""))) or campaigns_svc.GENERAL_CATEGORY,
    )
    db.add(campaign)
    db.flush()
    for title, amount in campaigns_svc.parse_line_items(str(form.get("line_items", ""))):
        db.add(CorpusLineItem(campaign_id=campaign.id, title=title, target_amount=amount))
    for flat, default in zip(flats, equal):
        raw = str(form.get(f"share_{flat.id}", "")).strip()
        share = default if raw == "" else int(raw.replace(",", ""))
        if share < 0:
            share = 0
        db.add(CorpusCampaignShare(campaign_id=campaign.id, flat_id=flat.id, share_amount=share))
    db.flush()
    db.refresh(campaign)
    audit_svc.log_audit(
        db, entity_type="corpus_campaign", entity_id=campaign.id, action="create",
        after={
            "name": campaign.name, "category": campaign.category, "target_amount": target,
            "due_date": campaign.due_date,
            "shares": {s.flat.number: s.share_amount for s in campaign.shares},
            "line_items": [(i.title, i.target_amount) for i in campaign.line_items],
        },
    )
    db.commit()
    return _back(_goal_url(campaign.id), msg=f"Created corpus goal '{name}'.")


@router.get("/corpus/campaigns/{campaign_id}", response_class=HTMLResponse)
def campaign_detail_old(campaign_id: int):
    return RedirectResponse(_goal_url(campaign_id), status_code=303)


@router.get("/corpus/goals/{campaign_id}", response_class=HTMLResponse)
def campaign_detail(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    campaign = _campaign_or_404(db, campaign_id)
    visible = _visible_flat_ids(request, db)
    summary = campaigns_svc.campaign_summary(db, campaign)
    payments = db.scalars(
        select(CorpusPayment)
        .where(CorpusPayment.campaign_id == campaign.id, CorpusPayment.flat_id.in_(visible))
        .options(selectinload(CorpusPayment.flat), selectinload(CorpusPayment.submitted_by),
                 selectinload(CorpusPayment.line_item))
        .order_by(CorpusPayment.paid_on.desc(), CorpusPayment.id.desc())
    ).all()
    status_rows = [
        {**r, "campaign": campaign, "due_date": campaign.due_date}
        for r in summary["rows"] if r["flat"].id in visible and r["remaining"] > 0
    ] if campaign.closed_at is None else []
    return templates.TemplateResponse(
        request,
        "corpus_campaign_detail.html",
        {
            "request": request,
            "summary": summary,
            "line_items": campaigns_svc.line_item_summary(db, campaign),
            "status_rows": status_rows,
            "categories": campaigns_svc.categories(db),
            "back": _goal_url(campaign.id),
            "visible_rows": [r for r in summary["rows"] if r["flat"].id in visible],
            "campaign": campaign,
            "payments": payments,
            "cancel_pending": owner_svc.pending_requests_map(db, "corpus_payment"),
            "payment_modes": owner_svc.PAYMENT_MODES,
            "today": date.today(),
            "period": None,
        },
    )


@router.post("/corpus/campaigns/{campaign_id}/update")
async def campaign_update(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    campaign = _campaign_or_404(db, campaign_id)
    form = await request.form()
    before = {
        "name": campaign.name, "target_amount": campaign.target_amount, "due_date": campaign.due_date,
        "shares": {s.flat.number: s.share_amount for s in campaign.shares},
    }
    name = str(form.get("name", "")).strip()
    target = _parse_amount(str(form.get("target_amount", "")))
    if campaign.is_general and target is None:
        target = campaign.target_amount
    if not name or target is None:
        return _back(_goal_url(campaign.id), error="Title and a positive target amount are required.")
    due_raw = str(form.get("due_date", "")).strip()
    campaign.name = name
    campaign.target_amount = target
    campaign.due_date = date.fromisoformat(due_raw) if due_raw else None
    campaign.description = str(form.get("description", "")).strip() or None
    if str(form.get("category", "")).strip():
        campaign.category = campaigns_svc.add_category(db, str(form.get("category")))
    for share in campaign.shares:
        raw = str(form.get(f"share_{share.flat_id}", "")).strip()
        if raw:
            share.share_amount = max(int(raw.replace(",", "")), 0)
    if campaign.is_general:
        # General corpus has no fixed target: it is whatever the flats' shares add up to.
        campaign.target_amount = sum(sh.share_amount for sh in campaign.shares)
    if form.get("closed") and campaign.closed_at is None:
        campaign.closed_at = datetime.utcnow()
    elif not form.get("closed"):
        campaign.closed_at = None
    db.flush()
    audit_svc.log_audit(
        db, entity_type="corpus_campaign", entity_id=campaign.id, action="update", before=before,
        after={
            "name": campaign.name, "target_amount": campaign.target_amount, "due_date": campaign.due_date,
            "shares": {s.flat.number: s.share_amount for s in campaign.shares},
            "closed": campaign.closed_at is not None,
        },
    )
    db.commit()
    return _back(_goal_url(campaign.id), msg="Saved.")


@router.post("/corpus/goals/{campaign_id}/line-items")
def line_item_add(
    request: Request,
    campaign_id: int,
    title: str = Form(...),
    target_amount: str = Form(""),
    db: Session = Depends(get_db),
):
    campaign = _campaign_or_404(db, campaign_id)
    title = title.strip()
    if not title:
        return _back(_goal_url(campaign.id), error="Enter a line item title.")
    item = CorpusLineItem(campaign_id=campaign.id, title=title[:255], target_amount=_parse_amount(target_amount))
    db.add(item)
    db.flush()
    audit_svc.log_audit(db, entity_type="corpus_line_item", entity_id=item.id, action="create", after=item)
    db.commit()
    return _back(_goal_url(campaign.id), msg=f"Added line item '{item.title}'.")


@router.post("/corpus/goals/{campaign_id}/line-items/{item_id}/delete")
def line_item_delete(request: Request, campaign_id: int, item_id: int, db: Session = Depends(get_db)):
    campaign = _campaign_or_404(db, campaign_id)
    item = campaigns_svc.line_item_for(db, campaign, item_id)
    if item is None:
        raise HTTPException(404)
    before = audit_svc.snapshot(item)
    for p in item.payments:
        p.line_item_id = None
    db.delete(item)
    audit_svc.log_audit(db, entity_type="corpus_line_item", entity_id=item_id, action="delete", before=before)
    db.commit()
    return _back(_goal_url(campaign.id), msg="Line item removed; its payments still count for the goal.")


def _record_corpus_payment(
    request: Request,
    db: Session,
    campaign: CorpusCampaign,
    flat_id: int,
    amount: str,
    paid_on: str,
    payment_mode: str,
    payment_note: str,
    line_item_id: str = "",
) -> tuple[bool, str]:
    """Shared by the goal page Pay button and the Corpus / Payments › Corpus pay form."""
    _require_flat(request, flat_id)
    if campaign.closed_at is not None:
        return False, "This corpus goal is closed."
    line_item = campaigns_svc.line_item_for(db, campaign, line_item_id) if line_item_id else None
    if line_item_id and line_item is None:
        return False, "That line item does not belong to this goal."
    remaining = campaigns_svc.remaining_for(db, campaign, flat_id)
    value = _parse_amount(amount)
    if remaining <= 0:
        return False, f"This flat's {campaign.name} share is already fully paid."
    if value is None or value > remaining:
        return False, f"Enter an amount between ₹1 and ₹{remaining} (remaining {campaign.name} share)."
    paid_date = date.fromisoformat(paid_on)
    period = campaigns_svc.period_for_date(db, paid_date)
    cp = CorpusPayment(
        flat_id=flat_id,
        period_id=period.id if period else None,
        amount=value,
        is_paid=True,
        paid_on=paid_date,
        payment_note=payment_note.strip() or None,
        description=campaign.name,
        campaign_id=campaign.id,
        line_item_id=line_item.id if line_item else None,
        payment_mode=payment_mode if payment_mode in owner_svc.PAYMENT_MODES else "Other",
        submitted_by_user_id=_owner_user_id(request),
    )
    db.add(cp)
    db.flush()
    audit_svc.log_audit(
        db, entity_type="corpus_payment", entity_id=cp.id,
        action="owner_submit" if cp.submitted_by_user_id else "create", after=cp,
    )
    db.commit()
    left = remaining - value
    label = f"{campaign.name} › {line_item.title}" if line_item else campaign.name
    return True, f"Recorded ₹{value} for {label}." + (f" ₹{left} remaining." if left else " Share fully paid.")


@router.post("/corpus/goals/{campaign_id}/delete")
def goal_delete(request: Request, campaign_id: int, confirm: str = Form(""), db: Session = Depends(get_db)):
    """Delete a goal that has no payments. Goals with payments are blocked: revert the payments first."""
    campaign = _campaign_or_404(db, campaign_id)
    if campaign.is_general:
        return _back(_goal_url(campaign.id), error="General corpus cannot be deleted.")
    count = len(campaign.payments)
    if count:
        return _back(_goal_url(campaign.id), error=(
            f"Not deleted: {campaign.name} has {count} payment(s). Revert them below first, "
            "or close the goal instead (Edit → Closed) to keep its history."))
    if confirm.strip().upper() != "DELETE":
        return _back(_goal_url(campaign.id), error="Type DELETE to confirm.")
    before = {"name": campaign.name, "target_amount": campaign.target_amount, "category": campaign.category,
              "due_date": campaign.due_date, "shares": {s.flat.number: s.share_amount for s in campaign.shares},
              "line_items": [i.title for i in campaign.line_items]}
    name = campaign.name
    db.delete(campaign)  # shares and line items cascade
    audit_svc.log_audit(db, entity_type="corpus_campaign", entity_id=campaign_id, action="delete", before=before)
    db.commit()
    return _back("/corpus", msg=f"Deleted corpus goal {name}.")


@router.post("/corpus/campaigns/{campaign_id}/pay")
def campaign_pay(
    request: Request,
    campaign_id: int,
    flat_id: int = Form(...),
    amount: str = Form(...),
    paid_on: str = Form(...),
    payment_mode: str = Form("UPI"),
    payment_note: str = Form(""),
    line_item_id: str = Form(""),
    db: Session = Depends(get_db),
):
    _require_flat(request, flat_id)
    campaign = _campaign_or_404(db, campaign_id)
    ok, message = _record_corpus_payment(
        request, db, campaign, flat_id, amount, paid_on, payment_mode, payment_note, line_item_id
    )
    back = _goal_url(campaign.id)
    return _back(back, msg=message) if ok else _back(back, error=message)


@router.post("/corpus/pay")
def corpus_pay_any(
    request: Request,
    target: str = Form(...),
    amount: str = Form(...),
    paid_on: str = Form(...),
    payment_mode: str = Form("UPI"),
    payment_note: str = Form(""),
    line_item_id: str = Form(""),
    back: str = Form("/payments/corpus"),
    db: Session = Depends(get_db),
):
    """Corpus / Payments › Corpus: the payer picks the flat + goal (any open one, incl. General corpus)."""
    if back not in ("/payments/corpus", "/corpus") and not back.startswith("/corpus/goals/"):
        back = "/payments/corpus"
    try:
        flat_id, campaign_id = (int(x) for x in target.split(":", 1))
    except ValueError:
        return _back(back, error="Choose which corpus goal you are paying.")
    _require_flat(request, flat_id)
    campaign = _campaign_or_404(db, campaign_id)
    ok, message = _record_corpus_payment(
        request, db, campaign, flat_id, amount, paid_on, payment_mode, payment_note, line_item_id
    )
    return _back(back, msg=message) if ok else _back(back, error=message)


@router.post("/corpus/campaigns/{campaign_id}/payments/{payment_id}/delete")
def campaign_payment_delete(request: Request, campaign_id: int, payment_id: int, db: Session = Depends(get_db)):
    cp = db.get(CorpusPayment, payment_id)
    if not cp or cp.campaign_id != campaign_id:
        raise HTTPException(404)
    before = audit_svc.snapshot(cp)
    pending = owner_svc.pending_requests_map(db, "corpus_payment").get(cp.id)
    db.delete(cp)
    if pending:
        pending.status = "approved"
        pending.decided_at = datetime.utcnow()
        pending.decided_by = getattr(request.state, "username", "admin")
        pending.decision_note = "Payment deleted by admin"
    audit_svc.log_audit(db, entity_type="corpus_payment", entity_id=payment_id, action="delete", before=before)
    db.commit()
    return _back(_goal_url(campaign_id), msg="Payment deleted.")


# ---- Cancel requests ----------------------------------------------------------------


@router.post("/owner/cancel-requests")
def cancel_request_create(
    request: Request,
    record_type: str = Form(...),
    record_id: int = Form(...),
    reason: str = Form(""),
    back: str = Form("/owner/submissions"),
    db: Session = Depends(get_db),
):
    if not back.startswith("/") or back.startswith("//"):
        back = "/owner/submissions"
    try:
        owner_svc.request_cancel(
            db, user_id=request.state.user_id, record_type=record_type, record_id=record_id, reason=reason
        )
    except owner_svc.SubmissionError as exc:
        db.rollback()
        return _back(back, owner_error=str(exc))
    db.commit()
    return _back(back, owner_msg="Cancel request sent to the admin. The entry still counts until the admin decides.")


@router.get("/owner/submissions", response_class=HTMLResponse)
def owner_submissions_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "owner_submissions.html",
        {"request": request, "items": owner_svc.submissions_for_user(db, request.state.user_id)},
    )


@router.get("/admin/cancel-requests", response_class=HTMLResponse)
def admin_cancel_requests(request: Request, db: Session = Depends(get_db)):
    reqs = db.scalars(
        select(CancelRequest)
        .options(selectinload(CancelRequest.requested_by), selectinload(CancelRequest.flat))
        .order_by(CancelRequest.status != "pending", CancelRequest.id.desc())
        .limit(200)
    ).all()
    return templates.TemplateResponse(
        request,
        "admin_cancel_requests.html",
        {"request": request, "requests": reqs, "labels": owner_svc.RECORD_LABELS},
    )


@router.post("/admin/cancel-requests/{req_id}/{decision}")
def admin_cancel_decide(
    request: Request,
    req_id: int,
    decision: str,
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    req = db.get(CancelRequest, req_id)
    if not req:
        raise HTTPException(404)
    admin_label = getattr(request.state, "username", "admin")
    try:
        if decision == "approve":
            owner_svc.approve(db, req, admin_label=admin_label, note=note)
        elif decision == "reject":
            owner_svc.reject(db, req, admin_label=admin_label, note=note)
        else:
            raise HTTPException(404)
    except owner_svc.SubmissionError as exc:
        db.rollback()
        return _back("/admin/cancel-requests", error=str(exc))
    db.commit()
    return _back("/admin/cancel-requests", msg=f"Request #{req.id} {req.status}.")
