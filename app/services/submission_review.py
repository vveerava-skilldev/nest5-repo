"""Admin review of owner submissions: the 'to review' list, Mark reviewed, and Reject for owner payments.

A submission is one `owner_submit` audit entry. Marking it reviewed stores a `submission_reviews` row
(keyed by that audit entry, which carries the kind + record id), so a record that is later cancelled
and submitted again is a new, un-reviewed submission. Reviewed items stay in the audit log / Activity.

Rejecting an owner payment removes it from every count exactly like an approved cancel request
(`owner_submissions.undo_record`) and keeps a `submission_rejections` row so the owner still sees it
under My submissions as Rejected, with the reason. Expenses keep their own v7.0 reject (status column).
"""
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    CancelRequest,
    Flat,
    SubmissionRejection,
    SubmissionReview,
)
from app.services import app_settings as app_settings_svc
from app.services import audit as audit_svc
from app.services import expense_review as expense_review_svc
from app.services import ledger as ledger_svc
from app.services import owner_submissions as owner_svc

LEGACY_REVIEWED_KEY = "owner_submissions_reviewed_at"
MAX_REASON = 255

KIND_SHORT = {
    "maintenance_payment": "Maintenance",
    "expense": "Expense",
    "corpus_payment": "Corpus",
    "project_contribution": "Project",
}


class ReviewError(ValueError):
    pass


def _legacy_floor(db: Session) -> datetime | None:
    raw = app_settings_svc.get_value(db, LEGACY_REVIEWED_KEY)
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _item(db: Session, audit: AuditLog, record) -> dict:
    kind = audit.entity_type
    flat_id = owner_svc.record_flat_id(kind, record)
    flat = db.get(Flat, flat_id) if flat_id else None
    if kind == "maintenance_payment":
        what = f"Maintenance {ledger_svc.month_label(record.period)}" if record.period else "Maintenance"
        link = f"/payments/maintenance?period_id={record.period_id}"
        amount = record.amount
    elif kind == "expense":
        what = f"Expense “{record.description}”"
        link = "/payments/expenses"
        amount = record.amount
    elif kind == "project_contribution":
        what = f"Project {record.project.name}" if record.project else "Project payment"
        link = f"/projects/{record.project_id}"
        amount = record.amount
    else:
        what = f"Corpus {record.campaign.name}" if record.campaign else "Corpus payment"
        link = "/payments/corpus"
        amount = record.amount
    return {
        "audit_id": audit.id,
        "kind": kind,
        "record_id": record.id,
        "what": what,
        "flat": f"Flat {flat.number}" if flat else "Account Balance",
        "amount": amount,
        "when": audit.created_at,
        "user": audit.user_label,
        "link": link,
    }


def pending(db: Session) -> list[dict]:
    """Owner submissions still waiting for the admin: live, not reviewed, newest first."""
    floor = _legacy_floor(db)
    q = select(AuditLog).where(AuditLog.action == "owner_submit").order_by(AuditLog.id.desc())
    if floor:
        q = q.where(AuditLog.created_at > floor)
    reviewed = set(db.scalars(select(SubmissionReview.audit_id)).all())
    seen: set[tuple[str, int | None]] = set()
    out: list[dict] = []
    for audit in db.scalars(q).all():
        key = (audit.entity_type, audit.entity_id)
        if key in seen:
            continue  # only the latest submission of a record can be pending
        seen.add(key)
        if audit.id in reviewed or audit.entity_type not in owner_svc.RECORD_MODELS or audit.entity_id is None:
            continue
        record = owner_svc.get_record(db, audit.entity_type, audit.entity_id)
        if not owner_svc.record_is_live(audit.entity_type, record):
            continue  # cancelled, rejected or reverted: nothing left to review
        out.append(_item(db, audit, record))
    return out


def count_pending(db: Session) -> int:
    return len(pending(db))


