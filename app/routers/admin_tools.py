"""Admin tools: expense category management and the Actions to-do hub."""
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ExpenseCategory
from app.routers.web import templates
from app.services import actions as actions_svc
from app.services import app_settings as app_settings_svc
from app.services import audit as audit_svc
from app.services import expense_categories as expense_cats_svc

router = APIRouter()

CATEGORIES_URL = "/admin/expense-categories"


def _back(url: str, **params) -> RedirectResponse:
    params = {k: v for k, v in params.items() if v}
    return RedirectResponse(f"{url}?{urlencode(params)}" if params else url, status_code=303)


def _category(db: Session, cat_id: int) -> ExpenseCategory:
    cat = db.get(ExpenseCategory, cat_id)
    if cat is None:
        raise HTTPException(404)
    return cat


@router.get(CATEGORIES_URL, response_class=HTMLResponse)
def categories_page(request: Request, db: Session = Depends(get_db)):
    rows = expense_cats_svc.all_rows(db)
    db.commit()
    return templates.TemplateResponse(request, "admin_expense_categories.html", {"request": request, "rows": rows})


@router.post(CATEGORIES_URL)
def category_add(request: Request, name: str = Form(...), owner_allowed: str = Form(""), db: Session = Depends(get_db)):
    try:
        cat = expense_cats_svc.add(db, name, owner_allowed=bool(owner_allowed))
    except expense_cats_svc.CategoryError as exc:
        db.rollback()
        return _back(CATEGORIES_URL, error=str(exc))
    audit_svc.log_audit(db, entity_type="expense_category", entity_id=cat.id, action="create", after=cat)
    db.commit()
    return _back(CATEGORIES_URL, msg=f"Added {cat.name}.")


@router.post(CATEGORIES_URL + "/{cat_id}/rename")
def category_rename(request: Request, cat_id: int, name: str = Form(...), update_existing: str = Form(""),
                    db: Session = Depends(get_db)):
    cat = _category(db, cat_id)
    before = audit_svc.snapshot(cat)
    old = cat.name
    try:
        changed = expense_cats_svc.rename(db, cat, name, update_existing=bool(update_existing))
    except expense_cats_svc.CategoryError as exc:
        db.rollback()
        return _back(CATEGORIES_URL, error=str(exc))
    audit_svc.log_audit(db, entity_type="expense_category", entity_id=cat.id, action="rename", before=before,
                        after={"name": cat.name, "expenses_updated": changed})
    db.commit()
    extra = f" {changed} existing expense(s) updated." if changed else " Existing expenses keep the old name."
    return _back(CATEGORIES_URL, msg=f"Renamed {old} to {cat.name}.{extra}")


@router.post(CATEGORIES_URL + "/{cat_id}/set")
def category_set(request: Request, cat_id: int, field: str = Form(...), value: str = Form(...),
                 db: Session = Depends(get_db)):
    cat = _category(db, cat_id)
    if field not in ("is_active", "owner_allowed"):
        raise HTTPException(400, detail="Unknown setting.")
    before = audit_svc.snapshot(cat)
    setattr(cat, field, value == "1")
    audit_svc.log_audit(db, entity_type="expense_category", entity_id=cat.id, action="update", before=before, after=cat)
    db.commit()
    label = {"is_active": ("enabled", "disabled"), "owner_allowed": ("allowed for owners", "admin-only")}[field]
    return _back(CATEGORIES_URL, msg=f"{cat.name} is now {label[0] if value == '1' else label[1]}.")


@router.get("/admin/actions", response_class=HTMLResponse)
def actions_page(request: Request, db: Session = Depends(get_db)):
    hub = actions_svc.build(db)
    return templates.TemplateResponse(request, "admin_actions.html", {"request": request, "hub": hub})


@router.post("/admin/actions/submissions-reviewed")
def submissions_reviewed(request: Request, db: Session = Depends(get_db)):
    app_settings_svc.set_value(db, actions_svc.REVIEWED_KEY, datetime.utcnow().isoformat(),
                               updated_by=getattr(request.state, "username", "admin"))
    db.commit()
    return _back("/admin/actions", msg="Owner submissions marked as reviewed.")
