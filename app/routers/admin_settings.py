"""Admin-only pages: recurring tasks and payment settings (with its append-only history)."""
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dates import ist_today
from app.models import RecurringTask, RecurringTaskOccurrence
from app.routers.web import templates
from app.services import payment_details as pay_svc
from app.services import recurring as recurring_svc
from app.services.access import is_admin
from app.services.audit import current_user_label

router = APIRouter()

RECURRING_URL = "/admin/recurring"
SETTINGS_URL = "/admin/payment-settings"
HISTORY_URL = SETTINGS_URL + "/history"
SAFE_BACK = (RECURRING_URL, "/admin/actions", "/")


def _admin(request: Request) -> str:
    if not is_admin(request):
        raise HTTPException(403, detail="This page is for administrators only.")
    return current_user_label.get() or "admin"


def _back(url: str, **params) -> RedirectResponse:
    params = {k: v for k, v in params.items() if v}
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}{urlencode(params)}" if params else url, status_code=303)


def _safe(back: str) -> str:
    return back if back in SAFE_BACK else RECURRING_URL


# ---- recurring tasks ---------------------------------------------------------------------------


def _task(db: Session, task_id: int) -> RecurringTask:
    task = db.get(RecurringTask, task_id)
    if task is None:
        raise HTTPException(404)
    return task


def _occurrence(db: Session, occ_id: int) -> RecurringTaskOccurrence:
    occ = db.get(RecurringTaskOccurrence, occ_id)
    if occ is None:
        raise HTTPException(404)
    return occ


@router.get(RECURRING_URL, response_class=HTMLResponse)
def recurring_page(request: Request, db: Session = Depends(get_db)):
    _admin(request)
    today = ist_today()
    return templates.TemplateResponse(
        request,
        "admin_recurring.html",
        {
            "request": request,
            "today": today,
            "tasks": recurring_svc.list_tasks(db),
            "occurrences": recurring_svc.current_occurrences(db, today),
            "done_rows": recurring_svc.recent_done(db),
            "label": recurring_svc.due_label,
            "repeat": recurring_svc.repeat_label,
            "status_for": recurring_svc.status_for,
        },
    )