def mark_reviewed(db: Session, audit_id: int, *, admin_label: str) -> SubmissionReview | None:
    audit = db.get(AuditLog, audit_id)
    if audit is None or audit.action != "owner_submit":
        raise ReviewError("That submission no longer exists.")
    if db.scalar(select(SubmissionReview).where(SubmissionReview.audit_id == audit_id)) is not None:
        return None
    row = SubmissionReview(
        audit_id=audit.id, kind=audit.entity_type, record_id=audit.entity_id,
        reviewed_at=datetime.utcnow(), reviewed_by=admin_label,
    )
    db.add(row)
    db.flush()
    audit_svc.log_audit(
        db, entity_type="submission", entity_id=audit.id, action="review",
        after={"kind": audit.entity_type, "record_id": audit.entity_id, "submitted_by": audit.user_label},
    )
    return row


def mark_all_reviewed(db: Session, *, admin_label: str) -> int:
    items = pending(db)
    now = datetime.utcnow()
    for it in items:
        db.add(SubmissionReview(
            audit_id=it["audit_id"], kind=it["kind"], record_id=it["record_id"],
            reviewed_at=now, reviewed_by=admin_label,
        ))
    if items:
        db.flush()
        audit_svc.log_audit(
            db, entity_type="submission", entity_id=None, action="review_all",
            after={"count": len(items), "audit_ids": [it["audit_id"] for it in items]},
        )
    return len(items)


def reject_payment(
    db: Session, record_type: str, record_id: int, *, admin_label: str, reason: str = ""
) -> str:
    """Reject an owner submission. Returns a short description of what was rejected."""
    if record_type == "expense":
        exp = expense_review_svc.reject(
            db, db.get(owner_svc.RECORD_MODELS["expense"], record_id), admin_label=admin_label, reason=reason
        )
        return f"expense “{exp.description}” ₹{exp.amount}"
    if record_type not in owner_svc.RECORD_MODELS:
        raise ReviewError("Unknown record type.")
    record = owner_svc.get_record(db, record_type, record_id)
    if not owner_svc.record_is_live(record_type, record):
        raise ReviewError("That payment no longer exists or was already removed.")
    if record.submitted_by_user_id is None:
        raise ReviewError("Only payments submitted by owners can be rejected; use Revert for admin entries.")
    summary = owner_svc.describe(db, record_type, record)[:255]
    flat_id = owner_svc.record_flat_id(record_type, record)
    row = SubmissionRejection(
        record_type=record_type,
        record_id=record_id,
        flat_id=flat_id,
        submitted_by_user_id=record.submitted_by_user_id,
        summary=summary,
        amount=record.amount,
        paid_on=record.paid_on,
        payee_vpa=getattr(record, "payee_vpa", None),
        payee_mobile=getattr(record, "payee_mobile", None),
        payee_name=getattr(record, "payee_name", None),
        payee_settings_id=getattr(record, "payee_settings_id", None),
        reason=reason.strip()[:MAX_REASON] or None,
        rejected_at=datetime.utcnow(),
        rejected_by=admin_label,
    )
    action, before = owner_svc.undo_record(db, record_type, record)
    db.add(row)
    for req in db.scalars(
        select(CancelRequest).where(
            CancelRequest.record_type == record_type, CancelRequest.record_id == record_id,
            CancelRequest.status == "pending",
        )
    ).all():
        req.status = "approved"
        req.decided_at = datetime.utcnow()
        req.decided_by = admin_label
        req.decision_note = "Payment was rejected by the admin."
    db.flush()
    audit_svc.log_audit(
        db, entity_type=record_type, entity_id=record_id, action="reject", before=json.loads(before) if before else None,
        after={"reason": row.reason, "rejection_id": row.id, "removed": action},
    )
    return f"{KIND_SHORT[record_type].lower()} payment ₹{row.amount}"


def rejections_for_user(db: Session, user_id: int) -> list[SubmissionRejection]:
    return list(db.scalars(
        select(SubmissionRejection).where(SubmissionRejection.submitted_by_user_id == user_id)
        .order_by(SubmissionRejection.id.desc())
    ).all())
