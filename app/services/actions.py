"""Admin Actions hub: one to-do list, each row linking to the page that resolves it."""
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    AuditLog,
    CancelRequest,
    Flat,
    LedgerPeriod,
    MaintenancePayment,
    PasswordResetToken,
    Project,
    ProjectPayment,
    User,
)
from app.services import app_settings as app_settings_svc
from app.services import balances as balances_svc
from app.services import corpus_campaigns as campaigns_svc
from app.services import ledger as ledger_svc
from app.services import schedule as schedule_svc

REVIEWED_KEY = "owner_submissions_reviewed_at"
SUBMISSION_LINKS = {
    "maintenance_payment": ("Maintenance payment", "/payments/maintenance"),
    "expense": ("Common expense", "/payments/expenses"),
    "corpus_payment": ("Corpus payment", "/payments/corpus"),
    "project_contribution": ("Project payment", "/payments/projects"),
}


def _reviewed_at(db: Session) -> datetime | None:
    raw = app_settings_svc.get_value(db, REVIEWED_KEY)
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def build(db: Session, today: date | None = None) -> dict:
    today = today or date.today()
    groups: list[dict] = []

    def group(key, title, link, rows, *, severity="warning", empty="Nothing to do.", action=None):
        groups.append({"key": key, "title": title, "link": link, "rows": rows, "count": len(rows),
                       "severity": severity, "empty": empty, "action": action})

    reqs = db.scalars(select(CancelRequest).where(CancelRequest.status == "pending").order_by(CancelRequest.id)).all()
    flats = {f.id: f for f in db.scalars(select(Flat)).all()}
    group("cancel_requests", "Cancel requests waiting", "/admin/cancel-requests", [
        {"text": r.summary or f"{r.record_type} #{r.record_id}",
         "detail": (f"Flat {flats[r.flat_id].number}" if r.flat_id in flats else "") + (f" · {r.reason}" if r.reason else ""),
         "link": "/admin/cancel-requests", "cta": "Approve / reject"} for r in reqs])

    bal = balances_svc.overview(db)
    settle_rows = [
        {"text": f"Flat {row['flat'].number}: ₹{row['total']} owed back ({len(row['items'])} expense(s))",
         "detail": ", ".join(f"{i['expense'].description} ₹{i['expense'].amount}" for i in row["items"][:3]),
         "link": "/settlements", "cta": "Settle"} for row in bal["by_flat"]]
    note = {"all": "Account Balance covers all of them.",
            "some": f"Account Balance (₹{bal['held']}) covers ₹{bal['settle_now']} of ₹{bal['owed']}.",
            "none": f"Account Balance (₹{bal['held']}) is not enough to settle any of them yet."}.get(bal["state"])
    group("settle", "Unsettled owner expenses to settle", "/settlements", settle_rows, empty="No owner expenses waiting.",
          action=note)

    overdue_m: dict[int, list] = {}
    for mp in db.scalars(
        select(MaintenancePayment).join(LedgerPeriod, MaintenancePayment.period_id == LedgerPeriod.id)
        .where(MaintenancePayment.is_paid.is_(False))
        .options(selectinload(MaintenancePayment.period), selectinload(MaintenancePayment.flat))
        .order_by(LedgerPeriod.year, LedgerPeriod.month)
    ).all():
        if (mp.period.year, mp.period.month) < (today.year, today.month):
            overdue_m.setdefault(mp.flat_id, []).append(mp)
    group("maintenance", "Overdue maintenance", "/payments/maintenance", [
        {"text": f"Flat {items[0].flat.number}: ₹{sum(m.amount for m in items)} unpaid",
         "detail": ", ".join(ledger_svc.month_label(m.period) for m in items),
         "link": f"/payments/maintenance?period_id={items[0].period_id}", "cta": "Record payment"}
        for _, items in sorted(overdue_m.items(), key=lambda kv: kv[1][0].flat.number)], severity="critical")

    corpus_rows = [
        {"text": f"Flat {d['flat'].number}: ₹{d['remaining']} left on {d['campaign'].name}",
         "detail": f"Due {d['due_date']:%d-%m-%Y}", "link": f"/corpus/goals/{d['campaign'].id}", "cta": "Open goal"}
        for d in campaigns_svc.dues_for_flats(db, None, today) if d["due_date"] and d["due_date"] < today]
    group("corpus", "Overdue corpus", "/corpus", sorted(corpus_rows, key=lambda r: r["text"]), severity="critical")

    project_rows = []
    for pp in db.scalars(
        select(ProjectPayment).join(Project)
        .where(Project.deleted_at.is_(None), Project.due_date.isnot(None), Project.due_date < today)
        .options(selectinload(ProjectPayment.project), selectinload(ProjectPayment.flat))
    ).all():
        if pp.remaining > 0:
            project_rows.append({"text": f"Flat {pp.flat.number}: ₹{pp.remaining} left on {pp.project.name}",
                                 "detail": f"Due {pp.project.due_date:%d-%m-%Y}", "link": f"/projects/{pp.project_id}",
                                 "cta": "Open project"})
    group("projects", "Overdue project shares", "/payments/projects", sorted(project_rows, key=lambda r: r["text"]),
          severity="critical")

    buckets = schedule_svc.schedule_buckets(db)
    group("assets", "Overdue asset tasks", "/schedule", [
        {"text": f"{t.asset}: {t.task}", "detail": f"Was due {t.next_due_on:%d-%m-%Y}",
         "link": f"/assets/{t.asset_id}" if t.asset_id else "/schedule", "cta": "Mark done"}
        for t in buckets["overdue"]], severity="critical")

    since = _reviewed_at(db)
    q = select(AuditLog).where(AuditLog.action == "owner_submit").order_by(AuditLog.created_at.desc())
    if since:
        q = q.where(AuditLog.created_at > since)
    subs = db.scalars(q.limit(50)).all()
    group("submissions", "New owner submissions to review", "/admin/actions", [
        {"text": f"{SUBMISSION_LINKS.get(a.entity_type, (a.entity_type, ''))[0]} by {a.user_label}",
         "detail": a.created_at.strftime("%d-%m-%Y %H:%M"),
         "link": SUBMISSION_LINKS.get(a.entity_type, ("", "/audit"))[1], "cta": "View"} for a in subs],
        severity="info", empty="No new owner submissions" + (f" since {since:%d-%m-%Y %H:%M}." if since else "."),
        action="mark_reviewed" if subs else None)

    now = datetime.utcnow()
    open_tokens = {t.user_id for t in db.scalars(
        select(PasswordResetToken).where(PasswordResetToken.used_at.is_(None), PasswordResetToken.expires_at > now)).all()}
    user_rows = []
    for u in db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.username)).all():
        reasons = []
        if u.must_change_password:
            reasons.append("still on a temporary password")
        if u.id in open_tokens:
            reasons.append("asked for a reset link")
        if reasons:
            user_rows.append({"text": u.username, "detail": " · ".join(reasons), "link": "/admin/users",
                              "cta": "Reset / send link"})
    group("users", "Users needing a password reset", "/admin/users", user_rows, severity="info")

    total = sum(g["count"] for g in groups)
    return {"groups": groups, "total": total, "balances": bal, "today": today}
