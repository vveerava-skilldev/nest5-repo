"""Role-based access: one app-wide guard so every route is checked on the server.

Owners are deny-by-default: only the (method, route template) pairs listed below are
allowed, so any route added later is admin-only until it is explicitly opened up.
"""
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, UserRole
from app.services import app_settings as app_settings_svc
from app.services import tenant as tenant_svc
from app.services.audit import current_user_label

PUBLIC_PATHS = {"/login", "/logout", "/forgot-password", "/reset-password"}
PASSWORD_CHANGE_PATHS = {"/account/password", "/logout"}

OWNER_READ_PATHS = {
    "/",
    "/payments",
    "/payments/maintenance",
    "/maintenance",
    "/payments/corpus",
    "/payments/projects",
    "/payments/expenses",
    "/corpus",
    "/expenses",
    "/projects",
    "/projects/{project_id}",
    "/schedule",
    "/assets",
    "/assets/{asset_id}",
    "/complaints",
    "/statements/{period_id}",
    "/flats/{flat_id}",
    "/flats/{flat_id}/statement.pdf",
    "/owner/statement.pdf",
    "/payments/building",
    "/account/password",
    "/corpus/campaigns",
    "/corpus/campaigns/{campaign_id}",
    "/corpus/goals/{campaign_id}",
    "/owner/submissions",
    "/upi/qr.svg",
    "/calendar",
}
# Flat ownership for these is checked in the handlers (flat comes from the form/record).
OWNER_WRITE_PATHS = {
    "/complaints",
    "/account/password",
    "/logout",
    "/owner/maintenance",
    "/owner/expenses",
    "/owner/expenses/{expense_id}/edit",
    "/owner/corpus/{item_id}/pay",
    "/corpus/campaigns/{campaign_id}/pay",
    "/corpus/pay",
    "/owner/projects/pay",
    "/owner/cancel-requests",
    "/account/dashboard-view",
}


def _route_path(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


def _to_login(request: Request) -> HTTPException:
    if request.method in ("GET", "HEAD"):
        return HTTPException(status_code=303, headers={"Location": "/login"})
    return HTTPException(status_code=401, detail="Not authenticated")


async def _form_user(request: Request) -> str | None:
    """Username the submitted page was rendered for (hidden `_as_user` field), if any."""
    ctype = request.headers.get("content-type", "")
    if not ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        return None
    form = await request.form()
    value = form.get("_as_user")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _switched_redirect(request: Request, expected: str) -> str:
    """Back to the page the form was on (same site only), flagged so it can explain the switch."""
    referer = urlsplit(request.headers.get("referer", ""))
    same_site = not referer.netloc or referer.netloc == request.url.netloc
    path = referer.path if same_site and referer.path.startswith("/") and not referer.path.startswith("//") else "/"
    params = [(k, v) for k, v in parse_qsl(referer.query if same_site else "") if k != "session_switched"]
    params.append(("session_switched", expected))
    return f"{path}?{urlencode(params)}"


def owner_may_access(user_flat_ids: set[int], method: str, path: str, path_params: dict) -> bool:
    allowed = OWNER_READ_PATHS if method in ("GET", "HEAD") else OWNER_WRITE_PATHS
    if path not in allowed:
        return False
    if path in ("/flats/{flat_id}", "/flats/{flat_id}/statement.pdf"):
        try:
            return int(path_params.get("flat_id")) in user_flat_ids
        except (TypeError, ValueError):
            return False
    return True


async def access_guard(request: Request, db: Session = Depends(get_db)) -> None:
    current_user_label.set(None)
    path = _route_path(request)
    if path in PUBLIC_PATHS:
        return
    if not request.session.get("authenticated"):
        raise _to_login(request)

    user_id = request.session.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if not user or not user.is_active:
        # Pre-RBAC session cookie or a disabled/removed account: sign in again.
        request.session.clear()
        raise _to_login(request)

    flats = [(f.id, f.number) for f in user.flats]
    request.state.user_id = user.id
    request.state.username = user.username
    request.state.display_name = user.display_name or user.username
    request.state.role = user.role
    request.state.user_flats = flats
    request.state.tenant_sections = tenant_svc.sections_for(user) if user.role == UserRole.TENANT.value else None
    request.state.building_view = app_settings_svc.get_bool(db, app_settings_svc.OWNER_BUILDING_VIEW)
    request.session["role"] = user.role
    request.session["username"] = user.username
    current_user_label.set(user.username)

    if request.method == "POST" and path != "/logout":
        expected = await _form_user(request)
        if expected and expected != user.username:
            raise HTTPException(status_code=303, headers={"Location": _switched_redirect(request, expected)})

    if user.must_change_password and path not in PASSWORD_CHANGE_PATHS:
        raise HTTPException(status_code=303, headers={"Location": "/account/password"})
    if user.role == UserRole.ADMIN.value:
        return
    if user.role == UserRole.TENANT.value:
        if path == "/":
            return
        if not tenant_svc.may_access(request.state.tenant_sections, request.method, path):
            raise HTTPException(status_code=403, detail="This page is not available for your account.")
        return
    if not owner_may_access({fid for fid, _ in flats}, request.method, path, request.path_params):
        raise HTTPException(status_code=403, detail="This page is for administrators only.")


def is_admin(request: Request) -> bool:
    return getattr(request.state, "role", None) == UserRole.ADMIN.value


def is_tenant(request: Request) -> bool:
    return getattr(request.state, "tenant_sections", None) is not None


def tenant_may(request: Request, section: str) -> bool:
    """True for owners/admins; for tenants only the sections the admin enabled."""
    sections = getattr(request.state, "tenant_sections", None)
    return True if sections is None else section in sections


def can_view_flat(request: Request, flat_id: int) -> bool:
    if is_admin(request):
        return True
    return any(fid == flat_id for fid, _ in getattr(request.state, "user_flats", []))
