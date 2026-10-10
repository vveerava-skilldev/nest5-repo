from datetime import date
from itertools import chain

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.services import corpus_campaigns as campaigns_svc
from app.services import maintenance_fee as maintenance_fee_svc
from app.models import (
    CorpusPayment,
    Expense,
    Flat,
    FundType,
    LedgerPeriod,
    MaintenancePayment,
    Project,
    ProjectPayment,
    Settlement,
)


def get_or_create_period(db: Session, year: int, month: int) -> LedgerPeriod:
    period = db.scalar(
        select(LedgerPeriod).where(LedgerPeriod.year == year, LedgerPeriod.month == month)
    )
    if period:
        return period
    period = LedgerPeriod(year=year, month=month, opening_fund_balance=0, corpus_opening=0)
    db.add(period)
    db.flush()
    ensure_maintenance_rows(db, period)
    return period


def ensure_maintenance_rows(db: Session, period: LedgerPeriod) -> None:
    flats = db.scalars(select(Flat).order_by(Flat.number)).all()
    existing = {
        mp.flat_id
        for mp in db.scalars(
            select(MaintenancePayment).where(MaintenancePayment.period_id == period.id)
        ).all()
    }
    for flat in flats:
        if flat.id not in existing:
            db.add(
                MaintenancePayment(
                    flat_id=flat.id,
                    period_id=period.id,
                    amount=maintenance_fee_svc.fee_for_period(db, period),
                )
            )
    db.flush()


def maintenance_collected(db: Session, period_id: int | None = None) -> int:
    q = select(func.coalesce(func.sum(MaintenancePayment.amount), 0)).where(
        MaintenancePayment.is_paid.is_(True)
    )
    if period_id:
        q = q.where(MaintenancePayment.period_id == period_id)
    return int(db.scalar(q) or 0)


def expense_total(db: Session, period_id: int | None = None, settled_only: bool = False) -> int:
    q = select(func.coalesce(func.sum(Expense.amount), 0)).where(Expense.deleted_at.is_(None))
    if period_id:
        q = q.where(Expense.period_id == period_id)
    if settled_only:
        q = q.where(Expense.is_settled.is_(True))
    return int(db.scalar(q) or 0)


def settlements_from_fund(db: Session, period_id: int | None = None) -> int:
    q = (
        select(func.coalesce(func.sum(Settlement.amount), 0))
        .join(Expense, Settlement.expense_id == Expense.id)
        .where(Settlement.reverted_at.is_(None), Expense.deleted_at.is_(None))
    )
    if period_id:
        q = q.where(Expense.period_id == period_id)
    return int(db.scalar(q) or 0)


def compute_fund_balance(db: Session, up_to_period: LedgerPeriod | None = None) -> int:
    """Maintenance fund = opening + collections - settlements (maintenance fund only)."""
    periods = db.scalars(select(LedgerPeriod).order_by(LedgerPeriod.year, LedgerPeriod.month)).all()
    if not periods:
        return 0
    balance = 0
    for p in periods:
        balance = p.opening_fund_balance
        balance += maintenance_collected(db, p.id)
        balance -= settlements_from_fund(db, p.id)
        if up_to_period and p.id == up_to_period.id:
            break
        if p.is_closed and p.closing_fund_balance is not None:
            balance = p.closing_fund_balance
    return balance


def available_for_settlement(db: Session) -> int:
    return compute_fund_balance(db)


def pending_maintenance(db: Session, period_id: int | None = None) -> list[MaintenancePayment]:
    q = select(MaintenancePayment).where(MaintenancePayment.is_paid.is_(False))
    if period_id:
        q = q.where(MaintenancePayment.period_id == period_id)
    return list(db.scalars(q).all())


def pending_project_payments(db: Session) -> list[ProjectPayment]:
    return list(
        db.scalars(
            select(ProjectPayment)
            .join(Project)
            .where(ProjectPayment.is_paid.is_(False), Project.deleted_at.is_(None))
        ).all()
    )


def pending_corpus(db: Session, period_id: int | None = None) -> list[CorpusPayment]:
    q = select(CorpusPayment).where(CorpusPayment.is_paid.is_(False))
    if period_id:
        q = q.where(CorpusPayment.period_id == period_id)
    return list(db.scalars(q).all())


def period_fund_summary(db: Session, period: LedgerPeriod) -> dict:
    collections = maintenance_collected(db, period.id)
    expenses = settlements_from_fund(db, period.id)
    computed_closing = period.opening_fund_balance + collections - expenses
    closing = (
        period.closing_fund_balance
        if period.is_closed and period.closing_fund_balance is not None
        else computed_closing
    )
    return {
        "opening": period.opening_fund_balance,
        "collections": collections,
        "expenses": expenses,
        "closing": closing,
        "computed_closing": computed_closing,
    }


def unsettled_expenses(db: Session) -> list[Expense]:
    return list(
        db.scalars(
            select(Expense).where(
                Expense.is_settled.is_(False),
                Expense.deleted_at.is_(None),
                Expense.paid_by_flat_id.isnot(None),
            )
        ).all()
    )


