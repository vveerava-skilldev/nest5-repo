"""Corpus collections: per-flat shares, partial payments, due dates."""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    CorpusCampaign,
    CorpusCampaignShare,
    CorpusCategory,
    CorpusLineItem,
    CorpusPayment,
    Flat,
    LedgerPeriod,
)

DUE_SOON_DAYS = 7


def equal_split(total: int, n: int) -> list[int]:
    """Split total into n integer shares that add up exactly (remainder to the first flats)."""
    if n <= 0:
        return []
    base, extra = divmod(total, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def period_for_date(db: Session, d: date) -> LedgerPeriod | None:
    return db.scalar(select(LedgerPeriod).where(LedgerPeriod.year == d.year, LedgerPeriod.month == d.month))


def active_campaigns(db: Session) -> list[CorpusCampaign]:
    return list(
        db.scalars(
            select(CorpusCampaign)
            .where(CorpusCampaign.closed_at.is_(None))
            .options(selectinload(CorpusCampaign.shares))
            .order_by(CorpusCampaign.due_date.is_(None), CorpusCampaign.due_date, CorpusCampaign.id)
        ).all()
    )


def paid_by_flat(db: Session, campaign_id: int) -> dict[int, int]:
    rows = db.execute(
        select(CorpusPayment.flat_id, func.coalesce(func.sum(CorpusPayment.amount), 0))
        .where(CorpusPayment.campaign_id == campaign_id, CorpusPayment.is_paid.is_(True))
        .group_by(CorpusPayment.flat_id)
    ).all()
    return {flat_id: int(total) for flat_id, total in rows}


def due_status(due: date | None, remaining: int, today: date | None = None) -> str:
    """'paid' | 'overdue' | 'due_soon' | 'open'."""
    if remaining <= 0:
        return "paid"
    if due is None:
        return "open"
    today = today or date.today()
    if due < today:
        return "overdue"
    if (due - today).days <= DUE_SOON_DAYS:
        return "due_soon"
    return "open"


def campaign_summary(db: Session, campaign: CorpusCampaign, today: date | None = None) -> dict:
    paid = paid_by_flat(db, campaign.id)
    flats = {f.id: f for f in db.scalars(select(Flat)).all()}
    rows = []
    for share in sorted(campaign.shares, key=lambda s: flats[s.flat_id].number):
        p = paid.get(share.flat_id, 0)
        remaining = max(share.share_amount - p, 0)
        rows.append(
            {
                "flat": flats[share.flat_id],
                "share": share.share_amount,
                "paid": p,
                "remaining": remaining,
                "status": due_status(campaign.due_date, remaining, today),
            }
        )
    share_total = sum(r["share"] for r in rows)
    paid_total = sum(r["paid"] for r in rows)
    return {
        "campaign": campaign,
        "rows": rows,
        "share_total": share_total,
        "paid_total": paid_total,
        "remaining_total": sum(r["remaining"] for r in rows),
        "target_gap": campaign.target_amount - share_total,
        "progress_pct": int(paid_total * 100 / share_total) if share_total else 0,
    }


def flat_share(db: Session, campaign_id: int, flat_id: int) -> CorpusCampaignShare | None:
    return db.scalar(
        select(CorpusCampaignShare).where(
            CorpusCampaignShare.campaign_id == campaign_id, CorpusCampaignShare.flat_id == flat_id
        )
    )


def remaining_for(db: Session, campaign: CorpusCampaign, flat_id: int) -> int:
    share = flat_share(db, campaign.id, flat_id)
    if not share:
        return 0
    return max(share.share_amount - paid_by_flat(db, campaign.id).get(flat_id, 0), 0)


def dues_for_flats(db: Session, flat_ids: list[int] | None = None, today: date | None = None) -> list[dict]:
    """Open campaign balances per flat (remaining > 0), soonest due first."""
    out = []
    for campaign in active_campaigns(db):
        for row in campaign_summary(db, campaign, today)["rows"]:
            if row["remaining"] <= 0:
                continue
            if flat_ids is not None and row["flat"].id not in flat_ids:
                continue
            out.append({**row, "campaign": campaign, "due_date": campaign.due_date})
    return out


def remaining_total_for_flat(db: Session, flat_id: int) -> int:
    return sum(d["remaining"] for d in dues_for_flats(db, [flat_id]))


# ---- General corpus + one consistent corpus picture --------------------------------

GENERAL_NAME = "General corpus"
GENERAL_CATEGORY = "General"
DEFAULT_CATEGORIES = ("General", "Building repair", "Lift", "Painting")
GENERAL_DESCRIPTION = "Open-ended corpus contributions (one-off amounts added per flat)."


def general_campaign(db: Session, *, create: bool = False, created_by: str = "system") -> CorpusCampaign | None:
    campaign = db.scalar(
        select(CorpusCampaign).where(CorpusCampaign.is_general.is_(True)).order_by(CorpusCampaign.id).limit(1)
    )
    if campaign or not create:
        return campaign
    campaign = CorpusCampaign(
        name=GENERAL_NAME, description=GENERAL_DESCRIPTION, target_amount=0, is_general=True,
        created_by=created_by, category=GENERAL_CATEGORY,
    )
    db.add(campaign)
    db.flush()
    for flat in db.scalars(select(Flat)).all():
        db.add(CorpusCampaignShare(campaign_id=campaign.id, flat_id=flat.id, share_amount=0))
    db.flush()
    return campaign


def _sync_general_target(db: Session, campaign: CorpusCampaign) -> None:
    db.flush()
    campaign.target_amount = int(
        db.scalar(
            select(func.coalesce(func.sum(CorpusCampaignShare.share_amount), 0)).where(
                CorpusCampaignShare.campaign_id == campaign.id
            )
        )
        or 0
    )


def add_to_general_share(db: Session, flat_id: int, amount: int, *, created_by: str = "admin") -> CorpusCampaign:
    """Raise a flat's General corpus share (what used to be 'add corpus line item')."""
    campaign = general_campaign(db, create=True, created_by=created_by)
    share = flat_share(db, campaign.id, flat_id)
    if share is None:
        share = CorpusCampaignShare(campaign_id=campaign.id, flat_id=flat_id, share_amount=0)
        db.add(share)
    share.share_amount = (share.share_amount or 0) + amount
    _sync_general_target(db, campaign)
    return campaign


def absorb_line_items(db: Session) -> int:
    """Fold any corpus rows without a collection into General corpus (same rule as the migration).

    Runs on startup and after a JSON restore so an old dump can never bring back a second,
    disconnected corpus view.
    """
    rows = db.scalars(select(CorpusPayment).where(CorpusPayment.campaign_id.is_(None))).all()
    if not rows:
        return 0
    campaign = general_campaign(db, create=True, created_by="system")
    for row in rows:
        share = flat_share(db, campaign.id, row.flat_id)
        if share is None:
            share = CorpusCampaignShare(campaign_id=campaign.id, flat_id=row.flat_id, share_amount=0)
            db.add(share)
            db.flush()
        share.share_amount += row.amount
        if row.is_paid:
            row.campaign_id = campaign.id
        else:
            db.delete(row)
    _sync_general_target(db, campaign)
    return len(rows)


def open_campaigns_for_flats(db: Session, flat_ids: list[int], today: date | None = None) -> list[dict]:
    """Every open collection × flat with that flat's share, paid, remaining and due status."""
    out = []
    for campaign in active_campaigns(db):
        for row in campaign_summary(db, campaign, today)["rows"]:
            if row["flat"].id in flat_ids and (row["share"] or row["paid"]):
                out.append({**row, "campaign": campaign, "due_date": campaign.due_date})
    return out


def flat_corpus_totals(db: Session, flat_id: int, today: date | None = None) -> dict:
    """One flat across all open collections (dashboard cell, flat page)."""
    rows = open_campaigns_for_flats(db, [flat_id], today)
    statuses = {r["status"] for r in rows}
    return {
        "rows": rows,
        "share": sum(r["share"] for r in rows),
        "paid": sum(r["paid"] for r in rows),
        "remaining": sum(r["remaining"] for r in rows),
        "status": next((s for s in ("overdue", "due_soon", "open") if s in statuses), "paid"),
    }


def building_totals(db: Session, today: date | None = None) -> dict:
    """All collections (open and closed): target, collected, still pending on open ones."""
    campaigns = db.scalars(select(CorpusCampaign).options(selectinload(CorpusCampaign.shares))).all()
    summaries = [campaign_summary(db, c, today) for c in campaigns]
    open_summaries = [s for s in summaries if s["campaign"].closed_at is None]
    target = sum(s["campaign"].target_amount for s in summaries)
    collected = sum(s["paid_total"] for s in summaries)
    return {
        "target": target,
        "share_total": sum(s["share_total"] for s in summaries),
        "collected": collected,
        "pending": sum(s["remaining_total"] for s in open_summaries),
        "open_count": len(open_summaries),
        "progress_pct": int(collected * 100 / target) if target else 0,
    }


def payments_for_flats(db: Session, flat_ids: list[int] | None = None, limit: int = 300) -> list[CorpusPayment]:
    q = (
        select(CorpusPayment)
        .where(CorpusPayment.is_paid.is_(True))
        .options(
            selectinload(CorpusPayment.flat),
            selectinload(CorpusPayment.campaign),
            selectinload(CorpusPayment.submitted_by),
        )
        .order_by(CorpusPayment.paid_on.desc(), CorpusPayment.id.desc())
        .limit(limit)
    )
    if flat_ids is not None:
        q = q.where(CorpusPayment.flat_id.in_(flat_ids))
    return list(db.scalars(q).all())


# ---- Goals: categories and line items ---------------------------------------------------


def categories(db: Session) -> list[str]:
    """Built-in categories first, then the ones the admin added (alphabetical)."""
    added = [n for n in db.scalars(select(CorpusCategory.name).order_by(CorpusCategory.name)).all()
             if n not in DEFAULT_CATEGORIES]
    return list(DEFAULT_CATEGORIES) + added


def add_category(db: Session, name: str) -> str | None:
    name = " ".join(name.split())[:100]
    if not name:
        return None
    existing = {n.lower(): n for n in categories(db)}
    if name.lower() in existing:
        return existing[name.lower()]
    db.add(CorpusCategory(name=name))
    db.flush()
    return name


def goal_category(campaign: CorpusCampaign) -> str:
    return campaign.category or GENERAL_CATEGORY


def parse_line_items(text: str) -> list[tuple[str, int | None]]:
    """One line item per line: "Title" or "Title: 5000" (amount optional)."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        title, amount = line, None
        if ":" in line:
            head, _, tail = line.rpartition(":")
            digits = tail.strip().replace(",", "").replace("₹", "")
            if head.strip() and digits.isdigit():
                title, amount = head.strip(), int(digits)
        out.append((title[:255], amount))
    return out


def line_item_summary(db: Session, campaign: CorpusCampaign) -> dict:
    """Per line item: target, paid, progress; plus what was paid to the goal without a line item."""
    paid = dict(
        db.execute(
            select(CorpusPayment.line_item_id, func.coalesce(func.sum(CorpusPayment.amount), 0))
            .where(CorpusPayment.campaign_id == campaign.id, CorpusPayment.is_paid.is_(True))
            .group_by(CorpusPayment.line_item_id)
        ).all()
    )
    rows = []
    for item in campaign.line_items:
        p = int(paid.get(item.id, 0))
        target = item.target_amount or 0
        rows.append({
            "item": item,
            "target": item.target_amount,
            "paid": p,
            "remaining": max(target - p, 0) if item.target_amount else None,
            "progress_pct": min(int(p * 100 / target), 100) if target else None,
        })
    return {"rows": rows, "unassigned": int(paid.get(None, 0))}


def line_item_for(db: Session, campaign: CorpusCampaign, line_item_id) -> CorpusLineItem | None:
    try:
        lid = int(line_item_id)
    except (TypeError, ValueError):
        return None
    item = db.get(CorpusLineItem, lid)
    return item if item is not None and item.campaign_id == campaign.id else None


def delete_goal(db: Session, campaign: CorpusCampaign, *, admin_label: str = "admin", backup=None) -> dict:
    """Remove a goal with its payments, shares and line items (admin action, audited).

    Pending owner cancel requests for its payments are closed. Corpus never enters the Account
    Balance, but closed-month rollups are refreshed exactly like after any other deletion.
    """
    from datetime import datetime

    from app.models import CancelRequest
    from app.services import audit as audit_svc
    from app.services import ledger as ledger_svc

    payments = list(campaign.payments)
    amount = sum(p.amount for p in payments)
    before = {
        "name": campaign.name, "is_general": bool(campaign.is_general), "target_amount": campaign.target_amount,
        "category": campaign.category, "due_date": campaign.due_date,
        "shares": {s.flat.number: s.share_amount for s in campaign.shares},
        "line_items": [i.title for i in campaign.line_items],
        "payments": [{"id": p.id, "flat": p.flat.number, "amount": p.amount, "paid_on": p.paid_on} for p in payments],
        "backup": backup.name if backup else None,
    }
    ids = [p.id for p in payments]
    if ids:
        for req in db.scalars(select(CancelRequest).where(
                CancelRequest.record_type == "corpus_payment", CancelRequest.record_id.in_(ids),
                CancelRequest.status == "pending")).all():
            req.status = "approved"
            req.decided_at = datetime.utcnow()
            req.decided_by = admin_label
            req.decision_note = "Corpus goal deleted by admin"
    for p in payments:
        db.delete(p)
    db.flush()
    campaign_id = campaign.id
    db.delete(campaign)  # shares and line items cascade
    db.flush()
    ledger_svc.refresh_closed_periods(db)
    audit_svc.log_audit(db, entity_type="corpus_campaign", entity_id=campaign_id, action="delete", before=before)
    return {"payments": len(payments), "amount": amount}
