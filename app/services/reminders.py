"""What a flat still owes: unpaid maintenance, corpus (campaign + line items), project shares."""
import calendar
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import CorpusPayment, Flat, LedgerPeriod, MaintenancePayment, Project, ProjectPayment
from app.services import corpus_campaigns as campaigns_svc
from app.services import ledger as ledger_svc


def maintenance_due_date(period: LedgerPeriod) -> date:
    """Maintenance for a month is due by the last day of that month (after it, it shows overdue)."""
    return date(period.year, period.month, calendar.monthrange(period.year, period.month)[1])


def due_hint(due: date | None) -> str:
    return f"due {due:%d-%m-%Y}" if due else ""


def reminders_for_flats(db: Session, flat_ids: list[int], today: date | None = None) -> dict:
    today = today or date.today()
    flats = {f.id: f for f in db.scalars(select(Flat).where(Flat.id.in_(flat_ids))).all()}
    items: list[dict] = []

    for mp in db.scalars(
        select(MaintenancePayment)
        .join(LedgerPeriod, MaintenancePayment.period_id == LedgerPeriod.id)
        .where(MaintenancePayment.flat_id.in_(flat_ids), MaintenancePayment.is_paid.is_(False))
        .options(selectinload(MaintenancePayment.period))
        .order_by(LedgerPeriod.year, LedgerPeriod.month)
    ).all():
        p = mp.period
        month_end_passed = (p.year, p.month) < (today.year, today.month)
        items.append(
            {
                "kind": "Maintenance",
                "flat": flats[mp.flat_id],
                "label": ledger_svc.month_label(p),
                "amount": mp.amount,
                "due_date": None,
                "due_hint": due_hint(maintenance_due_date(p)),
                "status": "overdue" if month_end_passed else "open",
                "link": f"/payments/maintenance?period_id={p.id}",
                "pay": {"key": f"m-{p.id}-{mp.flat_id}", "period_id": p.id, "partial": False,
                        "title": "Maintenance " + date(p.year, p.month, 1).strftime("%b-%Y")},
            }
        )

    for due in campaigns_svc.dues_for_flats(db, flat_ids, today):
        items.append(
            {
                "kind": "Corpus",
                "flat": due["flat"],
                "label": f"{due['campaign'].name} (paid ₹{due['paid']} of ₹{due['share']})",
                "amount": due["remaining"],
                "due_date": due["due_date"],
                "due_hint": due_hint(due["due_date"]),
                "status": due["status"],
                "link": f"/corpus/goals/{due['campaign'].id}",
                "pay": {"key": f"c-{due['campaign'].id}-{due['flat'].id}", "campaign_id": due["campaign"].id,
                        "partial": True, "title": f"Corpus {due['campaign'].name}",
                        "total": due["share"], "paid": due["paid"]},
            }
        )

    for cp in db.scalars(
        select(CorpusPayment).where(
            CorpusPayment.flat_id.in_(flat_ids),
            CorpusPayment.is_paid.is_(False),
            CorpusPayment.campaign_id.is_(None),
        )
    ).all():
        items.append(
            {
                "kind": "Corpus",
                "flat": flats[cp.flat_id],
                "label": cp.description or "Corpus item",
                "amount": cp.amount,
                "due_date": None,
                "status": "open",
                "link": "/payments/corpus" + (f"?period_id={cp.period_id}" if cp.period_id else ""),
            }
        )

    for pp in db.scalars(
        select(ProjectPayment)
        .join(Project)
        .where(
            ProjectPayment.flat_id.in_(flat_ids),
            ProjectPayment.is_paid.is_(False),
            Project.deleted_at.is_(None),
        )
        .options(selectinload(ProjectPayment.project))
    ).all():
        due = pp.project.due_date
        if due and due < today:
            status = "overdue"
        elif due and (due - today).days <= 7:
            status = "due_soon"
        else:
            status = "open"
        items.append(
            {
                "kind": "Project",
                "flat": flats[pp.flat_id],
                "label": pp.project.name + (f" (paid ₹{pp.paid} of ₹{pp.amount})" if pp.paid else ""),
                "amount": pp.remaining,
                "due_date": due,
                "due_hint": due_hint(due),
                "status": status,
                "link": "/payments/projects",
                "pay": {"key": f"p-{pp.id}", "share_id": pp.id, "partial": True, "title": f"Project {pp.project.name}",
                        "total": pp.amount, "paid": pp.paid},
            }
        )

    order = {"overdue": 0, "due_soon": 1, "open": 2}
    items.sort(key=lambda i: (order.get(i["status"], 3), i["due_date"] or date.max, i["flat"].number))
    return {
        "items": items,
        "total": sum(i["amount"] for i in items),
        "overdue": sum(1 for i in items if i["status"] == "overdue"),
        "due_soon": sum(1 for i in items if i["status"] == "due_soon"),
    }
