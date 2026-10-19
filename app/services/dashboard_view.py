"""Owner dashboard: Simple / Detailed view choice, per-user default, and the Simple view data.

The Detailed view is the original dashboard. The Simple view is a compact summary for
owners: their flats, a few metrics and their pending dues, each of which opens a pay
sheet that posts to the existing owner-submission endpoints.
"""
import re
from datetime import date

from sqlalchemy.orm import Session

from app.models import User
from app.services import upi as upi_svc

SIMPLE = "simple"
DETAILED = "detailed"
VIEWS = (SIMPLE, DETAILED)
SESSION_KEY = "dashboard_view"
VISIBLE_DUES = 4

_BACK_RE = re.compile(r"^/\?view=simple(&period_id=\d+)?$")


def safe_back(back: str, default: str) -> str:
    """Only the Simple dashboard (optionally with a month) is an allowed return address."""
    return back if _BACK_RE.match(back or "") else default


def resolve(session: dict, user: User | None, requested: str | None) -> tuple[str, bool]:
    """Pick the view. Returns (view, auto) where auto means nobody has chosen yet.

    Order: ?view= (remembered for this session) > session choice > saved default.
    With no choice at all the Detailed view is served and small screens are sent to Simple.
    """
    if requested in VIEWS:
        session[SESSION_KEY] = requested
        return requested, False
    if session.get(SESSION_KEY) in VIEWS:
        return session[SESSION_KEY], False
    saved = user.dashboard_view if user else None
    if saved in VIEWS:
        return saved, False
    return DETAILED, True


def set_default(db: Session, user: User, view: str | None) -> None:
    user.dashboard_view = view if view in VIEWS else None
    db.flush()


def simple_url(period_id: int | None = None, **extra) -> str:
    params = [("view", SIMPLE)]
    if period_id:
        params.append(("period_id", str(period_id)))
    params += [(k, str(v)) for k, v in extra.items() if v]
    return "/?" + "&".join(f"{k}={v}" for k, v in params)


def _due_rows(reminders: dict, payee: dict | None = None) -> list[dict]:
    payee = payee or {}
    rows = []
    for item in reminders["items"]:
        pay = item.get("pay")
        note = f"Flat {item['flat'].number} {pay['title']}" if pay else ""
        rows.append({
            **item,
            "key": pay["key"] if pay else None,
            "partial": bool(pay and pay["partial"]),
            "title": pay["title"] if pay else item["kind"],
            "upi_note": note[: upi_svc.NOTE_MAX],
            "upi_link": (upi_svc.build_link(item["amount"], note, vpa=payee.get("vpa"), payee=payee.get("payee"))
                         if pay and item["amount"] > 0 else None),
            **upi_svc.payee_details(payee.get("vpa"), payee.get("payee"), payee.get("mobile")),
        })
    return rows


def build_simple(
    *, reminders: dict, board_rows: list[dict], balances: dict, period, pay_key: str, show_all: bool,
    advances: dict | None = None, today: date | None = None, payee: dict | None = None,
) -> dict:
    today = today or date.today()
    dues = _due_rows(reminders, payee)
    visible = dues if show_all else dues[:VISIBLE_DUES]
    selected = next((d for d in dues if d["key"] and d["key"] == pay_key), None) if pay_key else None
    flats = [
        {"flat": r["flat"], "maintenance": r["maintenance"], "pending": r["row_pending"]}
        for r in board_rows
    ]
    return {
        "dues": dues,
        "visible": visible,
        "hidden_count": len(dues) - len(visible),
        "can_collapse": show_all and len(dues) > VISIBLE_DUES,
        "selected": selected,
        "flats": flats,
        "total_due": reminders["total"],
        "owed_back": advances["owed"] if advances else 0,
        "advances": advances["unsettled"] if advances else [],
        "advance_summary": advances if advances and advances.get("has_any") else None,
        "overdue": reminders["overdue"],
        "held": balances["held"],
        "effective": balances["effective"],
        "period": period,
        "today": today,
        "back_url": simple_url(period.id if period else None),
        "more_url": simple_url(period.id if period else None, more=1),
    }
