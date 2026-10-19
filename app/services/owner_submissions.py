"""Records submitted by owners and their cancel-request workflow."""
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CancelRequest, CorpusPayment, Expense, MaintenancePayment, ProjectContribution
from app.services import audit as audit_svc
from app.services import payment_details as payment_details_svc
from app.services import ledger as ledger_svc
from app.services import maintenance_fee as maintenance_fee_svc
from app.services import projects as projects_svc
from app.services import reminders as reminders_svc
from app.dates import format_date as fmt_date, to_ist

PAYMENT_MODES = ("UPI", "Cash", "Bank transfer", "Cheque", "Other")

RECORD_MODELS = {
    "maintenance_payment": MaintenancePayment,
    "expense": Expense,
    "corpus_payment": CorpusPayment,
    "project_contribution": ProjectContribution,
}
RECORD_LABELS = {
    "maintenance_payment": "Maintenance payment",
    "expense": "Common expense",
    "corpus_payment": "Corpus payment",
    "project_contribution": "Project payment",
}


class SubmissionError(ValueError):
    pass


def get_record(db: Session, record_type: str, record_id: int):
    model = RECORD_MODELS.get(record_type)
    if model is None:
        raise SubmissionError("Unknown record type")
    return db.get(model, record_id)


def record_flat_id(record_type: str, record) -> int | None:
    if record_type == "expense":
        return record.paid_by_flat_id
    return record.flat_id


def record_is_live(record_type: str, record) -> bool:
    if record is None:
        return False
    if record_type == "maintenance_payment":
        return record.is_paid
    if record_type == "expense":
        return record.deleted_at is None and not record.is_rejected
    if record_type == "project_contribution":
        return True
    return record.is_paid


def _utr_suffix(record) -> str:
    """The admin matches UPI payments against the bank statement by this reference."""
    if getattr(record, "payment_mode", None) == "UPI" and getattr(record, "payment_note", None):
        return f" — UTR {record.payment_note}"
    return ""


def _due_suffix(due: date | None) -> str:
    return f" — due {fmt_date(due)}" if due else ""


def describe(db: Session, record_type: str, record) -> str:
    if record_type == "maintenance_payment":
        label = ledger_svc.month_label(record.period) if record.period else "?"
        due = reminders_svc.maintenance_due_date(record.period) if record.period else None
        return f"Maintenance {label} — Flat {record.flat.number} — ₹{record.amount}{_utr_suffix(record)}{_due_suffix(due)}"
    if record_type == "expense":
        flat = f"Flat {record.paid_by_flat.number}" if record.paid_by_flat else "Account Balance"
        return f"Expense {fmt_date(record.expense_date)} {record.description} — {flat} — ₹{record.amount}"
    if record_type == "project_contribution":
        return (f"Project {record.project.name} — Flat {record.flat.number} — ₹{record.amount}{_utr_suffix(record)}"
                f"{_due_suffix(record.project.due_date)}")
    name = record.campaign.name if record.campaign else (record.description or "Corpus")
    due = record.campaign.due_date if record.campaign else None
    return f"Corpus {name} — Flat {record.flat.number} — ₹{record.amount}{_utr_suffix(record)}{_due_suffix(due)}"


def pending_requests_map(db: Session, record_type: str) -> dict[int, CancelRequest]:
    rows = db.scalars(
        select(CancelRequest).where(
            CancelRequest.record_type == record_type, CancelRequest.status == "pending"
        )
    ).all()
    return {r.record_id: r for r in rows}


def latest_requests_map(db: Session, record_type: str) -> dict[int, CancelRequest]:
    rows = db.scalars(
        select(CancelRequest)
        .where(CancelRequest.record_type == record_type)
        .order_by(CancelRequest.id)
    ).all()
    return {r.record_id: r for r in rows}


def request_cancel(db: Session, *, user_id: int, record_type: str, record_id: int, reason: str) -> CancelRequest:
    record = get_record(db, record_type, record_id)
    if not record_is_live(record_type, record):
        raise SubmissionError("That record no longer exists.")
    if record.submitted_by_user_id != user_id:
        raise SubmissionError("You can only cancel records you submitted.")
    if pending_requests_map(db, record_type).get(record_id):
        raise SubmissionError("A cancel request is already pending for this record.")
    req = CancelRequest(
        record_type=record_type,
        record_id=record_id,
        flat_id=record_flat_id(record_type, record),
        requested_by_user_id=user_id,
        reason=reason.strip() or None,
        status="pending",
        summary=describe(db, record_type, record)[:255],
    )
    db.add(req)
    db.flush()
    audit_svc.log_audit(db, entity_type="cancel_request", entity_id=req.id, action="create", after=req)
    return req


