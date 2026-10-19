"""Readable Recent activity: audit-log rows turned into one human sentence each.

`humanize()` knows every (entity_type, action) the app writes today; anything else falls back to a
generic "who did what to which record" line, so a new audit action never breaks the page.
"""
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.dates import ist_day_start_utc, ist_today
from app.models import AuditLog, CorpusCampaign, Flat, LedgerPeriod, Project, User

PAGE_SIZE = 25
CARD_SIZE = 10
ROLE_FILTERS = {"all": None, "owners": "owner", "admin": "admin", "tenants": "tenant"}
RANGE_DAYS = {"7": 7, "30": 30}

ENTITY_WORDS = {
    "maintenance_payment": "maintenance payment",
    "expense": "expense",
    "corpus_payment": "corpus payment",
    "project_contribution": "project payment",
    "project_payment": "project share",
    "project": "project",
    "corpus_campaign": "corpus goal",
    "corpus_campaign_share": "corpus share",
    "corpus_category": "corpus category",
    "corpus_line_item": "corpus line item",
    "cancel_request": "cancel request",
    "settlement": "settlement",
    "expense_category": "expense category",
    "maintenance_fee_revision": "maintenance fee revision",
    "ledger_period": "month",
    "asset_schedule": "asset task",
    "asset_task": "asset tasks",
    "asset": "asset",
    "asset_log": "asset log entry",
    "calendar_event": "calendar event",
    "recurring_task": "recurring task",
    "recurring_task_occurrence": "recurring task occurrence",
    "payment_settings": "payment settings",
    "complaint": "complaint",
    "user": "user account",
    "app_setting": "setting",
    "database": "database",
    "report": "report",
    "submission": "owner submission",
}


def inr(amount) -> str:
    """₹12,34,567 style (Indian digit grouping)."""
    try:
        n = int(amount)
    except (TypeError, ValueError):
        return ""
    sign = "-" if n < 0 else ""
    digits = str(abs(n))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        digits = ",".join(parts + [tail])
    return f"{sign}₹{digits}"


def _load(raw: str | None) -> dict:
    """Audit payloads are JSON; some rows were written through snapshot() twice (a JSON string)."""
    for _ in range(2):
        if not raw:
            return {}
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return {}
        if isinstance(value, dict):
            return value
        raw = value if isinstance(value, str) else None
    return {}


@dataclass
class Lookups:
    users: dict[str, User]
    flats: dict[int, str]
    periods: dict[int, str]
    campaigns: dict[int, str]
    projects: dict[int, str]


def lookups(db: Session) -> Lookups:
    return Lookups(
        users={u.username: u for u in db.scalars(select(User)).all()},
        flats={f.id: f.number for f in db.scalars(select(Flat)).all()},
        periods={p.id: f"{p.month:02d}-{p.year}" for p in db.scalars(select(LedgerPeriod)).all()},
        campaigns={c.id: c.name for c in db.scalars(select(CorpusCampaign)).all()},
        projects={x.id: x.name for x in db.scalars(select(Project)).all()},
    )


def _role_of(label: str, lk: Lookups) -> str:
    user = lk.users.get(label)
    if user is not None:
        return user.role
    return "admin" if label in ("admin", "", None) else "system"


def _flat_label(payload: dict, lk: Lookups) -> str | None:
    for key in ("flat_id", "paid_by_flat_id"):
        fid = payload.get(key)
        if isinstance(fid, int) and fid in lk.flats:
            return lk.flats[fid]
    return None


def _month(payload: dict, lk: Lookups) -> str | None:
    pid = payload.get("period_id")
    return lk.periods.get(pid) if isinstance(pid, int) else None


def _quoted(text) -> str:
    return f"“{text}”" if text else ""


