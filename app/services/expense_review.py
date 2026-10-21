"""Admin review of owner-submitted common expenses: reject, reinstate, purge, and the data repair.

An owner expense counts as soon as it is recorded (v3 design). A *rejected* expense stays in the
table as a record (status 'rejected', reason, who/when) but is excluded everywhere that counts
money: see `Expense.counted()`. Rejecting is refused once the expense is settled (revert the
settlement first), the same rule as approving a cancel request.
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CancelRequest, Expense, Settlement
from app.services import audit as audit_svc

REJECTED = "rejected"
APPROVED = "approved"
MAX_REASON = 255


class ExpenseReviewError(ValueError):
    pass


def _check_reviewable(exp: Expense | None) -> Expense:
    if exp is None or exp.deleted_at is not None:
        raise ExpenseReviewError("That expense no longer exists.")
    return exp


def reject(db: Session, exp: Expense | None, *, admin_label: str, reason: str = "") -> Expense:
    exp = _check_reviewable(exp)
    if exp.is_rejected:
        raise ExpenseReviewError("This expense is already rejected.")
    if exp.submitted_by_user_id is None:
        raise ExpenseReviewError("Only expenses recorded by owners can be rejected; edit or delete this one instead.")
    if exp.is_settled:
        raise ExpenseReviewError("This expense was already settled — revert the settlement first.")
    before = audit_svc.snapshot(exp)
    exp.status = REJECTED
    exp.rejection_reason = reason.strip()[:MAX_REASON] or None
    exp.rejected_at = datetime.utcnow()
    exp.rejected_by = admin_label
    # A pending "request cancel" on it is moot now.
    for req in db.scalars(
        select(CancelRequest).where(
            CancelRequest.record_type == "expense", CancelRequest.record_id == exp.id, CancelRequest.status == "pending"
        )
    ).all():
        req.status = "approved"
        req.decided_at = datetime.utcnow()
        req.decided_by = admin_label
        req.decision_note = "Expense was rejected by the admin."
    db.flush()
    audit_svc.log_audit(db, entity_type="expense", entity_id=exp.id, action="reject", before=before, after=exp)
    return exp


def reinstate(db: Session, exp: Expense | None) -> Expense:
    exp = _check_reviewable(exp)
    if not exp.is_rejected:
        raise ExpenseReviewError("This expense is not rejected.")
    before = audit_svc.snapshot(exp)
    exp.status = APPROVED
    exp.rejection_reason = None
    exp.rejected_at = None
    exp.rejected_by = None
    db.flush()
    audit_svc.log_audit(db, entity_type="expense", entity_id=exp.id, action="reinstate", before=before, after=exp)
    return exp


def rejected_expenses(db: Session) -> list[Expense]:
    return list(
        db.scalars(
            select(Expense).where(Expense.deleted_at.is_(None), Expense.status == REJECTED).order_by(Expense.id)
        ).all()
    )


def purge(db: Session, exp: Expense) -> None:
    """Remove a rejected expense row for good (the audit log keeps what it looked like)."""
    if not exp.is_rejected:
        raise ExpenseReviewError("Only rejected expenses can be deleted this way.")
    before = audit_svc.snapshot(exp)
    for st in db.scalars(select(Settlement).where(Settlement.expense_id == exp.id)).all():
        db.delete(st)
    exp_id = exp.id
    db.delete(exp)
    db.flush()
    audit_svc.log_audit(db, entity_type="expense", entity_id=exp_id, action="purge", before=before)


def purge_all_rejected(db: Session) -> int:
    rows = rejected_expenses(db)
    for exp in rows:
        purge(db, exp)
    if rows:
        audit_svc.log_audit(
            db, entity_type="expense", entity_id=None, action="clear_rejected",
            after={"purged": len(rows), "ids": [e.id for e in rows]},
        )
    return len(rows)


def repair_rejected_expenses(db: Session) -> int:
    """Idempotent: make sure no rejected expense still counts (settled flag / live settlement).

    Rows are never deleted; only their flags are corrected. Safe to run on every start.
    """
    fixed = 0
    now = datetime.utcnow()
    for exp in db.scalars(select(Expense).where(Expense.status == REJECTED)).all():
        changed = False
        if exp.is_settled:
            exp.is_settled = False
            changed = True
        st = exp.settlement
        if st is not None and st.reverted_at is None:
            st.reverted_at = now
            changed = True
        fixed += changed
    if fixed:
        db.flush()
    return fixed
