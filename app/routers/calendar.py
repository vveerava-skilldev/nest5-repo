"""Calendar page (admin + owners, read-only for owners) and admin-only event management."""
import json
from datetime import date, datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CalendarEvent
from app.routers.web import templates
from app.services import audit as audit_svc
from app.services import portal_calendar as cal_svc
from app.services.access import is_admin

router = APIRouter()


def _viewer(request: Request) -> cal_svc.Viewer:
    if is_admin(request):
        return cal_svc.Viewer(is_admin=True)
    return cal_svc.Viewer(is_admin=False, flat_ids={fid for fid, _ in getattr(request.state, "user_flats", [])})


def _require_admin(request: Request) -> None:
    # The app-wide guard already refuses owners; this keeps the handlers safe on their own.
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="Only the admin can manage calendar events.")


def _live_event(db: Session, event_id: int) -> CalendarEvent:
    ev = db.get(CalendarEvent, event_id)
    if ev is None:
        raise HTTPException(404)
    return ev


@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, m: str = "", db: Session = Depends(get_db)):
    today = date.today()
    year, month = cal_svc.parse_month(m, today)
    viewer = _viewer(request)
    start, end = cal_svc.month_bounds(year, month)
    items = cal_svc.month_items(db, year, month, viewer, today)
    days = cal_svc.expand_days(items, start, end)
    prev_y, prev_m = cal_svc.shift_month(year, month, -1)
    next_y, next_m = cal_svc.shift_month(year, month, 1)
    payload = {i["id"]: cal_svc.json_item(i) for i in items}
    return templates.TemplateResponse(
        request,
        "calendar.html",
        {
            "request": request,
            "compact_nav": True,
            "year": year,
            "month": month,
            "month_title": start.strftime("%B %Y"),
            "today": today,
            "weeks": cal_svc.month_grid(year, month, days),
            "agenda": cal_svc.agenda(days),
            "summary": cal_svc.summary(items),
            "types": cal_svc.TYPES,
            "prev_key": cal_svc.month_key(prev_y, prev_m),
            "next_key": cal_svc.month_key(next_y, next_m),
            "month_key": cal_svc.month_key(year, month),
            "month_options": cal_svc.month_options(year, month, today),
            "items_json": json.dumps(payload).replace("</", "<\\/"),
            "can_edit": viewer.is_admin,
            "is_current": (year, month) == (today.year, today.month),
        },
    )


def _form_context(request: Request, event: CalendarEvent | None, *, error: str | None = None,
                  values: dict | None = None, default_date: str = "") -> dict:
    return {
        "request": request,
        "event": event,
        "event_types": cal_svc.EVENT_TYPES,
        "error": error,
        "values": values or {},
        "default_date": default_date,
        "compact_nav": True,
        "back": "/calendar",
    }


@router.get("/calendar/events/new", response_class=HTMLResponse)
def event_new_form(request: Request, day: str = Query("", alias="date")):
    _require_admin(request)
    return templates.TemplateResponse(request, "calendar_event_form.html", _form_context(request, None, default_date=day[:10]))


@router.get("/calendar/events/{event_id}/edit", response_class=HTMLResponse)
def event_edit_form(request: Request, event_id: int, db: Session = Depends(get_db)):
    _require_admin(request)
    return templates.TemplateResponse(request, "calendar_event_form.html", _form_context(request, _live_event(db, event_id)))


def _fields_from_form(**raw) -> dict:
    return cal_svc.clean_event_fields(**raw)


def _redirect_month(day: date, msg: str) -> RedirectResponse:
    return RedirectResponse(f"/calendar?{urlencode({'m': cal_svc.month_key(day.year, day.month), 'msg': msg})}", status_code=303)


@router.post("/calendar/events")
def event_create(
    request: Request,
    title: str = Form(""),
    event_date: str = Form(""),
    end_date: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    event_type: str = Form("Other"),
    notes: str = Form(""),
    visible_to_owners: str = Form(""),
    db: Session = Depends(get_db),
):
    _require_admin(request)
    raw = dict(title=title, event_date=event_date, end_date=end_date, start_time=start_time, end_time=end_time,
               event_type=event_type, notes=notes, visible_to_owners=bool(visible_to_owners))
    try:
        fields = _fields_from_form(**raw)
    except cal_svc.EventError as exc:
        return templates.TemplateResponse(
            request, "calendar_event_form.html",
            _form_context(request, None, error=str(exc), values={**raw, "event_date": event_date, "end_date": end_date}),
            status_code=400)
    ev = CalendarEvent(**fields, created_by=getattr(request.state, "username", "admin"))
    db.add(ev)
    db.flush()
    audit_svc.log_audit(db, entity_type="calendar_event", entity_id=ev.id, action="create", after=ev)
    db.commit()
    return _redirect_month(ev.event_date, f"Added event {ev.title}.")


@router.post("/calendar/events/{event_id}")
def event_update(
    request: Request,
    event_id: int,
    title: str = Form(""),
    event_date: str = Form(""),
    end_date: str = Form(""),
    start_time: str = Form(""),
    end_time: str = Form(""),
    event_type: str = Form("Other"),
    notes: str = Form(""),
    visible_to_owners: str = Form(""),
    db: Session = Depends(get_db),
):
    _require_admin(request)
    ev = _live_event(db, event_id)
    raw = dict(title=title, event_date=event_date, end_date=end_date, start_time=start_time, end_time=end_time,
               event_type=event_type, notes=notes, visible_to_owners=bool(visible_to_owners))
    try:
        fields = _fields_from_form(**raw)
    except cal_svc.EventError as exc:
        return templates.TemplateResponse(
            request, "calendar_event_form.html",
            _form_context(request, ev, error=str(exc), values={**raw, "event_date": event_date, "end_date": end_date}),
            status_code=400)
    before = json.loads(audit_svc.snapshot(ev))
    for key, value in fields.items():
        setattr(ev, key, value)
    ev.updated_at = datetime.utcnow()
    ev.updated_by = getattr(request.state, "username", "admin")
    audit_svc.log_audit(db, entity_type="calendar_event", entity_id=ev.id, action="edit", before=before, after=ev)
    db.commit()
    return _redirect_month(ev.event_date, f"Saved event {ev.title}.")


@router.post("/calendar/events/{event_id}/delete")
def event_delete(request: Request, event_id: int, db: Session = Depends(get_db)):
    _require_admin(request)
    ev = _live_event(db, event_id)
    day, title = ev.event_date, ev.title
    audit_svc.log_audit(db, entity_type="calendar_event", entity_id=ev.id, action="delete", before=ev)
    db.delete(ev)
    db.commit()
    return _redirect_month(day, f"Deleted event {title}.")