def flat_balances(db: Session, flat: Flat) -> dict:
    """Per-flat ledger: advances awaiting reimbursement vs amounts owed."""
    advances = int(
        db.scalar(
            select(func.coalesce(func.sum(Expense.amount), 0)).where(
                Expense.paid_by_flat_id == flat.id,
                Expense.is_settled.is_(False),
                Expense.deleted_at.is_(None),
            )
        )
        or 0
    )
    owed_maintenance = int(
        db.scalar(
            select(func.coalesce(func.sum(MaintenancePayment.amount), 0)).where(
                MaintenancePayment.flat_id == flat.id,
                MaintenancePayment.is_paid.is_(False),
            )
        )
        or 0
    )
    owed_projects = int(
        db.scalar(
            select(func.coalesce(func.sum(ProjectPayment.amount - func.coalesce(ProjectPayment.paid_amount, 0)), 0))
            .join(Project)
            .where(
                ProjectPayment.flat_id == flat.id,
                ProjectPayment.is_paid.is_(False),
                Project.deleted_at.is_(None),
            )
        )
        or 0
    )
    owed_corpus = int(
        db.scalar(
            select(func.coalesce(func.sum(CorpusPayment.amount), 0)).where(
                CorpusPayment.flat_id == flat.id,
                CorpusPayment.is_paid.is_(False),
            )
        )
        or 0
    )
    owed_corpus += campaigns_svc.remaining_total_for_flat(db, flat.id)
    return {
        "flat": flat,
        "owed_maintenance": owed_maintenance,
        "owed_projects": owed_projects,
        "owed_corpus": owed_corpus,
        "total_owed": owed_maintenance + owed_projects + owed_corpus,
        "reimbursement_due": advances,
        "net": advances - (owed_maintenance + owed_projects + owed_corpus),
    }


def settlement_status_by_flat(db: Session) -> list[dict]:
    flats = db.scalars(select(Flat).order_by(Flat.number)).all()
    rows = []
    for flat in flats:
        unsettled = list(
            db.scalars(
                select(Expense).where(
                    Expense.paid_by_flat_id == flat.id,
                    Expense.deleted_at.is_(None),
                )
            ).all()
        )
        pending_amount = sum(e.amount for e in unsettled if not e.is_settled)
        settled_amount = sum(e.amount for e in unsettled if e.is_settled)
        rows.append(
            {
                "flat": flat,
                "pending_amount": pending_amount,
                "settled_amount": settled_amount,
                "expenses": unsettled,
            }
        )
    return rows


def close_period(db: Session, period: LedgerPeriod) -> int:
    balance = period.opening_fund_balance
    balance += maintenance_collected(db, period.id)
    balance -= settlements_from_fund(db, period.id)
    period.closing_fund_balance = balance
    period.is_closed = True
    next_month = period.month + 1
    next_year = period.year
    if next_month > 12:
        next_month = 1
        next_year += 1
    nxt = get_or_create_period(db, next_year, next_month)
    if nxt.opening_fund_balance == 0 and not nxt.is_closed:
        nxt.opening_fund_balance = balance
    return balance


def refresh_closed_periods(db: Session) -> list[LedgerPeriod]:
    """Recompute closed months' closing balances (and the carried-forward opening of the month after).

    A closed month keeps a stored closing balance that the next month's opening copies. When a
    payment, expense or settlement in a closed month changes later (cancel approved, revert, late
    entry), the stored figures must follow or the Account Balance keeps the old amount. An opening
    that was not carried forward (differs from the old closing) is left alone.
    """
    periods = db.scalars(select(LedgerPeriod).order_by(LedgerPeriod.year, LedgerPeriod.month)).all()
    changed = []
    for i, p in enumerate(periods):
        if not p.is_closed:
            continue
        new_closing = p.opening_fund_balance + maintenance_collected(db, p.id) - settlements_from_fund(db, p.id)
        if p.closing_fund_balance == new_closing:
            continue
        old_closing = p.closing_fund_balance
        p.closing_fund_balance = new_closing
        changed.append(p)
        nxt = periods[i + 1] if i + 1 < len(periods) else None
        if nxt is not None and (old_closing is None or nxt.opening_fund_balance == old_closing):
            nxt.opening_fund_balance = new_closing
            changed.append(nxt)
    return changed


_BALANCE_MODELS = (MaintenancePayment, Expense, Settlement)


def _touches_balance(session: Session) -> bool:
    return any(isinstance(o, _BALANCE_MODELS) for o in chain(session.new, session.dirty, session.deleted))


@event.listens_for(Session, "after_flush")
def _mark_balance_dirty(session: Session, _flush_context) -> None:
    if _touches_balance(session):
        session.info["balances_dirty"] = True


@event.listens_for(Session, "before_commit")
def _refresh_balances_on_commit(session: Session) -> None:
    if _touches_balance(session):
        session.flush()
    if session.info.pop("balances_dirty", False):
        refresh_closed_periods(session)


def month_label(period: LedgerPeriod) -> str:
    return date(period.year, period.month, 1).strftime("%B %Y")


