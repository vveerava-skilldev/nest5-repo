"""Login recovery, own-password change, and admin user management."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Flat, User, UserRole
from app.routers.web import templates
from app.services import audit as audit_svc
from app.services import mailer
from app.services import tenant as tenant_svc
from app.services import users as users_svc
from app.services.auth import generate_temp_password, password_problem

router = APIRouter()

FORGOT_DONE_MESSAGE = (
    "If that account exists and has a recovery email, a reset link has been sent. "
    "It works once and expires in {ttl} minutes."
)


def _base_url(request: Request) -> str:
    return (settings.app_base_url or str(request.base_url)).rstrip("/")


def _send_reset_link(db: Session, request: Request, user: User, *, reason: str) -> bool:
    """Email a single-use reset link. Returns False if the user has no recovery address."""
    to = users_svc.recovery_address(user)
    if not to:
        print(f"[nest5] Password reset requested for '{user.username}' but no email is on file.", flush=True)
        return False
    raw = users_svc.create_reset_token(db, user)
    link = f"{_base_url(request)}/reset-password?token={raw}"
    ttl = settings.reset_token_ttl_minutes
    body = (
        f"Hello {user.display_name or user.username},\n\n"
        f"A password reset was requested for your Nest5 Ledger login '{user.username}'.\n\n"
        f"Open this link to choose a new password (single use, expires in {ttl} minutes):\n"
        f"{link}\n\n"
        "If you did not ask for this, ignore this email — your password is unchanged.\n"
    )
    audit_svc.log_audit(
        db,
        entity_type="user",
        entity_id=user.id,
        action="password_reset_requested",
        after={"username": user.username, "sent_to": to, "reason": reason},
    )
    db.commit()
    mailer.send_email(to, "Nest5 Ledger — password reset link", body)
    return True


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return templates.TemplateResponse(
        request, "forgot_password.html", {"request": request, "message": None}
    )


@router.post("/forgot-password", response_class=HTMLResponse)
def forgot_password_submit(
    request: Request,
    login: str = Form(""),
    admin_recovery: str = Form(""),
    db: Session = Depends(get_db),
):
    users_svc.ensure_bootstrap_users(db)
    db.commit()
    if admin_recovery:
        user = users_svc.get_by_username(db, users_svc.ADMIN_USERNAME)
        reason = "admin_recovery"
    else:
        user = users_svc.get_by_login(db, login)
        reason = "forgot_password"
    if user and user.is_active:
        _send_reset_link(db, request, user, reason=reason)
    message = FORGOT_DONE_MESSAGE.format(ttl=settings.reset_token_ttl_minutes)
    if admin_recovery:
        message = (
            "An admin recovery link has been sent to the configured admin recovery email "
            f"(expires in {settings.reset_token_ttl_minutes} minutes)."
        )
    if not mailer.smtp_configured():
        message += " (Email is not set up on this server — the link was written to the server console and instance/outbox.log.)"
    return templates.TemplateResponse(
        request, "forgot_password.html", {"request": request, "message": message}
    )


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    tok = users_svc.lookup_reset_token(db, token)
    return templates.TemplateResponse(
        request,
        "reset_password.html",
        {
            "request": request,
            "token": token,
            "valid": tok is not None,
            "username": tok.user.username if tok else None,
            "error": None,
        },
        status_code=200 if tok else 400,
    )


@router.post("/reset-password", response_class=HTMLResponse)
def reset_password_submit(
    request: Request,
    token: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    tok = users_svc.lookup_reset_token(db, token)
    if not tok:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {"request": request, "token": token, "valid": False, "username": None, "error": None},
            status_code=400,
        )
    problem = password_problem(new_password, confirm_password)
    if problem:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {"request": request, "token": token, "valid": True, "username": tok.user.username, "error": problem},
            status_code=400,
        )
    user = tok.user
    users_svc.set_password(user, new_password)
    users_svc.consume_reset_token(db, tok)
    audit_svc.log_audit(
        db, entity_type="user", entity_id=user.id, action="password_reset",
        user_label=user.username, after={"username": user.username, "via": "email_link"},
    )
    db.commit()
    request.session.clear()
    return templates.TemplateResponse(
        request,
        "login.html",
        {"request": request, "error": None, "notice": "Password updated — please sign in.", "username": user.username},
    )


@router.get("/account/password", response_class=HTMLResponse)
def account_password_page(request: Request, db: Session = Depends(get_db)):
    user = db.get(User, request.state.user_id)
    return templates.TemplateResponse(
        request,
        "account_password.html",
        {"request": request, "forced": user.must_change_password, "error": None, "notice": None},
    )


@router.post("/account/password", response_class=HTMLResponse)
def account_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.get(User, request.state.user_id)
    error = None
    if not users_svc.authenticate(db, user.username, current_password):
        error = "Current password is incorrect."
    else:
        error = password_problem(new_password, confirm_password)
        if not error and new_password == current_password:
            error = "Choose a password different from the current one."
    if error:
        return templates.TemplateResponse(
            request,
            "account_password.html",
            {"request": request, "forced": user.must_change_password, "error": error, "notice": None},
            status_code=400,
        )
    users_svc.set_password(user, new_password)
    audit_svc.log_audit(
        db, entity_type="user", entity_id=user.id, action="password_change",
        after={"username": user.username},
    )
    db.commit()
    return templates.TemplateResponse(
        request,
        "account_password.html",
        {"request": request, "forced": False, "error": None, "notice": "Password changed."},
    )


# ---- Admin: user management ---------------------------------------------------------


def _users_context(request: Request, db: Session, **extra) -> dict:
    users = db.scalars(select(User).order_by(User.role, User.username)).all()
    flats = db.scalars(select(Flat).order_by(Flat.number)).all()
    return {
        "request": request,
        "users": users,
        "flats": flats,
        "roles": [r.value for r in UserRole],
        "tenant_sections": tenant_svc.SECTIONS,
        "tenant_labels": tenant_svc.LABELS,
        "tenant_sections_of": tenant_svc.sections_for,
        "smtp_ready": mailer.smtp_configured(),
        "message": None,
        "error": None,
        "temp_password": None,
        **extra,
    }


def _render_users(request: Request, db: Session, status_code: int = 200, **extra):
    return templates.TemplateResponse(
        request, "admin_users.html", _users_context(request, db, **extra), status_code=status_code
    )


def _flats_from_ids(db: Session, flat_ids: list[int]) -> list[Flat]:
    if not flat_ids:
        return []
    return list(db.scalars(select(Flat).where(Flat.id.in_(flat_ids)).order_by(Flat.number)).all())


def _get_user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404)
    return user


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, db: Session = Depends(get_db)):
    return _render_users(request, db)


@router.post("/admin/users", response_class=HTMLResponse)
def admin_user_create(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(""),
    email: str = Form(""),
    role: str = Form(UserRole.OWNER.value),
    flat_ids: list[int] = Form([]),
    tenant_payments: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    username = username.strip().lower()
    if role not in {r.value for r in UserRole}:
        raise HTTPException(400, "Unknown role")
    if not username or not username.replace(".", "").replace("_", "").replace("-", "").isalnum():
        return _render_users(request, db, 400, error="Username may use letters, digits, '.', '_' and '-'.")
    if users_svc.get_by_username(db, username):
        return _render_users(request, db, 400, error=f"Username '{username}' already exists.")
    temp = generate_temp_password()
    user = User(
        username=username,
        display_name=display_name.strip() or None,
        email=email.strip() or None,
        role=role,
        is_active=True,
        password_hash="",
        tenant_payments=tenant_svc.serialize(tenant_payments) if role == UserRole.TENANT.value else None,
    )
    users_svc.set_password(user, temp, must_change=True)
    user.flats = _flats_from_ids(db, flat_ids)
    db.add(user)
    db.flush()
    audit_svc.log_audit(db, entity_type="user", entity_id=user.id, action="create", after=users_svc.user_snapshot(user))
    db.commit()
    return _render_users(
        request, db,
        message=f"Created '{username}'. Give them this temporary password; they must change it at first login.",
        temp_password=(username, temp),
    )


@router.post("/admin/users/{user_id}/update", response_class=HTMLResponse)
def admin_user_update(
    request: Request,
    user_id: int,
    display_name: str = Form(""),
    email: str = Form(""),
    role: str = Form(...),
    flat_ids: list[int] = Form([]),
    tenant_payments: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    user = _get_user(db, user_id)
    if role not in {r.value for r in UserRole}:
        raise HTTPException(400, "Unknown role")
    if (
        user.role == UserRole.ADMIN.value
        and role != UserRole.ADMIN.value
        and user.is_active
        and users_svc.active_admin_count(db) <= 1
    ):
        return _render_users(request, db, 400, error="Cannot remove the last active admin.")
    before = users_svc.user_snapshot(user)
    user.display_name = display_name.strip() or None
    user.email = email.strip() or None
    user.role = role
    user.flats = _flats_from_ids(db, flat_ids)
    user.tenant_payments = tenant_svc.serialize(tenant_payments) if role == UserRole.TENANT.value else None
    after = users_svc.user_snapshot(user)
    action = "role_change" if before["role"] != after["role"] else "update"
    audit_svc.log_audit(db, entity_type="user", entity_id=user.id, action=action, before=before, after=after)
    db.commit()
    return _render_users(request, db, message=f"Saved '{user.username}'.")


@router.post("/admin/users/{user_id}/disable", response_class=HTMLResponse)
def admin_user_disable(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    if user.id == request.state.user_id:
        return _render_users(request, db, 400, error="You cannot disable your own account.")
    if user.role == UserRole.ADMIN.value and user.is_active and users_svc.active_admin_count(db) <= 1:
        return _render_users(request, db, 400, error="Cannot disable the last active admin.")
    before = users_svc.user_snapshot(user)
    user.is_active = False
    audit_svc.log_audit(db, entity_type="user", entity_id=user.id, action="disable", before=before, after=users_svc.user_snapshot(user))
    db.commit()
    return _render_users(request, db, message=f"Disabled '{user.username}'.")


@router.post("/admin/users/{user_id}/enable", response_class=HTMLResponse)
def admin_user_enable(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    before = users_svc.user_snapshot(user)
    user.is_active = True
    audit_svc.log_audit(db, entity_type="user", entity_id=user.id, action="enable", before=before, after=users_svc.user_snapshot(user))
    db.commit()
    return _render_users(request, db, message=f"Enabled '{user.username}'.")


@router.post("/admin/users/{user_id}/reset-password", response_class=HTMLResponse)
def admin_user_reset_password(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    temp = generate_temp_password()
    users_svc.set_password(user, temp, must_change=True)
    audit_svc.log_audit(
        db, entity_type="user", entity_id=user.id, action="password_reset",
        after={"username": user.username, "via": "admin_temporary_password"},
    )
    db.commit()
    return _render_users(
        request, db,
        message=f"New temporary password for '{user.username}' (shown once; they must change it at next login).",
        temp_password=(user.username, temp),
    )


@router.post("/admin/users/{user_id}/send-reset-link", response_class=HTMLResponse)
def admin_user_send_reset_link(request: Request, user_id: int, db: Session = Depends(get_db)):
    user = _get_user(db, user_id)
    if not _send_reset_link(db, request, user, reason="admin_sent_link"):
        return _render_users(request, db, 400, error=f"'{user.username}' has no email on file.")
    where = users_svc.recovery_address(user)
    suffix = "" if mailer.smtp_configured() else " (SMTP not configured — see instance/outbox.log)"
    return _render_users(request, db, message=f"Reset link for '{user.username}' sent to {where}{suffix}.")
