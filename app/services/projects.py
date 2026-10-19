"""Project shares and the payments (full or part) made towards them."""
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Flat, Project, ProjectContribution, ProjectPayment
from app.services import payment_details as payment_details_svc


class ProjectPaymentError(ValueError):
    pass


def sync(db: Session, pp: ProjectPayment) -> None:
    """Recompute a share's paid amount / paid flag / last paid date from its contributions."""
    db.flush()
    rows = db.scalars(select(ProjectContribution).where(ProjectContribution.project_payment_id == pp.id)).all()
    pp.paid_amount = sum(c.amount for c in rows)
    pp.is_paid = bool(pp.amount) and pp.paid_amount >= pp.amount
    pp.paid_on = max((c.paid_on for c in rows), default=None)
    if not rows:
        pp.payment_note = None


def backfill(db: Session) -> int:
    """Shares that predate contributions: a paid share becomes one contribution of its amount."""
    fixed = 0
    for pp in db.scalars(select(ProjectPayment).where(ProjectPayment.paid_amount.is_(None))).all():
        if pp.is_paid:
            legacy = ProjectContribution(project_payment_id=pp.id, project_id=pp.project_id, flat_id=pp.flat_id,
                                         amount=pp.amount, paid_on=pp.paid_on or date.today(),
                                         payment_note=pp.payment_note)
            payment_details_svc.stamp_initial(db, legacy)
            db.add(legacy)
            pp.paid_amount = pp.amount
        else:
            pp.paid_amount = 0
        fixed += 1
    return fixed


def record(db: Session, pp: ProjectPayment, amount: int, paid_on: date, *, payment_mode: str | None = None,
           note: str | None = None, user_id: int | None = None) -> ProjectContribution:
    if pp.project is None or pp.project.deleted_at is not None:
        raise ProjectPaymentError("That project no longer exists.")
    if pp.remaining <= 0:
        raise ProjectPaymentError(f"{pp.project.name} is already fully paid for Flat {pp.flat.number}.")
    if amount <= 0:
        raise ProjectPaymentError("Enter a valid amount.")
    if amount > pp.remaining:
        raise ProjectPaymentError(f"Only ₹{pp.remaining} is left on this share.")
    row = ProjectContribution(project_payment_id=pp.id, project_id=pp.project_id, flat_id=pp.flat_id,
                              amount=amount, paid_on=paid_on, payment_mode=payment_mode,
                              payment_note=note or None, submitted_by_user_id=user_id)
    payment_details_svc.stamp_payee(db, row)
    db.add(row)
    pp.reverted_at = None
    if note:
        pp.payment_note = note
    sync(db, pp)
    return row


def mark_paid(db: Session, pp: ProjectPayment, paid_on: date, note: str | None = None) -> ProjectContribution | None:
    """Admin "Mark paid": records whatever is left on the share."""
    if pp.remaining <= 0:
        return None
    return record(db, pp, pp.remaining, paid_on, note=note)


def revert(db: Session, pp: ProjectPayment) -> None:
    """Admin "Revert": removes every payment on the share."""
    for c in db.scalars(select(ProjectContribution).where(ProjectContribution.project_payment_id == pp.id)).all():
        db.delete(c)
    pp.reverted_at = datetime.utcnow()
    sync(db, pp)


def remove_contribution(db: Session, row: ProjectContribution) -> None:
    pp = row.share
    db.delete(row)
    sync(db, pp)


def totals(payments: list[ProjectPayment]) -> dict:
    share_total = sum(pp.amount for pp in payments)
    collected = sum(min(pp.paid, pp.amount) for pp in payments)
    return {
        "share_total": share_total,
        "collected": collected,
        "pending": share_total - collected,
        "paid_flats": sum(1 for pp in payments if pp.status == "paid"),
        "part_flats": sum(1 for pp in payments if pp.status == "part"),
        "flats": len(payments),
        "progress_pct": int(collected * 100 / share_total) if share_total else 0,
    }


def shares_for_flats(db: Session, flat_ids: list[int]) -> list[ProjectPayment]:
    return list(db.scalars(
        select(ProjectPayment)
        .join(Project)
        .join(Flat, ProjectPayment.flat_id == Flat.id)
        .where(Project.deleted_at.is_(None), ProjectPayment.flat_id.in_(flat_ids))
        .options(selectinload(ProjectPayment.flat), selectinload(ProjectPayment.project),
                 selectinload(ProjectPayment.contributions))
        .order_by(Project.id.desc(), Flat.number)
    ).all())


def contributions_for_flats(db: Session, flat_ids: list[int]) -> list[ProjectContribution]:
    return list(db.scalars(
        select(ProjectContribution)
        .join(Project, ProjectContribution.project_id == Project.id)
        .where(Project.deleted_at.is_(None), ProjectContribution.flat_id.in_(flat_ids))
        .options(selectinload(ProjectContribution.project), selectinload(ProjectContribution.flat))
        .order_by(ProjectContribution.paid_on.desc(), ProjectContribution.id.desc())
    ).all())


def has_payments(db: Session, project: Project) -> int:
    return db.scalar(select(func.count(ProjectContribution.id)).where(ProjectContribution.project_id == project.id)) or 0


def is_overdue(pp: ProjectPayment, today: date | None = None) -> bool:
    today = today or date.today()
    due = pp.project.due_date if pp.project else None
    return bool(due and due < today and pp.remaining > 0)
