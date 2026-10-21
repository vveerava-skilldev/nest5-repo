from sqlalchemy.orm import Session

from app.config import settings
from app.models import LedgerPeriod
from app.services import corpus_campaigns as campaigns_svc
from app.services import ledger as ledger_svc
from app.services import schedule as schedule_svc
from app.dates import format_date as fmt_date


def build_dashboard_alerts(
    db: Session,
    period: LedgerPeriod | None,
    *,
    schedule_within_days: int = 7,
    flat_ids: list[int] | None = None,
) -> list[dict]:
    """flat_ids: owners only see per-flat amounts for their own flats (building totals stay)."""
    alerts: list[dict] = []
    available = ledger_svc.available_for_settlement(db)
    unsettled = ledger_svc.unsettled_expenses(db)

    if not available:
        alerts.append(
            {
                "severity": "critical",
                "icon": "⛔",
                "title": "Settlement blocked",
                "detail": "Account Balance is insufficient to reimburse owner advances.",
                "count": len(unsettled),
            }
        )
    elif unsettled:
        total = sum(e.amount for e in unsettled)
        if total > available:
            alerts.append(
                {
                    "severity": "critical",
                    "icon": "⛔",
                    "title": "Settlement blocked",
                    "detail": f"Pending reimbursements (₹{total}) exceed available Account Balance (₹{available}).",
                    "count": len(unsettled),
                }
            )

    buckets = schedule_svc.schedule_buckets(db, within_days=schedule_within_days)
    if buckets["overdue"]:
        alerts.append(
            {
                "severity": "critical",
                "icon": "🔧",
                "title": "Overdue asset maintenance",
                "detail": "Preventive tasks past due date — see Asset schedule.",
                "count": len(buckets["overdue"]),
            }
        )
    if buckets["upcoming"]:
        alerts.append(
            {
                "severity": "warning",
                "icon": "📅",
                "title": f"Due within {schedule_within_days} days",
                "detail": "Upcoming tank, lift, generator, or other scheduled tasks.",
                "count": len(buckets["upcoming"]),
            }
        )

    pid = period.id if period else None
    pending_m = ledger_svc.pending_maintenance(db, pid)
    if pending_m:
        alerts.append(
            {
                "severity": "warning",
                "icon": "🏠",
                "title": "Pending maintenance fees",
                "detail": f"₹{settings.maintenance_fee}/flat not yet recorded for this month.",
                "count": len(pending_m),
            }
        )

    pending_p = ledger_svc.pending_project_payments(db)
    if pending_p:
        alerts.append(
            {
                "severity": "info",
                "icon": "🏗",
                "title": "Pending project shares",
                "detail": "Flats with unpaid equal-share project contributions.",
                "count": len(pending_p),
            }
        )

    pending_c = ledger_svc.pending_corpus(db, pid)
    if pending_c:
        alerts.append(
            {
                "severity": "info",
                "icon": "💰",
                "title": "Pending corpus payments",
                "detail": "Corpus contributions not marked paid for this period.",
                "count": len(pending_c),
            }
        )

    for campaign in campaigns_svc.active_campaigns(db):
        summary = campaigns_svc.campaign_summary(db, campaign)
        owing = [r for r in summary["rows"] if r["remaining"] > 0]
        if not owing:
            continue
        status = owing[0]["status"]
        severity = {"overdue": "critical", "due_soon": "warning"}.get(status, "info")
        state = {"overdue": "overdue since", "due_soon": "due"}.get(status, "due")
        when = f" ({state} {fmt_date(campaign.due_date)})" if campaign.due_date else ""
        shown = owing if flat_ids is None else [r for r in owing if r["flat"].id in flat_ids]
        per_flat = " · ".join(f"{r['flat'].number}: ₹{r['remaining']}" for r in shown)
        label = "Remaining per flat" if flat_ids is None else "Your flat"
        alerts.append(
            {
                "severity": severity,
                "icon": "💰",
                "title": f"Corpus due — {campaign.name}{when}",
                "detail": (f"{label}: {per_flat}. " if per_flat else "")
                + f"Building total ₹{summary['remaining_total']}.",
                "count": len(owing),
            }
        )

    order = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda a: order.get(a["severity"], 9))
    return alerts
