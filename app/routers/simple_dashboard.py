"""Owner dashboard view preference and the UPI QR image."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services import dashboard_view as dashboard_view_svc
from app.services import payment_details as payment_details_svc
from app.services import upi as upi_svc
from app.services.access import can_view_flat, is_admin

router = APIRouter()


@router.post("/account/dashboard-view")
def set_dashboard_view(
    request: Request,
    view: str = Form(...),
    make_default: str = Form(""),
    period_id: str = Form(""),
    db: Session = Depends(get_db),
):
    """Save (or clear) the signed-in user's default view; the page they are on is kept."""
    if view not in dashboard_view_svc.VIEWS:
        raise HTTPException(400, "Unknown view.")
    user = db.get(User, request.state.user_id)
    dashboard_view_svc.set_default(db, user, view if make_default else None)
    request.session[dashboard_view_svc.SESSION_KEY] = view
    db.commit()
    pid = int(period_id) if period_id.isdigit() else None
    suffix = f"&period_id={pid}" if pid else ""
    return RedirectResponse(f"/?view={view}{suffix}", status_code=303)


@router.get("/upi/qr.svg")
def upi_qr(request: Request, flat_id: int, amount: int, note: str = "", db: Session = Depends(get_db)):
    if not can_view_flat(request, flat_id):
        raise HTTPException(403, "You can only pay for your own flat.")
    if amount <= 0 or amount > 10_000_000:
        raise HTTPException(400, "Invalid amount.")
    details = payment_details_svc.get_details(db)
    svg = upi_svc.qr_svg(upi_svc.build_link(amount, note, vpa=details["vpa"], payee=details["payee"]))
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