def undo_record(db: Session, record_type: str, record) -> tuple[str, str | None]:
    """Take a live owner record out of every count. Returns (audit action, before snapshot).

    Shared by an approved cancel request and an admin rejection, so both leave identical ledgers:
    maintenance goes back to unpaid at the month's fee, an expense is soft-deleted, project and
    collection payments are deleted (the share stays, the remaining amount goes back up).
    """
    before = audit_svc.snapshot(record)
    if record_type == "maintenance_payment":
        record.is_paid = False
        record.paid_on = None
        record.payment_mode = None
        record.payment_note = None
        record.submitted_by_user_id = None
        payment_details_svc.clear_payee(record)
        record.reverted_at = datetime.utcnow()
        if record.period is not None:
            # The owner may have typed a different amount; what is owed again is the month's fee.
            record.amount = maintenance_fee_svc.fee_for_period(db, record.period)
        action = "revert"
    elif record_type == "expense":
        if record.is_settled:
            raise SubmissionError("This expense was already settled — revert the settlement first.")
        record.deleted_at = datetime.utcnow()
        action = "delete"
    elif record_type == "project_contribution":
        projects_svc.remove_contribution(db, record)
        action = "delete"
    elif record.campaign_id is not None:
        db.delete(record)
        action = "delete"
    else:
        # Legacy corpus line item: the obligation stays, only the payment is undone.
        record.is_paid = False
        record.paid_on = None
        record.payment_mode = None
        record.submitted_by_user_id = None
        payment_details_svc.clear_payee(record)
        record.reverted_at = datetime.utcnow()
        action = "revert"
    db.flush()
    return action, before


def approve(db: Session, req: CancelRequest, *, admin_label: str, note: str = "") -> None:
    if req.status != "pending":
        raise SubmissionError("This request was already decided.")
    record = get_record(db, req.record_type, req.record_id)
    if record_is_live(req.record_type, record):
        action, before = undo_record(db, req.record_type, record)
        audit_svc.log_audit(
            db,
            entity_type=req.record_type,
            entity_id=req.record_id,
            action=action,
            before=before,
            after=None if action == "delete" and req.record_type != "expense" else record,
        )
    req.status = "approved"
    req.decided_at = datetime.utcnow()
    req.decided_by = admin_label
    req.decision_note = note.strip() or None
    audit_svc.log_audit(db, entity_type="cancel_request", entity_id=req.id, action="approve", after=req)


def reject(db: Session, req: CancelRequest, *, admin_label: str, note: str = "") -> None:
    if req.status != "pending":
        raise SubmissionError("This request was already decided.")
    req.status = "rejected"
    req.decided_at = datetime.utcnow()
    req.decided_by = admin_label
    req.decision_note = note.strip() or None
    audit_svc.log_audit(db, entity_type="cancel_request", entity_id=req.id, action="reject", after=req)


def _record_date(record_type: str, rec) -> date | None:
    if record_type == "expense":
        return rec.expense_date
    return rec.paid_on


def submissions_for_user(db: Session, user_id: int) -> list[dict]:
    """Every record this user submitted (incl. cancelled ones), newest first, with cancel status."""
    out = []
    for record_type, model in RECORD_MODELS.items():
        latest = latest_requests_map(db, record_type)
        for rec in db.scalars(select(model).where(model.submitted_by_user_id == user_id)).all():
            live = record_is_live(record_type, rec)
            req = latest.get(rec.id)
            if req is not None and live and req.status == "approved":
                req = None  # an older, already-approved cancel of a since re-submitted row
            out.append(
                {
                    "record_type": record_type,
                    "label": RECORD_LABELS[record_type],
                    "record": rec,
                    "summary": describe(db, record_type, rec),
                    "live": live,
                    "request": req,
                    "when": _record_date(record_type, rec),
                    "rejection": None,
                }
            )
    from app.models import SubmissionRejection

    rejections = db.scalars(
        select(SubmissionRejection).where(SubmissionRejection.submitted_by_user_id == user_id)
    ).all()
    rejected_keys = {(r.record_type, r.record_id) for r in rejections}
    for rej in rejections:
        out.append(
            {
                "record_type": rej.record_type,
                "label": RECORD_LABELS.get(rej.record_type, rej.record_type),
                "record": None,
                "summary": rej.summary,
                "live": False,
                "request": None,
                "when": rej.paid_on or to_ist(rej.rejected_at).date(),
                "rejection": rej,
            }
        )
    # Approved maintenance cancels clear submitted_by on the row; list those from the requests.
    for req in db.scalars(
        select(CancelRequest).where(
            CancelRequest.requested_by_user_id == user_id,
            CancelRequest.status == "approved",
            CancelRequest.record_type != "expense",
        )
    ).all():
        if any(s["request"] is not None and s["request"].id == req.id for s in out):
            continue
        if (req.record_type, req.record_id) in rejected_keys:
            continue
        out.append(
            {
                "record_type": req.record_type,
                "label": RECORD_LABELS.get(req.record_type, req.record_type),
                "record": None,
                "summary": req.summary,
                "live": False,
                "request": req,
                "when": to_ist(req.created_at).date(),
                "rejection": None,
            }
        )
    out.sort(key=lambda s: s["when"] or date.min, reverse=True)
    return out