def _status_cell(*, has_items: bool, pending_amount: int, display_amount: int) -> dict:
    if not has_items:
        return {
            "has_items": False,
            "is_paid": True,
            "amount": 0,
            "pending_amount": 0,
        }
    is_paid = pending_amount == 0
    return {
        "has_items": True,
        "is_paid": is_paid,
        "amount": display_amount,
        "pending_amount": pending_amount,
    }


def build_dashboard_status(db: Session, period: LedgerPeriod | None) -> dict:
    """Per-flat payment status for dashboard: maintenance (period), corpus (all open collections), projects (active)."""
    flats = list(db.scalars(select(Flat).order_by(Flat.number)).all())
    projects = list(
        db.scalars(
            select(Project).where(Project.deleted_at.is_(None)).order_by(Project.name, Project.id)
        ).all()
    )

    maintenance_by_flat: dict[int, MaintenancePayment] = {}
    if period:
        ensure_maintenance_rows(db, period)
        for mp in db.scalars(
            select(MaintenancePayment).where(MaintenancePayment.period_id == period.id)
        ).all():
            maintenance_by_flat[mp.flat_id] = mp

    project_pay_index: dict[tuple[int, int], ProjectPayment] = {}
    for pp in db.scalars(
        select(ProjectPayment)
        .join(Project)
        .where(Project.deleted_at.is_(None))
    ).all():
        project_pay_index[(pp.flat_id, pp.project_id)] = pp

    project_totals: dict[int, int] = {p.id: 0 for p in projects}
    campaign_summaries = [
        campaigns_svc.campaign_summary(db, c) for c in campaigns_svc.active_campaigns(db)
    ]
    campaign_totals: dict[int, int] = {c["campaign"].id: 0 for c in campaign_summaries}
    rows: list[dict] = []
    total_maintenance_pending = 0
    total_corpus_pending = 0
    grand_pending = 0

    for flat in flats:
        mp = maintenance_by_flat.get(flat.id)
        if mp:
            maint_cell = _status_cell(
                has_items=True,
                pending_amount=0 if mp.is_paid else mp.amount,
                display_amount=mp.amount,
            )
        elif period:
            maint_cell = _status_cell(has_items=False, pending_amount=0, display_amount=0)
        else:
            maint_cell = _status_cell(has_items=False, pending_amount=0, display_amount=0)

        project_cells: list[dict] = []
        row_project_pending = 0
        for proj in projects:
            pp = project_pay_index.get((flat.id, proj.id))
            if pp:
                pending = pp.remaining
                project_cells.append(
                    {
                        "project_id": proj.id,
                        "project_name": proj.name,
                        "has_items": True,
                        "is_paid": pp.status == "paid",
                        "amount": pp.amount,
                        "paid": pp.paid,
                        "status": pp.status,
                        "pending_amount": pending,
                    }
                )
                row_project_pending += pending
                if pending:
                    project_totals[proj.id] = project_totals.get(proj.id, 0) + pending
            else:
                project_cells.append(
                    {
                        "project_id": proj.id,
                        "project_name": proj.name,
                        "has_items": False,
                        "is_paid": True,
                        "amount": 0,
                        "pending_amount": 0,
                    }
                )

        campaign_cells: list[dict] = []
        row_campaign_pending = 0
        for summary in campaign_summaries:
            crow = next((r for r in summary["rows"] if r["flat"].id == flat.id), None)
            if crow:
                campaign_cells.append({"has_items": True, **crow})
                row_campaign_pending += crow["remaining"]
                campaign_totals[summary["campaign"].id] += crow["remaining"]
            else:
                campaign_cells.append({"has_items": False, "remaining": 0})

        # One corpus figure per flat: every open collection (General corpus included), counted once.
        flat_campaign_rows = [c for c in campaign_cells if c["has_items"]]
        corpus_share = sum(c["share"] for c in flat_campaign_rows)
        corpus_paid = sum(c["paid"] for c in flat_campaign_rows)
        statuses = {c["status"] for c in flat_campaign_rows}
        corpus_cell = {
            **_status_cell(
                has_items=bool(corpus_share or corpus_paid),
                pending_amount=row_campaign_pending,
                display_amount=corpus_share,
            ),
            "paid": corpus_paid,
            "status": next((st for st in ("overdue", "due_soon", "open") if st in statuses), "paid"),
        }

        row_pending = maint_cell["pending_amount"] + corpus_cell["pending_amount"] + row_project_pending
        total_maintenance_pending += maint_cell["pending_amount"]
        total_corpus_pending += corpus_cell["pending_amount"]
        grand_pending += row_pending

        rows.append(
            {
                "flat": flat,
                "maintenance": maint_cell,
                "corpus": corpus_cell,
                "projects": project_cells,
                "campaigns": campaign_cells,
                "row_pending": row_pending,
            }
        )

    return {
        "projects": projects,
        "campaigns": [c["campaign"] for c in campaign_summaries],
        "rows": rows,
        "totals": {
            "maintenance_pending": total_maintenance_pending,
            "corpus_pending": total_corpus_pending,
            "projects_pending": project_totals,
            "campaigns_pending": campaign_totals,
            "grand_pending": grand_pending,
        },
    }