def _sentence(entry: AuditLog, role: str, lk: Lookups) -> tuple[str, str | None, int | None]:
    """(what the actor did, flat number if known, amount if known) — without the actor prefix."""
    t, a = entry.entity_type, entry.action
    before, after = _load(entry.before_json), _load(entry.after_json)
    p = {**before, **after}
    flat = _flat_label(p, lk)
    amount = p.get("amount") if isinstance(p.get("amount"), int) else None
    amt = f" {inr(amount)}" if amount is not None else ""
    desc = _quoted(p.get("description") or p.get("name") or p.get("title"))
    owner_like = role in ("owner", "tenant")

    if t == "maintenance_payment":
        month = _month(p, lk)
        for_month = f" for {month}" if month else ""
        verb = {
            "owner_submit": "submitted maintenance",
            "pay": "recorded maintenance payment",
            "revert": "reverted maintenance payment",
            "reject": "rejected maintenance payment",
        }.get(a)
        if verb:
            return f"{verb}{amt}{for_month}", flat, amount
    elif t == "expense":
        if a == "reject":
            return f"rejected expense {desc}{amt}".replace("  ", " "), flat, amount
        verb = {
            "owner_submit": "submitted expense",
            "owner_edit": "edited their expense",
            "create": "added expense",
            "update": "updated expense",
            "delete": "deleted expense",
            "settle": "settled expense",
            "unsettle": "reverted the settlement of expense",
            "reinstate": "reinstated expense",
            "purge": "deleted rejected expense",
        }.get(a)
        if verb:
            return f"{verb} {desc}{amt}".replace("  ", " ").strip(), flat, amount
        if a == "clear_rejected":
            n = (after.get("purged") if isinstance(after.get("purged"), int) else None)
            return f"cleared {n if n is not None else 'all'} rejected expense(s)", None, None
    elif t == "corpus_payment":
        goal = f" to {_quoted(lk.campaigns[p['campaign_id']])}" if p.get("campaign_id") in lk.campaigns else ""
        verb = {
            "owner_submit": "submitted corpus payment",
            "create": "recorded corpus payment",
            "revert": "reverted corpus payment",
            "delete": "deleted corpus payment",
            "reject": "rejected corpus payment",
        }.get(a)
        if verb:
            return f"{verb}{amt}{goal}", flat, amount
    elif t in ("project_contribution", "project_payment"):
        verb = {
            "owner_submit": "submitted project payment",
            "pay": "recorded project payment",
            "revert": "reverted project payment",
            "delete": "deleted project payment",
            "reject": "rejected project payment",
        }.get(a)
        if verb:
            proj = f" for {_quoted(lk.projects[p['project_id']])}" if p.get("project_id") in lk.projects else ""
            return f"{verb}{amt}{proj}", flat, amount
    elif t == "project":
        verb = {"create": "added project", "update": "updated project", "delete": "deleted project"}.get(a)
        if verb:
            cost = p.get("total_cost")
            return f"{verb} {desc}{' ' + inr(cost) if isinstance(cost, int) else ''}".strip(), None, None
    elif t == "corpus_campaign":
        verb = {"create": "added corpus goal", "update": "updated corpus goal", "delete": "deleted corpus goal"}.get(a)
        if verb:
            target = p.get("target_amount")
            return (f"{verb} {desc}{' target ' + inr(target) if isinstance(target, int) else ''}".strip(), None,
                    target if isinstance(target, int) else None)
    elif t == "corpus_campaign_share":
        return "changed a corpus share", flat, amount
    elif t == "corpus_category":
        return f"added corpus category {desc}".strip(), None, None
    elif t == "corpus_line_item":
        verb = {"create": "added corpus line item", "delete": "deleted corpus line item"}.get(a)
        if verb:
            return f"{verb} {desc}".strip(), None, None
    elif t == "cancel_request":
        summary = (p.get("summary") or "").split(" — UTR")[0].split(" — due")[0]
        who_for = f": {summary}" if summary else ""
        verb = {
            "create": "asked to cancel an entry",
            "approve": "approved a cancel request",
            "reject": "kept an entry (cancel request declined)",
        }.get(a)
        if verb:
            return f"{verb}{who_for}", None, None
    elif t == "settlement":
        verb = {"create": "settled an expense", "update": "settled an expense", "revert": "reverted a settlement",
                "edit_note": "edited settlement note"}.get(a)
        if verb:
            return f"{verb}{amt}", flat, amount
    elif t == "expense_category":
        verb = {"create": "added expense category", "rename": "renamed expense category",
                "update": "changed expense category"}.get(a)
        if verb:
            return f"{verb} {desc}".strip(), None, None
    elif t == "maintenance_fee_revision":
        return f"revised the maintenance fee to {inr(amount)}".strip(), None, amount
    elif t == "ledger_period":
        if a == "auto_create":
            return f"auto-created maintenance dues for {p.get('month') or 'the new month'}", None, None
        return "closed the month", None, None
    elif t == "recurring_task":
        verb = {"create": "added recurring task", "update": "edited recurring task",
                "enable": "enabled recurring task", "disable": "disabled recurring task"}.get(a)
        if verb:
            return f"{verb} {_quoted(p.get('title'))}".strip(), None, None
    elif t == "recurring_task_occurrence":
        task = _quoted(p.get("task"))
        month = f" ({p['month']})" if p.get("month") else ""
        verb = {"done": "marked recurring task", "undo": "undid recurring task"}.get(a)
        if verb:
            return f"{verb} {task}{month}{' done' if a == 'done' else ''}".strip(), None, p.get("amount") if isinstance(p.get("amount"), int) else None
    elif t == "payment_settings":
        return "changed the payment details (UPI ID / mobile / payee)", None, None
    elif t == "asset_schedule":
        verb = {"done": "marked an asset task done", "delete": "deleted an asset task",
                "create": "added an asset task", "update": "updated an asset task"}.get(a)
        if verb:
            task = _quoted(p.get("task"))
            return f"{verb} {task}".strip(), None, None
    elif t == "asset":
        verb = {"create": "added asset", "update": "updated asset", "delete": "deleted asset"}.get(a)
        if verb:
            return f"{verb} {desc}".strip(), None, None
    elif t == "asset_log":
        verb = {"create": "logged maintenance on an asset", "delete": "deleted an asset maintenance log"}.get(a)
        if verb:
            cost = p.get("cost")
            return f"{verb}{' ' + inr(cost) if isinstance(cost, int) else ''}", None, cost if isinstance(cost, int) else None
    elif t == "asset_task" and a == "clear_all":
        return "cleared all asset tasks and alerts", None, None
    elif t == "calendar_event":
        verb = {"create": "added calendar event", "edit": "edited calendar event", "delete": "deleted calendar event"}.get(a)
        if verb:
            return f"{verb} {desc}".strip(), None, None
    elif t == "complaint" and a == "create":
        return f"raised complaint {desc}".strip(), None, None
    elif t == "app_setting":
        return "changed a setting", None, None
    elif t == "database" and a == "restore":
        return "restored the database from a backup", None, None
    elif t == "report" and a == "export":
        what = p.get("report") or "a"
        return f"exported the {what} report as {str(p.get('format') or 'file').upper()}", None, None
    elif t == "submission":
        if a == "review":
            return f"marked an owner submission as reviewed ({ENTITY_WORDS.get(p.get('kind'), 'submission')})", None, None
        if a == "review_all":
            n = after.get("count")
            return f"marked {n if isinstance(n, int) else 'all'} owner submission(s) as reviewed", None, None
    elif t == "user":
        target = p.get("username") or ""
        who = f" {target}" if target else ""
        verb = {
            "create": "created user",
            "update": "updated user",
            "role_change": "changed the role of user",
            "disable": "disabled user",
            "enable": "enabled user",
            "delete": "deleted user",
            "restore": "restored user",
            "password_reset": "reset the password of user",
        }.get(a)
        if a == "password_change":
            return "changed their password", None, None
        if a == "password_reset_requested":
            return "asked for a password reset link", None, None
        if verb:
            return f"{verb}{who}", None, None

    word = ENTITY_WORDS.get(t, t.replace("_", " "))
    action = a.replace("_", " ")
    ref = f" #{entry.entity_id}" if entry.entity_id is not None else ""
    return f"{action} on {word}{ref}", flat, amount


