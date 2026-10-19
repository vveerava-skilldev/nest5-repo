"""Admin-only Payee / UPI audit report (read-only).

Lists payments by the payee snapshot stored on each payment row (UPI ID, mobile, name, settings-history
version). Covered: maintenance, corpus and project payments, plus rejected owner submissions (kept in
submission_rejections with the payee they were submitted against). Not covered: expenses and owner
settlements/reimbursements are money going out and carry no payee snapshot.

Totals count only paid rows. Rejected and reverted rows are shown, marked, and left out of every total
unless the "include" toggle is on. Payments made in cash / bank transfer / cheque are grouped as
"No UPI / cash-bank" even though they carry a snapshot, because that money did not arrive at the UPI
account; rows with no payee at all (pending dues, reverted rows) fall in the same group.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CorpusPayment,
    Flat,
    LedgerPeriod,
    MaintenancePayment,
    PaymentDetailChange,
    Project,
    ProjectContribution,
    SubmissionRejection,
    User,
)
from app.dates import format_date_or_blank, to_ist
from app.services import payment_details as pay_svc

NO_UPI = "No UPI / cash-bank"
PAGE_SIZE = 25
NON_UPI_MODES = {"cash", "bank transfer", "cheque"}

TYPES = {"maintenance": "Maintenance", "corpus": "Corpus", "project": "Project"}
STATUSES = {
    "flagged": "Paid, rejected and reverted",
    "paid": "Paid (counted)",
    "pending": "Pending (not yet paid)",
    "rejected": "Rejected",
    "reverted": "Reverted",
    "all": "Everything",
}
MODES = ("UPI", "Cash", "Bank transfer", "Cheque", "Other", "none")
REJECT_TYPES = {"maintenance_payment": "maintenance", "corpus_payment": "corpus", "project_contribution": "project"}

COLUMNS = (
    "Date", "Flat", "Owner", "Type", "Detail", "Amount", "Mode", "UTR / reference", "Status", "Counted",
    "Paid to UPI ID", "Paid to mobile", "Paid to name", "Settings version", "Entered by",
)


@dataclass
class Filters:
    vpa: str = ""
    vpa_text: str = ""
    mobile: str = ""
    payee: str = ""
    version: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    type: str = "all"
    mode: str = "all"
    status: str = "flagged"
    flat: int | None = None
    include: bool = False
    page: int = 1

    def as_query(self) -> dict[str, str]:
        out = {
            "vpa": self.vpa, "vpa_text": self.vpa_text, "mobile": self.mobile, "payee": self.payee,
            "version": str(self.version or ""), "date_from": self.date_from.isoformat() if self.date_from else "",
            "date_to": self.date_to.isoformat() if self.date_to else "", "type": self.type, "mode": self.mode,
            "status": self.status, "flat": str(self.flat or ""), "include": "1" if self.include else "",
        }
        return {k: v for k, v in out.items() if v and not (k in ("type", "mode") and v == "all")
                and not (k == "status" and v == "flagged")}


def _date(raw: str | None) -> date | None:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return None


def _int(raw: str | None) -> int | None:
    try:
        return int((raw or "").strip())
    except ValueError:
        return None


def parse(params: Any) -> Filters:
    g = lambda k: (params.get(k) or "").strip()  # noqa: E731
    f = Filters(
        vpa=g("vpa"), vpa_text=g("vpa_text"), mobile=g("mobile"), payee=g("payee"), version=_int(g("version")),
        date_from=_date(g("date_from")), date_to=_date(g("date_to")), flat=_int(g("flat")),
        include=g("include") in ("1", "on", "true", "yes"), page=max(_int(g("page")) or 1, 1),
    )
    f.type = g("type") if g("type") in TYPES else "all"
    f.mode = g("mode") if g("mode") in MODES else "all"
    f.status = g("status") if g("status") in STATUSES else "flagged"
    return f


def _group(vpa: str | None, mode: str | None) -> str:
    if not vpa or (mode or "").strip().lower() in NON_UPI_MODES:
        return NO_UPI
    return vpa


def _row(*, kind: str, rid: int, when, flat: Flat | None, rtype: str, detail: str, amount: int | None, mode, ref,
         status: str, src, by: str, reason: str | None = None) -> dict:
    vpa = getattr(src, "payee_vpa", None)
    return {
        "key": f"{kind}:{rid}", "date": when, "flat_id": flat.id if flat else None, "flat": flat.number if flat else "—",
        "owner": (flat.contact_name or "—") if flat else "—", "type": rtype, "type_label": TYPES[rtype], "detail": detail,
        "amount": amount or 0, "mode": mode or "", "ref": ref or "", "status": status, "counted": status == "paid",
        "vpa": vpa or "", "mobile": getattr(src, "payee_mobile", None) or "", "payee": getattr(src, "payee_name", None) or "",
        "version": getattr(src, "payee_settings_id", None), "by": by, "reason": reason or "", "group": _group(vpa, mode),
    }


def _by(user: User | None, fallback: str = "admin") -> str:
    return f"owner {user.username}" if user is not None else fallback


def collect_all(db: Session) -> list[dict]:
    """Every payment-like row, unfiltered."""
    flats = {f.id: f for f in db.scalars(select(Flat)).all()}
    users = {u.id: u for u in db.scalars(select(User)).all()}
    periods = {p.id: p for p in db.scalars(select(LedgerPeriod)).all()}
    projects = {p.id: p for p in db.scalars(select(Project)).all()}
    rows: list[dict] = []

    for mp in db.scalars(select(MaintenancePayment)).all():
        status = "paid" if mp.is_paid else ("reverted" if mp.reverted_at else "pending")
        period = periods.get(mp.period_id)
        detail = f"Maintenance {period.month:02d}-{period.year}" if period else "Maintenance"
        rows.append(_row(kind="maintenance", rid=mp.id, when=mp.paid_on if mp.is_paid else None, flat=flats.get(mp.flat_id),
                         rtype="maintenance", detail=detail, amount=mp.amount, mode=mp.payment_mode, ref=mp.payment_note,
                         status=status, src=mp, by=_by(users.get(mp.submitted_by_user_id))))
    for cp in db.scalars(select(CorpusPayment)).all():
        status = "paid" if cp.is_paid else ("reverted" if cp.reverted_at else "pending")
        name = (cp.campaign.name if cp.campaign else None) or cp.description or "Corpus"
        rows.append(_row(kind="corpus", rid=cp.id, when=cp.paid_on if cp.is_paid else None, flat=flats.get(cp.flat_id),
                         rtype="corpus", detail=name, amount=cp.amount, mode=cp.payment_mode, ref=cp.payment_note,
                         status=status, src=cp, by=_by(users.get(cp.submitted_by_user_id))))
    for pc in db.scalars(select(ProjectContribution)).all():
        proj = projects.get(pc.project_id)
        if proj is None or proj.deleted_at is not None:
            continue
        rows.append(_row(kind="project", rid=pc.id, when=pc.paid_on, flat=flats.get(pc.flat_id), rtype="project",
                         detail=proj.name, amount=pc.amount, mode=pc.payment_mode, ref=pc.payment_note, status="paid",
                         src=pc, by=_by(users.get(pc.submitted_by_user_id))))
    for rj in db.scalars(select(SubmissionRejection)).all():
        rtype = REJECT_TYPES.get(rj.record_type)
        if rtype is None:
            continue
        rows.append(_row(kind="rejection", rid=rj.id, when=rj.paid_on or (to_ist(rj.rejected_at).date() if rj.rejected_at else None),
                         flat=flats.get(rj.flat_id), rtype=rtype, detail=rj.summary or TYPES[rtype], amount=rj.amount,
                         mode=None, ref=None, status="rejected", src=rj,
                         by=f"{_by(users.get(rj.submitted_by_user_id))}; rejected by {rj.rejected_by or 'admin'}",
                         reason=rj.reason))
    return rows


def apply_filters(rows: list[dict], f: Filters) -> list[dict]:
    out = []
    for r in rows:
        if f.vpa_text:
            if f.vpa_text.lower() not in r["vpa"].lower():
                continue
        elif f.vpa == NO_UPI:
            if r["group"] != NO_UPI:
                continue
        elif f.vpa and r["vpa"].lower() != f.vpa.lower():
            continue
        if f.mobile and f.mobile.replace(" ", "") not in r["mobile"]:
            continue
        if f.payee and f.payee.lower() not in r["payee"].lower():
            continue
        if f.version is not None and r["version"] != f.version:
            continue
        if f.date_from and (r["date"] is None or r["date"] < f.date_from):
            continue
        if f.date_to and (r["date"] is None or r["date"] > f.date_to):
            continue
        if f.type != "all" and r["type"] != f.type:
            continue
        if f.mode != "all":
            m = r["mode"] or "none"
            if m != f.mode:
                continue
        if f.flat is not None and r["flat_id"] != f.flat:
            continue
        if f.status == "flagged":
            if r["status"] == "pending":
                continue
        elif f.status != "all" and r["status"] != f.status:
            continue
        out.append(r)
    out.sort(key=lambda r: (r["date"] or date.min, r["key"]), reverse=True)
    return out


def _counts(r: dict, include: bool) -> bool:
    return r["counted"] or (include and r["status"] in ("rejected", "reverted"))


def summarize(rows: list[dict], history: list[PaymentDetailChange], include: bool) -> dict:
    """Per payee account, and per settings-change period next to the balance recorded then."""
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(r["group"], {"payee": r["group"], "count": 0, "total": 0, "excluded": 0, "excluded_total": 0})
        if _counts(r, include):
            g["count"] += 1
            g["total"] += r["amount"]
        elif r["status"] in ("rejected", "reverted"):
            g["excluded"] += 1
            g["excluded_total"] += r["amount"]
    by_payee = sorted(groups.values(), key=lambda g: (g["payee"] == NO_UPI, -g["total"], g["payee"]))

    ordered = sorted(history, key=lambda h: h.id)
    periods = []
    for i, h in enumerate(ordered):
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        mine = [r for r in rows if r["version"] == h.id and _counts(r, include)]
        upi = [r for r in mine if r["group"] != NO_UPI]
        other = [r for r in mine if r["group"] == NO_UPI]
        periods.append({
            "id": h.id, "vpa": h.new_upi_vpa, "mobile": h.new_upi_mobile, "payee": h.new_payee_name,
            "from": h.changed_at, "to": nxt.changed_at if nxt else None, "initial": bool(h.is_initial), "reason": h.reason,
            "upi_count": len(upi), "upi_total": sum(r["amount"] for r in upi),
            "other_count": len(other), "other_total": sum(r["amount"] for r in other),
            "fund_at_start": h.fund_balance, "total_at_start": h.total_balance, "bank_entered": h.bank_balance_entered,
            "fund_at_end": nxt.fund_balance if nxt else None, "total_at_end": nxt.total_balance if nxt else None,
        })
    counted = [r for r in rows if _counts(r, include)]
    return {
        "by_payee": by_payee, "periods": periods, "count": len(counted), "total": sum(r["amount"] for r in counted),
        "excluded": sum(1 for r in rows if r["status"] in ("rejected", "reverted") and not include),
    }


def vpa_choices(db: Session) -> list[str]:
    """Every UPI ID ever used (history, plus any on a payment row), newest first."""
    seen: list[str] = []
    for h in sorted(pay_svc.history(db), key=lambda h: h.id, reverse=True):
        for v in (h.new_upi_vpa, h.old_upi_vpa):
            if v and v not in seen:
                seen.append(v)
    for model in (MaintenancePayment, CorpusPayment, ProjectContribution, SubmissionRejection):
        for v in db.scalars(select(model.payee_vpa).where(model.payee_vpa.is_not(None)).distinct()).all():
            if v and v not in seen:
                seen.append(v)
    return seen


def build(db: Session, f: Filters) -> dict:
    all_rows = collect_all(db)
    rows = apply_filters(all_rows, f)
    history = list(pay_svc.history(db))
    if f.version is not None:
        history = [h for h in history if h.id == f.version]
    summary = summarize(rows, history, f.include)
    total_pages = max((len(rows) + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(f.page, total_pages)
    return {
        "rows": rows, "page_rows": rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE], "page": page, "pages": total_pages,
        "summary": summary, "history": list(pay_svc.history(db)), "vpas": vpa_choices(db),
        "flats": db.scalars(select(Flat).order_by(Flat.number)).all(),
    }


def export_rows(rows: list[dict], include: bool = False) -> list[list]:
    out = []
    for r in rows:
        out.append([
            format_date_or_blank(r["date"]), r["flat"], r["owner"], r["type_label"], r["detail"], r["amount"],
            r["mode"], r["ref"], r["status"].title() + (f" ({r['reason']})" if r["reason"] else ""),
            "Yes" if _counts(r, include) else "No", r["vpa"], r["mobile"], r["payee"], r["version"] or "", r["by"],
        ])
    return out
