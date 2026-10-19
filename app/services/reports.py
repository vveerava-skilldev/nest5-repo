"""Admin-only Reports (read-only): consolidated summary, monthly trend, one category in detail, drilldown.

Every figure on every screen and export comes from one list of "entries" (one per payment, share, expense
or settlement) through one predicate (`pick`), so a number and the drilldown behind it can never differ.
Counting rules match the rest of the app: expenses use `Expense.counted()` (rejected and cancelled ones are
never in a total), maintenance/corpus/projects count paid rows only, settlements count live ones only.
Rejected / cancelled / reverted items are collected as `excluded` entries and shown separately.

Month attribution (stated on the page): maintenance uses its ledger month; expenses and settlements use the
ledger month they belong to (falling back to their date); corpus and project payments use the paid-on month.
Shares and pending balances are "as of today": they are not narrowed by the month range, only by the flat.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dates import format_date_or_blank, ist_today, to_ist
from app.models import (
    CancelRequest,
    CorpusCampaign,
    CorpusPayment,
    Expense,
    Flat,
    LedgerPeriod,
    MaintenancePayment,
    Project,
    ProjectContribution,
    ProjectPayment,
    Settlement,
    SubmissionRejection,
    User,
)
from app.services import balances as balances_svc
from app.services import corpus_campaigns as campaigns_svc
from app.services import ledger as ledger_svc
from app.services import projects as projects_svc
from app.services.activity import inr

PAGE_SIZE = 25
KINDS = {"summary": "Consolidated summary", "trend": "Monthly trend", "detail": "One category in detail"}
CATS = {
    "maintenance": "Maintenance",
    "corpus": "Corpus",
    "projects": "Projects",
    "expenses": "Expenses",
    "settlements": "Settlements",
}
MEASURES = {
    "maintenance": {"billed": "Billed", "collected": "Collected", "pending": "Pending"},
    "corpus": {"shares": "Goal shares", "collected": "Collected", "pending": "Pending"},
    "projects": {"shares": "Project shares", "collected": "Collected", "pending": "Pending"},
    "expenses": {"total": "Total", "settled": "Settled", "unsettled": "Unsettled", "owed": "Owed to owners",
                 "by_flat": "Paid by a flat", "by_account": "Paid from Account Balance"},
    "settlements": {"paidout": "Paid out", "paidout_nofund": "Paid out (no month)"},
}
AS_OF_TODAY = {("corpus", "shares"), ("corpus", "pending"), ("projects", "shares"), ("projects", "pending")}
REASONS = {"rejected": "Rejected", "cancelled": "Cancelled", "reverted": "Reverted"}
DEFS = {
    ("maintenance", "billed"): "Monthly maintenance due from the flats in these months, paid or not.",
    ("maintenance", "collected"): "Maintenance actually received (marked paid) in these months.",
    ("maintenance", "pending"): "Billed in these months but not yet paid.",
    ("corpus", "shares"): "What flats have been asked to give towards goals, as of today.",
    ("corpus", "collected"): "Corpus money received in these months.",
    ("corpus", "pending"): "Still to be received on goals that are open, as of today.",
    ("projects", "shares"): "The flats' shares of project costs, as of today.",
    ("projects", "collected"): "Project money received in these months.",
    ("projects", "pending"): "Still to be received on project shares, as of today.",
    ("expenses", "total"): "All approved building expenses in these months.",
    ("expenses", "settled"): "Expenses already paid out, either from the Account Balance or back to the owner who paid.",
    ("expenses", "unsettled"): "Expenses not yet paid out.",
    ("expenses", "owed"): "Part of the unsettled amount that owners paid from their own pocket and are still owed.",
    ("expenses", "by_flat"): "Expenses an owner paid first and recorded as an advance.",
    ("expenses", "by_account"): "Expenses paid directly from the Account Balance.",
    ("settlements", "paidout"): "Money that actually left the Account Balance for settled expenses.",
    ("settlements", "paidout_nofund"): "Settled, but the expense has no ledger month, so it is not in any balance.",
}
GLOSSARY = {
    "Account Balance": "The maintenance fund: opening balance + maintenance collected - settled expenses paid out.",
    "Billed": "Amount due from flats in the period, whether paid or not.",
    "Collected": "Amount received and marked paid.",
    "Pending": "Amount due but not yet paid.",
    "Settled": "An expense that has been paid out and recorded as settled.",
    "Owed to owners": "Unsettled expenses that an owner paid from their own pocket.",
    "Not counted": "Rejected, cancelled or reverted items. Never part of a total.",
}


def ym_of(d: date) -> int:
    return d.year * 12 + d.month - 1


def ym_label(ym: int) -> str:
    return date(ym // 12, ym % 12 + 1, 1).strftime("%b %Y")


def ym_value(ym: int) -> str:
    return f"{ym // 12:04d}-{ym % 12 + 1:02d}"


def parse_ym(raw: str | None) -> int | None:
    m = re.fullmatch(r"\s*(\d{4})-(\d{1,2})\s*", raw or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        return None
    return int(m.group(1)) * 12 + int(m.group(2)) - 1


def fy_label(fy: int) -> str:
    return f"FY {fy}-{str(fy + 1)[2:]} (Apr {fy} - Mar {fy + 1})"


def current_fy(today: date) -> int:
    return today.year if today.month >= 4 else today.year - 1


def _int(raw: str | None) -> int | None:
    try:
        return int((raw or "").strip())
    except ValueError:
        return None


@dataclass
class Params:
    kind: str = "summary"
    cat: str = "maintenance"
    scope: str = "all"
    fy: int | None = None
    ym_from: int | None = None
    ym_to: int | None = None
    flat: int | None = None
    include: bool = False
    cum: bool = False
    measure: str = ""
    sub: str = ""
    line: str = ""
    month: int | None = None
    dflat: int | None = None
    reason: str = ""
    page: int = 1
    errors: list[str] = field(default_factory=list)

    def bounds(self, today: date | None = None) -> tuple[int | None, int | None]:
        if self.scope == "fy":
            fy = self.fy if self.fy is not None else current_fy(today or ist_today())
            return fy * 12 + 3, (fy + 1) * 12 + 2
        if self.scope == "months":
            lo, hi = self.ym_from, self.ym_to
            if lo is not None and hi is not None and lo > hi:
                lo, hi = hi, lo
            return lo, hi
        return None, None

    def scope_query(self) -> dict[str, str]:
        out = {"kind": self.kind, "cat": self.cat, "scope": self.scope}
        if self.scope == "fy":
            out["fy"] = str(self.fy if self.fy is not None else current_fy(ist_today()))
        if self.scope == "months":
            if self.ym_from is not None:
                out["from"] = ym_value(self.ym_from)
            if self.ym_to is not None:
                out["to"] = ym_value(self.ym_to)
        if self.flat:
            out["flat"] = str(self.flat)
        if self.include:
            out["include"] = "1"
        if self.cum:
            out["cum"] = "1"
        return out

    def drill_query(self) -> dict[str, str]:
        out = {}
        for k, v in (("measure", self.measure), ("sub", self.sub), ("line", self.line), ("reason", self.reason)):
            if v:
                out[k] = v
        if self.month is not None:
            out["month"] = ym_value(self.month)
        if self.dflat:
            out["dflat"] = str(self.dflat)
        return out

    def url(self, path: str = "/admin/reports", *, drill: dict | None = None, **over) -> str:
        q = {**self.scope_query()}
        for k, v in over.items():
            if v in (None, ""):
                q.pop(k, None)
            else:
                q[k] = str(v)
        if drill is not None:
            q.update({k: str(v) for k, v in drill.items() if v not in (None, "", False)})
        return f"{path}?{urlencode(q)}"

    @property
    def is_drill(self) -> bool:
        return bool(self.measure)


def parse(params: Any) -> Params:
    g = lambda k: (params.get(k) or "").strip()  # noqa: E731
    p = Params()
    p.kind = g("kind") if g("kind") in KINDS else "summary"
    p.cat = g("cat") if g("cat") in CATS else "maintenance"
    p.scope = g("scope") if g("scope") in ("all", "fy", "months") else "all"
    p.fy = _int(g("fy"))
    p.ym_from, p.ym_to = parse_ym(g("from")), parse_ym(g("to"))
    p.flat = _int(g("flat"))
    p.include = g("include") in ("1", "on", "true", "yes")
    p.cum = g("cum") in ("1", "on", "true", "yes")
    p.measure = g("measure") if g("measure") in {m for ms in MEASURES.values() for m in ms} | {"excluded"} else ""
    p.sub, p.line = g("sub"), g("line")
    p.month = parse_ym(g("month"))
    p.dflat = _int(g("dflat"))
    p.reason = g("reason") if g("reason") in REASONS else ""
    p.page = max(_int(g("page")) or 1, 1)
    if p.scope == "months" and p.ym_from is None and p.ym_to is None:
        p.scope = "all"
    return p


# ---- entries -------------------------------------------------------------------------------------------

def _e(**kw) -> dict:
    base = {
        "key": "", "cat": "", "sub": None, "sub_label": "", "line": None, "line_label": "", "ym": None,
        "date": None, "flat_id": None, "flat": "", "owner": "", "type": "", "detail": "", "amts": {}, "mode": "",
        "ref": "", "vpa": "", "status": "", "counted": True, "reason": "", "by": "admin", "at": None, "note": "",
    }
    base.update(kw)
    return base


def _by(user: User | None) -> str:
    return f"owner {user.username}" if user is not None else "admin"


_RUPEES = re.compile(r"₹\s*([\d,]+)")


def collect(db: Session) -> list[dict]:
    flats = {f.id: f for f in db.scalars(select(Flat)).all()}
    users = {u.id: u for u in db.scalars(select(User)).all()}
    periods = {p.id: p for p in db.scalars(select(LedgerPeriod)).all()}
    campaigns = {c.id: c for c in db.scalars(select(CorpusCampaign)).all()}
    projects = {p.id: p for p in db.scalars(select(Project)).all()}
    live_projects = {i: p for i, p in projects.items() if p.deleted_at is None}
    out: list[dict] = []

    def who(fid: int | None) -> dict:
        f = flats.get(fid) if fid else None
        return {"flat_id": f.id if f else None, "flat": f.number if f else "", "owner": (f.contact_name or "") if f else ""}

    def pym(pid: int | None, fallback: date | None) -> int | None:
        p = periods.get(pid) if pid else None
        if p is not None:
            return p.year * 12 + p.month - 1
        return ym_of(fallback) if fallback else None

    def snap(row) -> dict:
        return {"vpa": getattr(row, "payee_vpa", None) or "", "mode": getattr(row, "payment_mode", None) or "",
                "ref": getattr(row, "payment_note", None) or ""}

    rejected_ids: set[tuple[str, int]] = set()
    for rj in db.scalars(select(SubmissionRejection)).all():
        if rj.record_id is not None:
            rejected_ids.add((rj.record_type, rj.record_id))
    cancel_ids: set[tuple[str, int]] = set()
    approved_cancels = [c for c in db.scalars(select(CancelRequest).where(CancelRequest.status == "approved")).all()
                        if c.record_type in ("maintenance_payment", "corpus_payment", "project_contribution")]
    for c in approved_cancels:
        cancel_ids.add((c.record_type, c.record_id))
    explained = rejected_ids | cancel_ids

    # maintenance: one row per flat per month
    for mp in db.scalars(select(MaintenancePayment)).all():
        per = periods.get(mp.period_id)
        if per is None:
            continue
        paid = bool(mp.is_paid)
        amts = {"billed": mp.amount, ("collected" if paid else "pending"): mp.amount}
        out.append(_e(
            key=f"m:{mp.id}", cat="maintenance", sub=str(per.id), sub_label=ym_label(per.year * 12 + per.month - 1),
            ym=per.year * 12 + per.month - 1, date=mp.paid_on if paid else None, **who(mp.flat_id),
            type="Maintenance", detail=f"Maintenance {ym_label(per.year * 12 + per.month - 1)}", amts=amts,
            status="Paid" if paid else "Pending", by=_by(users.get(mp.submitted_by_user_id)), **snap(mp)))
        if mp.reverted_at is not None and not paid and ("maintenance_payment", mp.id) not in explained:
            out.append(_e(
                key=f"m-rev:{mp.id}", cat="maintenance", ym=per.year * 12 + per.month - 1, date=None, **who(mp.flat_id),
                type="Maintenance", detail=f"Maintenance {ym_label(per.year * 12 + per.month - 1)} payment reverted (due again)",
                amts={"excluded": mp.amount}, counted=False, reason="reverted", status="Reverted", at=mp.reverted_at,
                by="admin", **snap(mp)))

    # corpus: payments, legacy unpaid rows, per-goal shares
    paid_by_campaign: dict[tuple[int, int], int] = {}
    for cp in db.scalars(select(CorpusPayment)).all():
        camp = campaigns.get(cp.campaign_id) if cp.campaign_id else None
        line = cp.line_item
        sub = str(camp.id) if camp else "0"
        sub_label = camp.name if camp else (cp.description or "Corpus (no goal)")
        common = dict(cat="corpus", sub=sub, sub_label=sub_label, line=str(line.id) if line else "",
                      line_label=line.title if line else "", **who(cp.flat_id), type="Corpus", detail=sub_label)
        if cp.is_paid:
            if camp:
                paid_by_campaign[(camp.id, cp.flat_id)] = paid_by_campaign.get((camp.id, cp.flat_id), 0) + cp.amount
            out.append(_e(key=f"c:{cp.id}", ym=pym(None, cp.paid_on), date=cp.paid_on, amts={"collected": cp.amount},
                          status="Paid", by=_by(users.get(cp.submitted_by_user_id)), **snap(cp), **common))
        elif cp.reverted_at is None:
            out.append(_e(key=f"c:{cp.id}", ym=None, amts={"pending": cp.amount}, status="Pending",
                          by=_by(users.get(cp.submitted_by_user_id)), **snap(cp), **common))
        elif ("corpus_payment", cp.id) not in explained:
            out.append(_e(key=f"c-rev:{cp.id}", ym=None, amts={"excluded": cp.amount}, counted=False, reason="reverted",
                          status="Reverted", at=cp.reverted_at, **snap(cp), **common))
    for camp in campaigns.values():
        for share in camp.shares:
            paid = paid_by_campaign.get((camp.id, share.flat_id), 0)
            remaining = max(share.share_amount - paid, 0) if camp.closed_at is None else 0
            due = f", due {format_date_or_blank(camp.due_date)}" if camp.due_date else ""
            amts = {"shares": share.share_amount}
            if remaining:
                amts["pending"] = remaining
            out.append(_e(
                key=f"cs:{camp.id}:{share.flat_id}", cat="corpus", sub=str(camp.id), sub_label=camp.name,
                **who(share.flat_id), type="Corpus share", detail=f"{camp.name}{due}", amts=amts,
                status="Closed goal" if camp.closed_at else ("Paid" if share.share_amount and not remaining else "Open"),
                by="—"))

    # projects: contributions and per-flat shares
    for pc in db.scalars(select(ProjectContribution)).all():
        proj = live_projects.get(pc.project_id)
        if proj is None:
            continue
        out.append(_e(
            key=f"pc:{pc.id}", cat="projects", sub=str(proj.id), sub_label=proj.name, ym=ym_of(pc.paid_on),
            date=pc.paid_on, **who(pc.flat_id), type="Project", detail=proj.name, amts={"collected": pc.amount},
            status="Paid", by=_by(users.get(pc.submitted_by_user_id)), at=pc.created_at, **snap(pc)))
    for pp in db.scalars(select(ProjectPayment)).all():
        proj = live_projects.get(pp.project_id)
        if proj is None:
            continue
        due = f", due {format_date_or_blank(proj.due_date)}" if proj.due_date else ""
        amts = {"shares": pp.amount}
        if pp.remaining:
            amts["pending"] = pp.remaining
        out.append(_e(
            key=f"ps:{pp.id}", cat="projects", sub=str(proj.id), sub_label=proj.name, **who(pp.flat_id),
            type="Project share", detail=f"{proj.name}{due}", amts=amts,
            status={"paid": "Paid", "part": "Part paid", "pending": "Pending"}[pp.status], by="—"))

    # expenses and settlements
    for ex in db.scalars(select(Expense)).all():
        sett = ex.settlement if ex.settlement is not None and ex.settlement.reverted_at is None else None
        ym = pym(ex.period_id, ex.expense_date)
        rejected = ex.status == "rejected"
        common = dict(cat="expenses", sub=ex.category or "general", sub_label=ex.category or "general", ym=ym,
                      date=ex.expense_date, **who(ex.paid_by_flat_id), type="Expense", detail=ex.description,
                      by=_by(users.get(ex.submitted_by_user_id)))
        if rejected or ex.deleted_at is not None:
            out.append(_e(key=f"e-x:{ex.id}", amts={"excluded": ex.amount}, counted=False,
                          reason="rejected" if rejected else "cancelled", status="Rejected" if rejected else "Cancelled",
                          at=ex.rejected_at if rejected else ex.deleted_at, note=ex.rejection_reason or "", **common))
            continue
        amts = {"total": ex.amount, ("by_flat" if ex.paid_by_flat_id else "by_account"): ex.amount}
        if ex.is_settled:
            amts["settled"] = ex.amount
        else:
            amts["unsettled"] = ex.amount
            if ex.paid_by_flat_id:
                amts["owed"] = ex.amount
        status = "Settled" if ex.is_settled else ("Unsettled, owed to owner" if ex.paid_by_flat_id else "Unsettled")
        out.append(_e(key=f"e:{ex.id}", amts=amts, status=status, note=(sett.note or "") if sett else "", **common))
        if ex.settlement is not None:
            st = ex.settlement
            scommon = dict(cat="settlements", sub=ex.category or "general", sub_label=ex.category or "general",
                           date=st.settled_on, **who(ex.paid_by_flat_id), type="Settlement", detail=ex.description,
                           note=st.note or "", by="admin")
            if st.reverted_at is None:
                key = "paidout" if ex.period_id else "paidout_nofund"
                out.append(_e(key=f"s:{st.id}", ym=ym, amts={key: st.amount}, status="Settled", **scommon))
            else:
                out.append(_e(key=f"s-rev:{st.id}", ym=ym, amts={"excluded": st.amount}, counted=False,
                              reason="reverted", status="Reverted", at=st.reverted_at, **scommon))

    # rejected / cancelled owner payments
    type_map = {"maintenance_payment": "maintenance", "corpus_payment": "corpus", "project_contribution": "projects",
                "maintenance": "maintenance", "corpus": "corpus", "project": "projects"}
    for rj in db.scalars(select(SubmissionRejection)).all():
        cat = type_map.get(rj.record_type)
        if cat is None:
            continue
        out.append(_e(
            key=f"rj:{rj.id}", cat=cat, ym=ym_of(rj.paid_on) if rj.paid_on else None, date=rj.paid_on,
            **who(rj.flat_id), type=CATS[cat], detail=rj.summary or CATS[cat], amts={"excluded": rj.amount or 0},
            counted=False, reason="rejected", status="Rejected", at=rj.rejected_at, note=rj.reason or "",
            by=f"{_by(users.get(rj.submitted_by_user_id))}; rejected by {rj.rejected_by or 'admin'}",
            vpa=rj.payee_vpa or ""))
    for c in approved_cancels:
        if (c.record_type, c.record_id) in rejected_ids:
            continue
        cat = type_map[c.record_type]
        m = _RUPEES.search(c.summary or "")
        out.append(_e(
            key=f"cr:{c.id}", cat=cat, ym=None, **who(c.flat_id), type=CATS[cat], detail=c.summary or CATS[cat],
            amts={"excluded": int(m.group(1).replace(",", "")) if m else 0}, counted=False, reason="cancelled",
            status="Cancelled", at=c.decided_at, note=c.reason or "", by=_by(users.get(c.requested_by_user_id))))
    return out


# ---- selection and totals ------------------------------------------------------------------------------

def pick(entries: list[dict], *, cat: str | None = None, measure: str | None = None, lo: int | None = None,
         hi: int | None = None, flat: int | None = None, sub: str | None = None, line: str | None = None,
         ym: int | None = None, counted: bool = True, reason: str = "") -> list[dict]:
    out = []
    for e in entries:
        if cat and e["cat"] != cat:
            continue
        if e["counted"] != counted:
            continue
        if counted and measure and not e["amts"].get(measure):
            continue
        if not counted and "excluded" not in e["amts"]:
            continue
        if reason and e["reason"] != reason:
            continue
        if flat and e["flat_id"] != flat:
            continue
        if sub not in (None, "") and e["sub"] != sub:
            continue
        if line not in (None, "") and e["line"] != line:
            continue
        if e["ym"] is not None:
            if lo is not None and e["ym"] < lo:
                continue
            if hi is not None and e["ym"] > hi:
                continue
            if ym is not None and e["ym"] != ym:
                continue
        elif ym is not None:
            continue
        out.append(e)
    return out


def tot(entries: list[dict], measure: str, **kw) -> int:
    return sum(e["amts"].get(measure, 0) for e in pick(entries, measure=measure, **kw))


def count(entries: list[dict], measure: str, **kw) -> int:
    return len(pick(entries, measure=measure, **kw))


# ---- context (scope, months) -------------------------------------------------------------------------------

@dataclass
class Ctx:
    p: Params
    entries: list[dict]
    lo: int | None
    hi: int | None
    periods: dict[int, LedgerPeriod]
    months: list[int]
    campaigns: list = field(default_factory=list)
    projects: list = field(default_factory=list)
    lines: dict = field(default_factory=dict)

    def kw(self, **over) -> dict:
        base = {"lo": self.lo, "hi": self.hi, "flat": self.p.flat}
        base.update(over)
        return base


def make_ctx(db: Session, p: Params, entries: list[dict] | None = None) -> Ctx:
    entries = collect(db) if entries is None else entries
    lo, hi = p.bounds()
    periods = {per.year * 12 + per.month - 1: per for per in db.scalars(select(LedgerPeriod)).all()}
    seen = set(periods) | {e["ym"] for e in entries if e["ym"] is not None and e["counted"]}
    months = sorted(m for m in seen if (lo is None or m >= lo) and (hi is None or m <= hi))
    camps = list(db.scalars(select(CorpusCampaign).order_by(CorpusCampaign.id)).all())
    return Ctx(
        p=p, entries=entries, lo=lo, hi=hi, periods=periods, months=months, campaigns=camps,
        projects=[x for x in db.scalars(select(Project).order_by(Project.id)).all() if x.deleted_at is None],
        lines={str(c.id): [(str(li.id), li.title, li.target_amount) for li in c.line_items] for c in camps},
    )


# ---- view model helpers --------------------------------------------------------------------------------------

def cell(v, *, money: bool = False, href: str | None = None, bold: bool = False, tip: str = "") -> dict:
    return {"v": v, "money": money, "href": href, "bold": bold, "tip": tip}


def _link(ctx: Ctx, cat: str, measure: str, **drill) -> str:
    return ctx.p.url("/admin/reports/drill", drill={"cat": cat, "measure": measure, **drill})


def num(ctx: Ctx, cat: str, measure: str, *, bold: bool = False, drill: dict | None = None, **kw) -> dict:
    drill = drill or {}
    pk = {"sub": drill.get("sub"), "line": drill.get("line"), "flat": drill.get("dflat")}
    if pk["flat"] is None:
        pk["flat"] = ctx.p.flat
    pk = {k: v for k, v in pk.items() if v not in (None, "")}
    base = {"lo": ctx.lo, "hi": ctx.hi}
    base.update(pk)
    base.update(kw)
    total = tot(ctx.entries, measure, cat=cat, **base)
    return cell(total, money=True, bold=bold, href=_link(ctx, cat, measure, **drill))


def tile(ctx: Ctx, cat: str, measure: str, label: str | None = None, **kw) -> dict:
    c = num(ctx, cat, measure, **kw)
    c.update({"label": label or MEASURES[cat][measure], "def": DEFS.get((cat, measure), "")})
    return c


def _table(header: list[str], rows: list[list[dict]], *, total: list[dict] | None = None, tips: list[str] | None = None) -> dict:
    return {"header": header, "rows": rows, "total": total, "tips": tips or [""] * len(header)}


def _subs(entries: list[dict], cat: str) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for e in entries:
        if e["cat"] == cat and e["sub"] is not None and e["counted"]:
            seen.setdefault(e["sub"], e["sub_label"])
    if cat == "maintenance":
        return []
    return sorted(seen.items(), key=lambda kv: (kv[1].lower(), kv[0]))


# ---- blocks for the consolidated summary and the category detail ---------------------------------------------

def block_maintenance(ctx: Ctx) -> dict:
    rows = []
    for ym in ctx.months:
        d = {"month": ym_value(ym)}
        rows.append([cell(ym_label(ym)), num(ctx, "maintenance", "billed", drill=d, lo=ym, hi=ym),
                     num(ctx, "maintenance", "collected", drill=d, lo=ym, hi=ym),
                     num(ctx, "maintenance", "pending", drill=d, lo=ym, hi=ym)])
    total = [cell("Total", bold=True), num(ctx, "maintenance", "billed", bold=True),
             num(ctx, "maintenance", "collected", bold=True), num(ctx, "maintenance", "pending", bold=True)]
    return {
        "id": "maintenance", "title": "Maintenance", "cat": "maintenance",
        "intro": "Monthly maintenance billed to every flat and what has been paid.",
        "tiles": [tile(ctx, "maintenance", m) for m in ("billed", "collected", "pending")],
        "tables": [_table(["Month", "Billed", "Collected", "Pending"], rows, total=total,
                          tips=["", DEFS[("maintenance", "billed")], DEFS[("maintenance", "collected")], DEFS[("maintenance", "pending")]])],
    }


def block_corpus(ctx: Ctx) -> dict:
    campaigns = {str(c.id): c for c in ctx.campaigns}
    rows = []
    for sub, label in _subs(ctx.entries, "corpus"):
        c = campaigns.get(sub)
        d = {"sub": sub}
        rows.append([cell(label + (" (closed)" if c is not None and c.closed_at else "")),
                     cell(c.target_amount if c is not None else "", money=c is not None),
                     num(ctx, "corpus", "shares", drill=d), num(ctx, "corpus", "collected", drill=d),
                     num(ctx, "corpus", "pending", drill=d)])
        for line in ctx.lines.get(sub, []):
            ld = {"sub": sub, "line": line[0]}
            rows.append([cell("   - " + line[1]), cell(line[2] if line[2] else "", money=bool(line[2])), cell(""),
                         num(ctx, "corpus", "collected", drill=ld), cell("")])
    targets = sum(c.target_amount for c in campaigns.values())
    total = [cell("Total", bold=True), cell(targets, money=True, bold=True), num(ctx, "corpus", "shares", bold=True),
             num(ctx, "corpus", "collected", bold=True), num(ctx, "corpus", "pending", bold=True)]
    return {
        "id": "corpus", "title": "Corpus", "cat": "corpus",
        "intro": "Goals the building is collecting for. Sub-goals (line items) are shown under their goal.",
        "tiles": [tile(ctx, "corpus", m) for m in ("collected", "pending")],
        "tables": [_table(["Goal / sub-goal", "Target", "Shares set", "Collected", "Pending"], rows, total=total,
                          tips=["", "The goal's target amount as entered by the admin.", DEFS[("corpus", "shares")],
                                DEFS[("corpus", "collected")], DEFS[("corpus", "pending")]])],
    }


def block_projects(ctx: Ctx) -> dict:
    projs = {str(p.id): p for p in ctx.projects}
    rows = []
    for sub, label in _subs(ctx.entries, "projects"):
        pr = projs.get(sub)
        d = {"sub": sub}
        rows.append([cell(label), cell(pr.total_cost if pr else "", money=bool(pr)),
                     cell(pr.share_per_flat if pr else "", money=bool(pr)), num(ctx, "projects", "shares", drill=d),
                     num(ctx, "projects", "collected", drill=d), num(ctx, "projects", "pending", drill=d)])
    budget = sum(p.total_cost for p in projs.values())
    total = [cell("Grand total", bold=True), cell(budget, money=True, bold=True), cell(""),
             num(ctx, "projects", "shares", bold=True), num(ctx, "projects", "collected", bold=True),
             num(ctx, "projects", "pending", bold=True)]
    return {
        "id": "projects", "title": "Projects", "cat": "projects",
        "intro": "Each project's budget, the flats' shares, and what has come in. There are no separate sub-projects; "
                 "open the per-flat view of a project for its breakdown.",
        "tiles": [tile(ctx, "projects", m) for m in ("collected", "pending")],
        "tables": [_table(["Project", "Budget", "Share per flat", "Shares set", "Collected", "Pending"], rows, total=total,
                          tips=["", "Total cost entered for the project.", "Amount asked from each flat.",
                                DEFS[("projects", "shares")], DEFS[("projects", "collected")], DEFS[("projects", "pending")]])],
    }


def block_expenses(ctx: Ctx) -> dict:
    rows = []
    for sub, label in _subs(ctx.entries, "expenses"):
        d = {"sub": sub}
        rows.append([cell(label), num(ctx, "expenses", "total", drill=d), num(ctx, "expenses", "settled", drill=d),
                     num(ctx, "expenses", "unsettled", drill=d), num(ctx, "expenses", "by_flat", drill=d),
                     num(ctx, "expenses", "by_account", drill=d)])
    total = [cell("Total", bold=True)] + [num(ctx, "expenses", m, bold=True)
                                          for m in ("total", "settled", "unsettled", "by_flat", "by_account")]
    return {
        "id": "expenses", "title": "Expenses", "cat": "expenses",
        "intro": "Approved building expenses. Rejected and cancelled expenses are never counted.",
        "tiles": [tile(ctx, "expenses", m) for m in ("total", "settled", "unsettled", "owed")],
        "tables": [_table(["Category", "Total", "Settled", "Unsettled", "Paid by a flat", "Paid from Account Balance"], rows,
                          total=total, tips=["", DEFS[("expenses", "total")], DEFS[("expenses", "settled")],
                                             DEFS[("expenses", "unsettled")], DEFS[("expenses", "by_flat")],
                                             DEFS[("expenses", "by_account")]])],
    }


def block_settlements(ctx: Ctx) -> dict:
    tiles = [tile(ctx, "settlements", "paidout")]
    nofund = tot(ctx.entries, "paidout_nofund", cat="settlements", **ctx.kw())
    if nofund:
        tiles.append(tile(ctx, "settlements", "paidout_nofund"))
    return {
        "id": "settlements", "title": "Settlements", "cat": "settlements",
        "intro": "Expenses that have been paid out. Each settlement's note is shown in the drilldown.",
        "tiles": tiles, "tables": [],
    }


def block_excluded(ctx: Ctx) -> dict:
    rows = []
    for cat in CATS:
        for reason, rl in REASONS.items():
            hits = pick(ctx.entries, cat=cat, counted=False, reason=reason, **ctx.kw())
            if not hits:
                continue
            amt = sum(e["amts"]["excluded"] for e in hits)
            rows.append([cell(CATS[cat]), cell(rl), cell(len(hits)),
                         cell(amt, money=True, href=_link(ctx, cat, "excluded", reason=reason))])
    return {
        "id": "excluded", "title": "Not counted (shown separately)", "cat": "",
        "intro": "Rejected, cancelled and reverted items. They are never part of any total above.",
        "tiles": [], "tables": [_table(["Area", "Why", "Items", "Amount"], rows)],
    }


# ---- reconciliation -------------------------------------------------------------------------------------------

def reconcile(db: Session, ctx: Ctx) -> dict:
    """Ties the report to the code the dashboard and Balances page use. Always whole-society."""
    E = ctx.entries
    checks: list[dict] = []

    def check(label: str, expected: int, actual: int, hint: str) -> None:
        checks.append({"label": label, "expected": expected, "actual": actual, "ok": expected == actual, "hint": hint})

    months = []
    ordered = sorted(ctx.periods)
    for ym in ctx.months:
        per = ctx.periods.get(ym)
        if per is None:
            continue
        summ = ledger_svc.period_fund_summary(db, per)
        collected = tot(E, "collected", cat="maintenance", lo=ym, hi=ym)
        paid = tot(E, "paidout", cat="settlements", lo=ym, hi=ym)
        months.append({
            "ym": ym, "label": ym_label(ym), "opening": per.opening_fund_balance, "collected": collected,
            "paid_out": paid, "closing": summ["closing"], "closed": bool(per.is_closed),
            "ok": per.opening_fund_balance + collected - paid == summ["closing"],
            "collected_ok": collected == summ["collections"], "paid_ok": paid == summ["expenses"],
        })
    for m in months:
        check(f"{m['label']}: opening + collected - paid out = closing", m["opening"] + m["collected"] - m["paid_out"],
              m["closing"], "The month's closing balance does not equal its opening plus collections minus payouts.")
    chain_prev = None
    for ym in ordered:
        per = ctx.periods[ym]
        if chain_prev is not None and ym == chain_prev[0] + 1 and ym in ctx.months:
            check(f"{ym_label(ym)}: opening = {ym_label(chain_prev[0])} closing", chain_prev[1], per.opening_fund_balance,
                  "A month's opening balance differs from the previous month's closing balance.")
        summ = ledger_svc.period_fund_summary(db, per)
        chain_prev = (ym, summ["closing"])

    held = ledger_svc.compute_fund_balance(db)
    if ordered:
        last = ctx.periods[ordered[-1]]
        lsum = ledger_svc.period_fund_summary(db, last)
        derived = lsum["closing"] if last.is_closed and last.closing_fund_balance is not None else (
            last.opening_fund_balance + tot(E, "collected", cat="maintenance", lo=ordered[-1], hi=ordered[-1])
            - tot(E, "paidout", cat="settlements", lo=ordered[-1], hi=ordered[-1]))
        check("Account Balance now (from the entries) = dashboard / Balances figure", derived, held,
              "The balance rebuilt from individual entries differs from the balance the dashboard shows.")
    bal = balances_svc.overview(db)
    check("Owed to owners = Balances page 'owed'", tot(E, "owed", cat="expenses"), bal["owed"],
          "Unsettled owner advances differ from the Balances page.")
    check("All expenses = ledger total", tot(E, "total", cat="expenses"), ledger_svc.expense_total(db),
          "Expense total differs from the ledger's counted expenses.")
    check("Settled expenses = ledger settled total", tot(E, "settled", cat="expenses"),
          ledger_svc.expense_total(db, settled_only=True), "Settled expenses differ from the ledger.")
    check("Settled expenses = amounts paid out", tot(E, "settled", cat="expenses"),
          tot(E, "paidout", cat="settlements") + tot(E, "paidout_nofund", cat="settlements"),
          "A settled expense and its settlement amount differ (an expense may have been edited after it was settled).")
    check("Paid out = ledger settlements", tot(E, "paidout", cat="settlements"), ledger_svc.settlements_from_fund(db),
          "Money paid out differs from what the Account Balance deducts.")
    check("Maintenance collected = ledger", tot(E, "collected", cat="maintenance"), ledger_svc.maintenance_collected(db),
          "Maintenance collected differs from the ledger.")

    flats = db.scalars(select(Flat)).all()
    fb = [ledger_svc.flat_balances(db, f) for f in flats]
    check("Maintenance pending = sum of flat balances", tot(E, "pending", cat="maintenance"),
          sum(x["owed_maintenance"] for x in fb), "Pending maintenance differs from the flats' statements.")
    check("Corpus pending = sum of flat balances", tot(E, "pending", cat="corpus"),
          sum(x["owed_corpus"] for x in fb), "Pending corpus differs from the flats' statements.")
    check("Projects pending = sum of flat balances", tot(E, "pending", cat="projects"),
          sum(x["owed_projects"] for x in fb), "Pending project shares differ from the flats' statements.")
    campaigns = db.scalars(select(CorpusCampaign)).all()
    check("Corpus collected = goals' collected", tot(E, "collected", cat="corpus") - sum(
        e["amts"]["collected"] for e in E if e["cat"] == "corpus" and e["counted"] and e["sub"] == "0" and "collected" in e["amts"]),
          sum(campaigns_svc.campaign_summary(db, c)["paid_total"] for c in campaigns),
          "Corpus collected differs from the goals' own totals.")
    live = [pr for pr in db.scalars(select(Project)).all() if pr.deleted_at is None]
    check("Projects collected = projects' collected", tot(E, "collected", cat="projects"),
          sum(projects_svc.totals(list(pr.payments))["collected"] for pr in live),
          "Project collections differ from the projects' own totals.")
    nofund = tot(E, "paidout_nofund", cat="settlements")
    warnings = []
    if nofund:
        warnings.append(f"{inr(nofund)} of settled expenses has no ledger month, so it is not in any balance.")
    bad = [c for c in checks if not c["ok"]]
    first = next((m for m in months), None)
    last_m = months[-1] if months else None
    return {
        "ok": not bad and not warnings, "checks": checks, "bad": bad, "warnings": warnings, "months": months,
        "balance": held, "owed": bal["owed"], "effective": bal["effective"],
        "opening": first["opening"] if first else 0,
        "collected": sum(m["collected"] for m in months), "paid_out": sum(m["paid_out"] for m in months),
        "closing": last_m["closing"] if last_m else None,
        "statement_ok": bool(months) and all(m["ok"] for m in months),
        "corpus_held": tot(E, "collected", cat="corpus"), "projects_held": tot(E, "collected", cat="projects"),
    }


# ---- top-level builders ----------------------------------------------------------------------------------------

def summary_blocks(ctx: Ctx) -> list[dict]:
    blocks = [block_maintenance(ctx), block_corpus(ctx), block_projects(ctx), block_expenses(ctx), block_settlements(ctx)]
    if ctx.p.include:
        blocks.append(block_excluded(ctx))
    return blocks


def trend(db: Session, ctx: Ctx) -> dict:
    E = ctx.entries
    flow_cols = [("m_billed", "Maintenance billed"), ("m_collected", "Maintenance collected"), ("m_pending", "Maintenance pending"),
                 ("corpus", "Corpus collected"), ("projects", "Projects collected"), ("expenses", "Expenses"),
                 ("paid_out", "Paid out")]
    spec = {
        "m_billed": ("maintenance", "billed"), "m_collected": ("maintenance", "collected"),
        "m_pending": ("maintenance", "pending"), "corpus": ("corpus", "collected"),
        "projects": ("projects", "collected"), "expenses": ("expenses", "total"), "paid_out": ("settlements", "paidout"),
    }
    rows, running = [], {k: 0 for k, _ in flow_cols}
    for ym in ctx.months:
        per = ctx.periods.get(ym)
        row = {"ym": ym, "label": ym_label(ym), "opening": per.opening_fund_balance if per else None,
               "closing": ledger_svc.period_fund_summary(db, per)["closing"] if per else None, "vals": {}, "links": {}}
        for k, (cat, meas) in spec.items():
            v = tot(E, meas, cat=cat, lo=ym, hi=ym, flat=ctx.p.flat)
            running[k] += v
            row["vals"][k] = running[k] if ctx.p.cum else v
            row["links"][k] = _link(ctx, cat, meas, month=ym_value(ym))
        rows.append(row)
    totals = {k: (rows[-1]["vals"][k] if rows else 0) if ctx.p.cum else sum(r["vals"][k] for r in rows) for k, _ in flow_cols}
    return {"cols": flow_cols, "rows": rows, "totals": totals, "spec": spec,
            "total_links": {k: _link(ctx, cat, meas) for k, (cat, meas) in spec.items()}}


def chart_svg(tr: dict) -> str:
    """Grouped bars (collected from everyone vs expenses) plus a closing-balance line, as inline SVG."""
    rows = tr["rows"]
    if not rows:
        return ""
    w, h, pad_l, pad_b, pad_t = 640, 240, 52, 34, 16
    inner_w, inner_h = w - pad_l - 10, h - pad_b - pad_t
    bars = [(r["vals"]["m_collected"] + r["vals"]["corpus"] + r["vals"]["projects"], r["vals"]["expenses"]) for r in rows]
    closings = [r["closing"] for r in rows]
    top = max([1] + [v for pair in bars for v in pair] + [c for c in closings if c is not None])
    low = min([0] + [c for c in closings if c is not None])
    span = top - low or 1

    def y(v: float) -> float:
        return pad_t + inner_h - (v - low) / span * inner_h

    n = len(rows)
    slot = inner_w / n
    bw = max(min(slot * 0.32, 26), 3)
    parts = [f'<svg class="rpt-chart" viewBox="0 0 {w} {h}" role="img" aria-label="Collected, expenses and Account Balance by month">']
    for i in range(5):
        v = low + span * i / 4
        parts.append(f'<line x1="{pad_l}" y1="{y(v):.1f}" x2="{w - 10}" y2="{y(v):.1f}" class="rpt-grid"/>'
                     f'<text x="{pad_l - 6}" y="{y(v) + 3:.1f}" text-anchor="end" class="rpt-axis">{inr(round(v))}</text>')
    pts = []
    for i, (r, (inc, exp)) in enumerate(zip(rows, bars)):
        cx = pad_l + slot * i + slot / 2
        parts.append(f'<rect x="{cx - bw - 1:.1f}" y="{y(inc):.1f}" width="{bw:.1f}" height="{max(y(low) - y(inc), 0):.1f}" class="rpt-bar-in"><title>{r["label"]}: collected {inr(inc)}</title></rect>')
        parts.append(f'<rect x="{cx + 1:.1f}" y="{y(exp):.1f}" width="{bw:.1f}" height="{max(y(low) - y(exp), 0):.1f}" class="rpt-bar-out"><title>{r["label"]}: expenses {inr(exp)}</title></rect>')
        if n <= 14 or i % 2 == 0:
            parts.append(f'<text x="{cx:.1f}" y="{h - 12}" text-anchor="middle" class="rpt-axis">{r["label"]}</text>')
        if closings[i] is not None:
            pts.append((cx, y(closings[i]), r["label"], closings[i]))
    if pts:
        parts.append('<polyline class="rpt-line" fill="none" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b, _, _ in pts) + '"/>')
        for a, b, lab, val in pts:
            parts.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="3.5" class="rpt-dot"><title>{lab}: Account Balance {inr(val)}</title></circle>')
    parts.append("</svg>")
    return "".join(parts)


def _detail_tables(db: Session, ctx: Ctx) -> list[dict]:
    """By month, by flat and (where there is one) by goal / project / category for the chosen category."""
    cat = ctx.p.cat
    measures = [m for m in MEASURES[cat] if m != "paidout_nofund"]
    flats = {f.id: f for f in db.scalars(select(Flat).order_by(Flat.number)).all()}

    def hdr(first: str) -> list[str]:
        return [first] + [MEASURES[cat][m] for m in measures]

    def tips() -> list[str]:
        return [""] + [DEFS.get((cat, m), "") for m in measures]

    tables = []
    subs = _subs(ctx.entries, cat)
    if subs:
        title = {"corpus": "By goal", "projects": "By project", "expenses": "By category", "settlements": "By expense category"}[cat]
        rows = []
        for sub, label in subs:
            rows.append([cell(label)] + [num(ctx, cat, m, drill={"sub": sub}) for m in measures])
            if cat == "corpus":
                for lid, ltitle, _target in ctx.lines.get(sub, []):
                    rows.append([cell("   - " + ltitle)] + [
                        num(ctx, cat, m, drill={"sub": sub, "line": lid}) if m == "collected" else cell("") for m in measures])
        tables.append({"title": title, **_table(hdr(title.replace("By ", "").title()), rows,
                       total=[cell("Total", bold=True)] + [num(ctx, cat, m, bold=True) for m in measures], tips=tips())})
    month_rows = [[cell(ym_label(ym))] + [
        cell("") if (cat, m) in AS_OF_TODAY else num(ctx, cat, m, drill={"month": ym_value(ym)}, lo=ym, hi=ym)
        for m in measures] for ym in ctx.months]
    tables.append({"title": "By month", **_table(hdr("Month"), month_rows,
                   total=[cell("Total", bold=True)] + [num(ctx, cat, m, bold=True) for m in measures], tips=tips())})
    fids = sorted({e["flat_id"] for e in ctx.entries if e["cat"] == cat and e["counted"] and e["flat_id"]},
                  key=lambda i: flats[i].number if i in flats else "")
    if ctx.p.flat:
        fids = [f for f in fids if f == ctx.p.flat]
    flat_rows = [[cell(f"Flat {flats[i].number}" + (f" ({flats[i].contact_name})" if flats[i].contact_name else ""))]
                 + [num(ctx, cat, m, drill={"dflat": i}, flat=i) for m in measures] for i in fids if i in flats]
    if flat_rows:
        tables.append({"title": "By flat", **_table(hdr("Flat"), flat_rows,
                       total=[cell("Total", bold=True)] + [num(ctx, cat, m, bold=True) for m in measures], tips=tips())})
    return tables


# ---- drilldown ------------------------------------------------------------------------------------------------

DRILL_COLUMNS = ("Date", "Flat", "Owner", "Type", "Detail", "Amount", "Mode", "UTR / reference", "Payee UPI ID",
                 "Status", "Recorded by", "Recorded at (IST)", "Note")


def drill(ctx: Ctx) -> dict:
    p = ctx.p
    cat, measure = p.cat, p.measure
    if measure == "excluded":
        rows = pick(ctx.entries, cat=cat, counted=False, reason=p.reason, lo=ctx.lo, hi=ctx.hi,
                    flat=p.dflat or p.flat, sub=p.sub or None, line=p.line or None, ym=p.month)
        counted_rows, excluded_rows = [], rows
        total = sum(e["amts"]["excluded"] for e in rows)
    else:
        kw = dict(lo=ctx.lo, hi=ctx.hi, flat=p.dflat or p.flat, sub=p.sub or None, line=p.line or None, ym=p.month)
        counted_rows = pick(ctx.entries, cat=cat, measure=measure, **kw)
        total = sum(e["amts"][measure] for e in counted_rows)
        excluded_rows = pick(ctx.entries, cat=cat, counted=False, **kw) if p.include else []
    key = lambda e: (e["date"] or date.min, e["key"])  # noqa: E731
    counted_rows = sorted(counted_rows, key=key, reverse=True)
    excluded_rows = sorted(excluded_rows, key=key, reverse=True)
    allrows = counted_rows + (excluded_rows if measure != "excluded" else [])
    if measure == "excluded":
        allrows = excluded_rows
    pages = max((len(allrows) + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(p.page, pages)
    shown = allrows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    label = MEASURES.get(cat, {}).get(measure, "Not counted") if measure != "excluded" else "Not counted" + (
        f" ({REASONS[p.reason]})" if p.reason else "")
    bits = [CATS[cat], label]
    if p.sub:
        sl = next((e["sub_label"] for e in ctx.entries if e["cat"] == cat and e["sub"] == p.sub), p.sub)
        bits.append(sl)
    if p.line:
        ll = next((e["line_label"] for e in ctx.entries if e["cat"] == cat and e["line"] == p.line), p.line)
        bits.append(ll)
    if p.month is not None:
        bits.append(ym_label(p.month))
    return {"title": " - ".join(bits), "rows": shown, "all": allrows, "total": total, "count": len(counted_rows),
            "page": page, "pages": pages, "page_total": sum((e["amts"].get(measure, 0) if e["counted"] else 0) for e in shown),
            "excluded_count": len(excluded_rows) if measure != "excluded" else len(allrows), "measure": measure,
            "def": DEFS.get((cat, measure), "Rejected, cancelled or reverted items; never part of a total.")}


def row_amount(e: dict, measure: str) -> int:
    if not e["counted"]:
        return e["amts"].get("excluded", 0)
    return e["amts"].get(measure, 0)


def drill_row_values(e: dict, measure: str) -> list:
    return [format_date_or_blank(e["date"]), e["flat"], e["owner"], e["type"] + (f" - {e['sub_label']}" if e["sub_label"] and e["cat"] in ("corpus", "projects", "expenses", "settlements") else ""),
            e["detail"], row_amount(e, measure), e["mode"], e["ref"], e["vpa"],
            e["status"] if e["counted"] else f"Not counted ({e['status']})", e["by"],
            _ist_stamp(e["at"]), e["note"]]


def _ist_stamp(v) -> str:
    if v is None:
        return ""
    return f"{to_ist(v):%d-%m-%Y %H:%M} IST"


def build(db: Session, p: Params) -> dict:
    ctx = make_ctx(db, p)
    out: dict = {"ctx": ctx, "p": p, "lo": ctx.lo, "hi": ctx.hi, "months": ctx.months}
    if p.is_drill:
        out["drill"] = drill(ctx)
        return out
    out["recon"] = reconcile(db, ctx) if p.kind == "summary" else None
    if p.kind == "summary":
        out["blocks"] = summary_blocks(ctx)
    elif p.kind == "trend":
        out["trend"] = trend(db, ctx)
        out["chart"] = chart_svg(out["trend"])
    else:
        out["detail_title"] = CATS[p.cat]
        out["detail_tiles"] = [tile(ctx, p.cat, m) for m in MEASURES[p.cat] if m != "paidout_nofund"]
        out["detail_tables"] = _detail_tables(db, ctx)
    return out


def scope_text(ctx: Ctx) -> str:
    p = ctx.p
    if p.scope == "fy":
        return fy_label(p.fy if p.fy is not None else current_fy(ist_today()))
    if p.scope == "months":
        lo, hi = ctx.lo, ctx.hi
        if lo is not None and hi is not None:
            return f"{ym_label(lo)} to {ym_label(hi)}"
        return f"from {ym_label(lo)}" if lo is not None else f"up to {ym_label(hi)}"
    return "All time"


def filter_lines(db: Session, p: Params, ctx: Ctx) -> list[tuple[str, str]]:
    flat = db.get(Flat, p.flat) if p.flat else None
    out = [("Report", KINDS[p.kind] + (f" - {CATS[p.cat]}" if p.kind == "detail" else "")),
           ("Period", scope_text(ctx)),
           ("Flat", f"Flat {flat.number}" if flat else "All flats"),
           ("Rejected / cancelled / reverted", "Shown separately (never in totals)" if p.include else "Not shown")]
    if p.kind == "trend":
        out.append(("Running totals", "Yes" if p.cum else "No"))
    return out


# ---- export tables ----------------------------------------------------------------------------------------------

def _plain(c: dict):
    return c["v"]


def block_export_tables(blocks: list[dict]) -> list[dict]:
    tables = []
    tiles = [["Section", "Figure", "Amount (Rs.)", "What it means"]]
    for b in blocks:
        for t in b["tiles"]:
            tiles.append([b["title"], t["label"], t["v"], t["def"]])
    if len(tiles) > 1:
        tables.append({"title": "Key figures", "header": tiles[0], "rows": tiles[1:], "total": None})
    for b in blocks:
        for t in b["tables"]:
            tables.append({"title": b["title"], "header": t["header"],
                           "rows": [[_plain(c) for c in r] for r in t["rows"]],
                           "total": [_plain(c) for c in t["total"]] if t["total"] else None})
    return tables


def reconcile_export_table(rc: dict) -> dict:
    rows = [[c["label"], c["expected"], c["actual"], "OK" if c["ok"] else "DOES NOT TIE"] for c in rc["checks"]]
    return {"title": "Reconciliation", "header": ["Check", "Report figure (Rs.)", "App figure (Rs.)", "Result"], "rows": rows, "total": None}


def trend_export_table(tr: dict) -> dict:
    header = ["Month"] + [lbl for _, lbl in tr["cols"]] + ["Opening balance", "Closing balance"]
    rows = [[r["label"]] + [r["vals"][k] for k, _ in tr["cols"]] + [r["opening"] if r["opening"] is not None else "",
                                                                    r["closing"] if r["closing"] is not None else ""]
            for r in tr["rows"]]
    first, last = (tr["rows"][0], tr["rows"][-1]) if tr["rows"] else (None, None)
    total = ["Total"] + [tr["totals"][k] for k, _ in tr["cols"]] + [
        first["opening"] if first and first["opening"] is not None else "", last["closing"] if last and last["closing"] is not None else ""]
    return {"title": "Monthly trend", "header": header, "rows": rows, "total": total}


def drill_export_table(d: dict) -> dict:
    rows = [drill_row_values(e, d["measure"]) for e in d["all"]]
    total = ["Total", "", "", "", "", d["total"], "", "", "", "", "", "", ""]
    return {"title": d["title"], "header": list(DRILL_COLUMNS), "rows": rows, "total": total}


def details_table(ctx: Ctx, cats: list[str] | None = None) -> dict:
    """Every counted entry in the scope (what Details holds on the Excel sheet when not exporting a drilldown)."""
    rows = []
    cats = cats or list(CATS)
    for e in sorted((x for x in ctx.entries if x["cat"] in cats), key=lambda e: (e["cat"], e["date"] or date.min, e["key"])):
        if not e["counted"] and not ctx.p.include:
            continue
        if e["counted"]:
            measure = next((m for m in ("collected", "total", "paidout", "paidout_nofund") if e["amts"].get(m)), None)
            if measure is None:
                continue
        else:
            measure = "excluded"
        if not pick([e], cat=e["cat"], measure=measure if e["counted"] else None, counted=e["counted"], **ctx.kw()):
            continue
        rows.append([CATS[e["cat"]]] + drill_row_values(e, measure))
    return {"title": "Details", "header": ["Area"] + list(DRILL_COLUMNS), "rows": rows, "total": None}


def export_package(db: Session, p: Params) -> dict:
    """Everything an export needs, built from the same data as the screen."""
    data = build(db, p)
    ctx = data["ctx"]
    tables: list[dict] = []
    if p.is_drill:
        d = data["drill"]
        tables.append(drill_export_table(d))
        title = d["title"]
    elif p.kind == "summary":
        tables += block_export_tables(data["blocks"])
        if data["recon"] is not None:
            tables.append(reconcile_export_table(data["recon"]))
        title = KINDS["summary"]
    elif p.kind == "trend":
        tables.append(trend_export_table(data["trend"]))
        title = KINDS["trend"]
    else:
        for t in data["detail_tables"]:
            tables.append({"title": t["title"], "header": t["header"], "rows": [[_plain(c) for c in r] for r in t["rows"]],
                           "total": [_plain(c) for c in t["total"]] if t["total"] else None})
        title = f"{KINDS['detail']} - {CATS[p.cat]}"
    return {"title": title, "tables": tables, "data": data, "ctx": ctx, "params": filter_lines(db, p, ctx)}


def workbook_package(db: Session, p: Params) -> dict:
    """The four Excel sheets: Summary, Trend, Details (the open drilldown, or every entry in scope)."""
    entries = collect(db)
    sp = replace(p, kind="summary", measure="", page=1)
    sctx = make_ctx(db, sp, entries)
    summary = block_export_tables(summary_blocks(sctx)) + [reconcile_export_table(reconcile(db, sctx))]
    tp = replace(p, kind="trend", measure="", page=1)
    tctx = make_ctx(db, tp, entries)
    trend_tbl = trend_export_table(trend(db, tctx))
    ctx = make_ctx(db, p, entries)
    if p.is_drill:
        details = drill_export_table(drill(ctx))
    else:
        details = details_table(ctx, [p.cat] if p.kind == "detail" else None)
    return {"summary": summary, "trend": trend_tbl, "details": details, "params": filter_lines(db, p, ctx),
            "ctx": ctx}
