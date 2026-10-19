"""Admin Actions hub: one to-do list, each row linking to the page that resolves it."""
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.dates import ist_today
from app.models import (
    CancelRequest,
    Flat,
    LedgerPeriod,
    MaintenancePayment,
    PasswordResetToken,
    Project,
    ProjectPayment,
    User,
)
from app.services import balances as balances_svc
from app.services import corpus_campaigns as campaigns_svc
from app.services import ledger as ledger_svc
from app.services import recurring as recurring_svc
from app.services import reminders as reminders_svc
from app.services import schedule as schedule_svc
from app.services import submission_review as review_svc
from app.dates import format_date as fmt_date, format_datetime as fmt_datetime

SECTIONS = (
    ("review", "Needs your review", ("submissions", "cancel_requests")),
    ("money", "Money", ("settle", "maintenance", "corpus", "projects", "recurring")),
    ("other", "Other", ("assets", "users")),
)


def build(db: Session, today: date | None = None) -> dict:
    today = today or ist_today()
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
        if today > reminders_svc.maintenance_due_date(mp.period):
            overdue_m.setdefault(mp.flat_id, []).append(mp)
    group("maintenance", "Overdue maintenance", "/payments/maintenance", [
        {"text": f"Flat {items[0].flat.number}: ₹{sum(m.amount for m in items)} unpaid",
         "detail": ", ".join(ledger_svc.month_label(m.period) for m in items),
         "link": f"/payments/maintenance?period_id={items[0].period_id}", "cta": "Record payment"}
        for _, items in sorted(overdue_m.items(), key=lambda kv: kv[1][0].flat.number)], severity="critical")

    corpus_rows = [
        {"text": f"Flat {d['flat'].number}: ₹{d['remaining']} left on {d['campaign'].name}",
         "detail": f"Due {fmt_date(d['due_date'])}", "link": f"/corpus/goals/{d['campaign'].id}", "cta": "Open goal"}
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
                                 "detail": f"Due {fmt_date(pp.project.due_date)}",
                                 "link": f"/projects/{pp.project_id}", "cta": "Open project"})
    group("projects", "Overdue project shares", "/payments/projects", sorted(project_rows, key=lambda r: r["text"]),
          severity="critical")

    recurring_rows = []
    for item in recurring_svc.open_occurrences(db, today):
        occ, task = item["occurrence"], item["task"]
        when = (f"overdue since {fmt_date(item['due_date'])}" if item["status"] == "overdue"
                else ("due today" if item["days"] == 0 else f"due {fmt_date(item['due_date'])}, in {item['days']} day(s)"))
        recurring_rows.append({
            "text": f"{task.title} ({occ.month:02d}-{occ.year})",
            "detail": f"{recurring_svc.due_label(task)} · {when}",
            "overdue": item["status"] == "overdue",
            "link": "/admin/recurring", "cta": "Mark done", "recurring_id": occ.id})
    group("recurring", "Recurring tasks due", "/admin/recurring", recurring_rows,
          severity="critical" if any(r["overdue"] for r in recurring_rows) else "warning")

    buckets = schedule_svc.schedule_buckets(db)
    group("assets", "Overdue asset tasks", "/schedule", [
        {"text": f"{t.asset}: {t.task}", "detail": f"Was due {fmt_date(t.next_due_on)}",
         "link": f"/assets/{t.asset_id}" if t.asset_id else "/schedule", "cta": "Mark done"}
        for t in buckets["overdue"]], severity="critical")

    subs = review_svc.pending(db)
    group("submissions", "New owner submissions to review", "/admin/actions", [
        {"text": f"{it['what']} · {it['flat']} · ₹{it['amount']}",
         "detail": f"{fmt_datetime(it['when'])} · by {it['user']}",
         "link": it["link"], "cta": "Open",
         "review": {"audit_id": it["audit_id"], "kind": it["kind"], "record_id": it["record_id"]}} for it in subs],
        severity="info", empty="No new owner submissions.", action="mark_reviewed" if subs else None)

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
    by_key = {g["key"]: g for g in groups}
    sections = []
    for key, title, keys in SECTIONS:
        members = [by_key[k] for k in keys if by_key[k]["count"]]
        if members:
            sections.append({"key": key, "title": title, "groups": members,
                             "count": sum(g["count"] for g in members)})
    return {"groups": groups, "sections": sections, "total": total, "balances": bal, "today": today}