@router.post(RECURRING_URL + "/tasks")
def recurring_add(
    request: Request,
    title: str = Form(""),
    day_of_month: str = Form(""),
    due_mode: str = Form("by"),
    interval_months: str = Form("1"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    actor = _admin(request)
    try:
        task = recurring_svc.create_task(
            db, actor=actor, title=title, day_of_month=day_of_month, due_mode=due_mode,
            interval_months=interval_months, notes=notes,
        )
    except recurring_svc.RecurringError as exc:
        db.rollback()
        return _back(RECURRING_URL, error=str(exc))
    db.commit()
    return _back(RECURRING_URL, msg=f"Added “{task.title}”.")


@router.post(RECURRING_URL + "/tasks/{task_id}/edit")
def recurring_edit(
    request: Request,
    task_id: int,
    title: str = Form(""),
    day_of_month: str = Form(""),
    due_mode: str = Form("by"),
    interval_months: str = Form("1"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    actor = _admin(request)
    task = _task(db, task_id)
    try:
        recurring_svc.update_task(
            db, task, actor=actor, title=title, day_of_month=day_of_month, due_mode=due_mode,
            interval_months=interval_months, notes=notes,
        )
    except recurring_svc.RecurringError as exc:
        db.rollback()
        return _back(RECURRING_URL, error=str(exc))
    db.commit()
    return _back(RECURRING_URL, msg=f"Saved “{task.title}”.")


@router.post(RECURRING_URL + "/tasks/{task_id}/toggle")
def recurring_toggle(request: Request, task_id: int, db: Session = Depends(get_db)):
    actor = _admin(request)
    task = _task(db, task_id)
    recurring_svc.set_active(db, task, not task.is_active, actor=actor)
    db.commit()
    return _back(RECURRING_URL, msg=f"“{task.title}” {'enabled' if task.is_active else 'disabled'}.")


@router.post(RECURRING_URL + "/occurrence/{occ_id}/done")
def recurring_done(
    request: Request,
    occ_id: int,
    done_on: str = Form(""),
    amount: str = Form(""),
    note: str = Form(""),
    back: str = Form(RECURRING_URL),
    db: Session = Depends(get_db),
):
    actor = _admin(request)
    occ = _occurrence(db, occ_id)
    url = _safe(back)
    try:
        recurring_svc.mark_done(db, occ, done_on=done_on or ist_today().isoformat(), amount=amount, note=note,
                                actor=actor)
    except recurring_svc.RecurringError as exc:
        db.rollback()
        return _back(url, error=str(exc))
    db.commit()
    return _back(url, msg=f"Marked “{occ.task.title}” ({occ.month:02d}-{occ.year}) done.")


@router.post(RECURRING_URL + "/occurrence/{occ_id}/undo")
def recurring_undo(request: Request, occ_id: int, back: str = Form(RECURRING_URL), db: Session = Depends(get_db)):
    actor = _admin(request)
    occ = _occurrence(db, occ_id)
    url = _safe(back)
    try:
        recurring_svc.undo_done(db, occ, actor=actor)
    except recurring_svc.RecurringError as exc:
        db.rollback()
        return _back(url, error=str(exc))
    db.commit()
    return _back(url, msg=f"Undid “{occ.task.title}” ({occ.month:02d}-{occ.year}).")


# ---- payment settings ----------------------------------------------------------------------------


def _settings_ctx(request: Request, db: Session, **extra) -> dict:
    current = pay_svc.get_details(db)
    ctx = {
        "request": request,
        "current": current,
        "form": dict(vpa=current["vpa"], mobile=current["mobile"], payee=current["payee"], reason="", bank=""),
        "errors": [],
        "preview": None,
        "last": pay_svc.latest_change(db),
    }
    ctx.update(extra)
    return ctx


@router.get(SETTINGS_URL, response_class=HTMLResponse)
def payment_settings_page(request: Request, db: Session = Depends(get_db)):
    _admin(request)
    pay_svc.ensure_initial(db)
    db.commit()
    return templates.TemplateResponse(request, "admin_payment_settings.html", _settings_ctx(request, db))


@router.post(SETTINGS_URL + "/review", response_class=HTMLResponse)
def payment_settings_review(
    request: Request,
    upi_vpa: str = Form(""),
    upi_mobile: str = Form(""),
    payee_name: str = Form(""),
    reason: str = Form(""),
    bank_balance: str = Form(""),
    db: Session = Depends(get_db),
):
    _admin(request)
    form = dict(vpa=upi_vpa.strip(), mobile=upi_mobile.strip(), payee=payee_name.strip(), reason=reason.strip(),
                bank=bank_balance.strip())
    ctx = _settings_ctx(request, db, form=form)
    try:
        clean = pay_svc.validate(upi_vpa, upi_mobile, payee_name, reason, bank_balance)
        changes = pay_svc.diff(ctx["current"], clean)
        if not changes:
            raise pay_svc.PaymentSettingsError("Nothing changed: the new values are the same as the current ones.")
    except pay_svc.PaymentSettingsError as exc:
        ctx["errors"] = exc.errors
        return templates.TemplateResponse(request, "admin_payment_settings.html", ctx, status_code=400)
    ctx["preview"] = {"changes": changes, "clean": clean, "balances": pay_svc.snapshot_balances(db)}
    return templates.TemplateResponse(request, "admin_payment_settings.html", ctx)


@router.post(SETTINGS_URL + "/confirm")
def payment_settings_confirm(
    request: Request,
    upi_vpa: str = Form(""),
    upi_mobile: str = Form(""),
    payee_name: str = Form(""),
    reason: str = Form(""),
    bank_balance: str = Form(""),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
):
    actor = _admin(request)
    if confirm != "yes":
        return _back(SETTINGS_URL, error="Review and confirm the change first.")
    try:
        row = pay_svc.apply_change(
            db, vpa=upi_vpa, mobile=upi_mobile, payee=payee_name, reason=reason, bank_balance=bank_balance,
            actor=actor,
        )
    except pay_svc.PaymentSettingsError as exc:
        db.rollback()
        return _back(SETTINGS_URL, error="; ".join(exc.errors))
    db.commit()
    return _back(HISTORY_URL, msg=f"Payment details updated (change #{row.id}). Past payments keep the payee they were made to.")


@router.get(HISTORY_URL, response_class=HTMLResponse)
def payment_settings_history(request: Request, db: Session = Depends(get_db)):
    _admin(request)
    pay_svc.ensure_initial(db)
    db.commit()
    return templates.TemplateResponse(
        request,
        "admin_payment_history.html",
        {"request": request, "rows": pay_svc.history(db), "current": pay_svc.get_details(db)},
    )