def humanize(entry: AuditLog, lk: Lookups) -> dict:
    label = entry.user_label or "admin"
    role = _role_of(label, lk)
    text, flat, amount = _sentence(entry, role, lk)
    user = lk.users.get(label)
    if flat:
        flats = f"Flat {flat}"
    elif user is not None and role in ("owner", "tenant") and user.flats:
        flats = ", ".join(f"Flat {f.number}" for f in user.flats)
    else:
        flats = None
    badge = {"admin": "Admin", "owner": "Owner", "tenant": "Tenant"}.get(role, "System")
    bits = [role] if role in ("owner", "tenant") else []
    if flats and role in ("owner", "tenant"):
        bits.append(flats)
    who = f"{label} ({', '.join(bits)})" if bits else label
    return {
        "id": entry.id,
        "when": entry.created_at,
        "actor": label,
        "role": role,
        "badge": badge,
        "flat": flats,
        "amount": amount,
        "who": who,
        "text": text,
        "sentence": f"{who} {text}",
        "entity": f"{entry.entity_type} #{entry.entity_id}" if entry.entity_id is not None else entry.entity_type,
        "action": entry.action,
    }


def humanize_many(db: Session, entries: list[AuditLog]) -> list[dict]:
    lk = lookups(db)
    return [humanize(e, lk) for e in entries]


def parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def feed(
    db: Session,
    *,
    role: str = "all",
    range_key: str = "",
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = PAGE_SIZE,
    offset: int = 0,
    today: date | None = None,
) -> dict:
    """Newest-first activity entries. Returns {items, total, has_more}."""
    today = today or ist_today()
    q = select(AuditLog)
    role = role if role in ROLE_FILTERS else "all"
    wanted = ROLE_FILTERS[role]
    if wanted is not None:
        names = [u.username for u in db.scalars(select(User).where(User.role == wanted)).all()]
        if wanted == "admin":
            names.append("admin")
        q = q.where(AuditLog.user_label.in_(names or [""]))
    start = end = None
    if range_key in RANGE_DAYS:
        start = ist_day_start_utc(today - timedelta(days=RANGE_DAYS[range_key] - 1))
    else:
        d_from, d_to = parse_date(date_from), parse_date(date_to)
        if d_from:
            start = ist_day_start_utc(d_from)
        if d_to:
            end = ist_day_start_utc(d_to + timedelta(days=1))
    if start:
        q = q.where(AuditLog.created_at >= start)
    if end:
        q = q.where(AuditLog.created_at < end)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).offset(offset)).all()
    return {"items": humanize_many(db, list(rows)), "total": total, "has_more": offset + len(rows) < total}


def recent(db: Session, limit: int = CARD_SIZE) -> list[dict]:
    return feed(db, limit=limit)["items"]
