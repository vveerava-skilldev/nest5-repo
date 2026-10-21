"""Admin tools: expense category management and the Actions to-do hub."""
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ExpenseCategory
from app.routers.web import templates
from app.services import actions as actions_svc
from app.services import activity as activity_svc
from app.services import expense_review as expense_review_svc
from app.services import owner_submissions as owner_svc
from app.services import submission_review as review_svc
from app.services import audit as audit_svc
from app.services import expense_categories as expense_cats_svc

router = APIRouter()

CATEGORIES_URL = "/admin/expense-categories"


def _back(url: str, **params) -> RedirectResponse:
    params = {k: v for k, v in params.items() if v}
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}{urlencode(params)}" if params else url, status_code=303)


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


def _safe_back(back: str, default: str = "/admin/actions") -> str:
    return back if back.startswith("/") and not back.startswith("//") else default


@router.post("/admin/actions/submissions-reviewed")
def submissions_reviewed(request: Request, back: str = Form("/admin/actions"), db: Session = Depends(get_db)):
    n = review_svc.mark_all_reviewed(db, admin_label=getattr(request.state, "username", "admin"))
    db.commit()
    msg = f"{n} owner submission(s) marked as reviewed." if n else "No owner submissions waiting for review."
    return _back(_safe_back(back), msg=msg)


@router.post("/admin/actions/submissions/{audit_id}/reviewed")
def submission_reviewed(request: Request, audit_id: int, back: str = Form("/admin/actions"),
                        db: Session = Depends(get_db)):
    try:
        review_svc.mark_reviewed(db, audit_id, admin_label=getattr(request.state, "username", "admin"))
    except review_svc.ReviewError as exc:
        db.rollback()
        return _back(_safe_back(back), error=str(exc))
    db.commit()
    return _back(_safe_back(back), msg="Marked as reviewed. It stays in the history.")


@router.post("/admin/submissions/{kind}/{record_id}/reject")
def submission_reject(request: Request, kind: str, record_id: int, reason: str = Form(""),
                      back: str = Form("/admin/actions"), db: Session = Depends(get_db)):
    try:
        what = review_svc.reject_payment(
            db, kind, record_id, admin_label=getattr(request.state, "username", "admin"), reason=reason
        )
    except (review_svc.ReviewError, expense_review_svc.ExpenseReviewError, owner_svc.SubmissionError) as exc:
        db.rollback()
        return _back(_safe_back(back), error=str(exc))
    db.commit()
    return _back(_safe_back(back), msg=f"Rejected {what}; it no longer counts anywhere. The owner sees it as Rejected.")


@router.get("/admin/activity", response_class=HTMLResponse)
def activity_page(request: Request, role: str = "all", range: str = "", date_from: str = "", date_to: str = "",
                  page: int = 1, db: Session = Depends(get_db)):
    page = max(page, 1)
    data = activity_svc.feed(
        db, role=role, range_key=range, date_from=date_from, date_to=date_to,
        limit=activity_svc.PAGE_SIZE, offset=(page - 1) * activity_svc.PAGE_SIZE,
    )
    return templates.TemplateResponse(request, "admin_activity.html", {
        "request": request, "feed": data, "role": role if role in activity_svc.ROLE_FILTERS else "all",
        "range": range, "date_from": date_from, "date_to": date_to, "page": page,
        "page_size": activity_svc.PAGE_SIZE,
    })
