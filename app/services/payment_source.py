"""Who paid an expense (v7.5): a flat (owner advance, reimbursed later) or the society account.

The Account Balance only ever drops through a Settlement row. A society-paid expense therefore creates its
settlement when it is saved, so every balance, closed month, report and export keeps using the same formulas.
`expenses.payment_source` records the choice:

- ``flat``     a flat is set; the flat is reimbursed when the admin settles it (unchanged behaviour).
- ``society``  no flat; settled when saved, with a short note on the settlement.
- empty        an older expense with no flat that the admin has not confirmed yet. It is counted exactly as
               before and never guessed; the admin confirms it with one click.
"""
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Expense, Flat, FundType, Settlement
from app.services import audit as audit_svc
from app.services import ledger as ledger_svc
from app.services.activity import inr

log = logging.getLogger("nest5.payment_source")

FLAT = "flat"
SOCIETY = "society"
SOCIETY_LABEL = "Society account"
UNCONFIRMED_LABEL = "To confirm"
MAX_NOTE = 255


class SourceError(ValueError):
    """A rule was broken; the message is shown to the admin as is."""


@dataclass
class Outcome:
    action: str  # audit action to record on the expense
    message: str


def snap(obj) -> dict:
    return json.loads(audit_svc.snapshot(obj) or "{}")


def clean_note(note: str | None) -> str:
    return " ".join((note or "").split())[:MAX_NOTE]


def live_settlement(exp: Expense) -> Settlement | None:
    st = exp.settlement
    return st if st is not None and st.reverted_at is None else None


def label(exp: Expense) -> str:
    """Short text for lists and exports."""
    src = exp.source
    if src == FLAT:
        return f"Flat {exp.paid_by_flat.number}" if exp.paid_by_flat is not None else "Flat"
    if src == SOCIETY:
        return SOCIETY_LABEL
    return UNCONFIRMED_LABEL


def _check_balance(db: Session, needed: int, what: str = "") -> None:
    available = ledger_svc.available_for_settlement(db)
    if available < needed:
        raise SourceError(
            f"Balance {inr(available)} cannot cover {inr(needed)}{what}. "
            "Save it as paid by a flat instead (it is reimbursed later), or record it once the balance is higher."
        )


def _settle(db: Session, exp: Expense, settled_on: date, note: str) -> Settlement:
    """Create the settlement (settlements.expense_id is unique, so a reverted row is reused)."""
    st = exp.settlement
    if st is not None:
        before = snap(st)
        st.fund_type = FundType.MAINTENANCE
        st.amount = exp.amount
        st.settled_on = settled_on
        st.note = note or None
        st.reverted_at = None
        action = "update"
    else:
        before = None
        st = Settlement(expense_id=exp.id, fund_type=FundType.MAINTENANCE, amount=exp.amount,
                        settled_on=settled_on, note=note or None)
        db.add(st)
        action = "create"
    exp.is_settled = True
    db.flush()
    audit_svc.log_audit(db, entity_type="settlement", entity_id=st.id, action=action, before=before, after=st)
    return st


def change(
    db: Session,
    exp: Expense,
    *,
    source: str,
    flat_id: int | None,
    amount: int,
    note: str = "",
) -> Outcome:
    """Move an expense (new, edited or confirmed) to `source`, keeping the flat, the settlement and the
    amount in step. Raises SourceError before anything changes when a rule is broken."""
    source = (source or "").strip()
    if source not in (FLAT, SOCIETY):
        raise SourceError("Choose who paid: a flat, or the society account.")
    if amount is None or amount < 1:
        raise SourceError("Enter an amount of at least ₹1.")
    note = clean_note(note)
    old = exp.source
    settled = bool(exp.is_settled)
    live = live_settlement(exp)
    amount_changed = exp.id is not None and amount != exp.amount

    if source == FLAT:
        flat = db.get(Flat, flat_id) if flat_id else None
        if flat is None:
            raise SourceError("Choose the flat that paid.")
        if settled and old != SOCIETY and amount_changed:
            raise SourceError(
                "This expense is already settled. Revert its settlement on the Settlements page before changing the amount."
            )
        reverted = False
        if old == SOCIETY:
            if live is not None:
                before = snap(live)
                live.reverted_at = datetime.utcnow()
                audit_svc.log_audit(db, entity_type="settlement", entity_id=live.id, action="revert",
                                    before=before, after=live)
            exp.is_settled = False
            reverted = True
        exp.payment_source = FLAT
        exp.paid_by_flat_id = flat.id
        exp.amount = amount
        db.flush()
        if reverted:
            return Outcome("switch_source", f"Now paid by Flat {flat.number}; the settlement was reverted and the balance is restored.")
        if old is None:
            return Outcome("confirm_source", f"Confirmed as paid by Flat {flat.number}.")
        return Outcome("update", "Expense updated.")

    # source == SOCIETY
    if exp.submitted_by_user_id is not None and old == FLAT:
        raise SourceError(
            "An owner recorded this as their own advance. Reject it or ask the owner to cancel it; it cannot be moved to the society account."
        )
    if old == FLAT and settled:
        raise SourceError(
            "This expense was already reimbursed to the flat, so it cannot be changed to the society account."
        )
    if not note and live is not None and old != FLAT:
        note = clean_note(live.note)
    if not note:
        raise SourceError("A short note is required for an expense paid from the society account (for example: paid in cash from the office account).")

    if old in (SOCIETY, None) and settled and live is not None:
        delta = amount - live.amount
        if delta > 0:
            _check_balance(db, delta, " more")
        out_action = "confirm_source" if old is None else "update"
        exp.payment_source = SOCIETY
        exp.paid_by_flat_id = None
        exp.amount = amount
        before = snap(live)
        changed = live.amount != amount or (live.note or "") != note or (old == SOCIETY and live.settled_on != exp.expense_date)
        live.amount = amount
        live.note = note
        if old == SOCIETY:
            live.settled_on = exp.expense_date
        db.flush()
        if changed:
            audit_svc.log_audit(db, entity_type="settlement", entity_id=live.id, action="update", before=before, after=live)
        msg = "Confirmed as paid from the society account." if old is None else "Expense updated."
        return Outcome(out_action, msg)

    _check_balance(db, amount)
    exp.payment_source = SOCIETY
    exp.paid_by_flat_id = None
    exp.amount = amount
    _settle(db, exp, exp.expense_date, note)
    return Outcome("switch_source" if old == FLAT else "update",
                   f"Paid from the society account; the Account Balance is now {inr(ledger_svc.available_for_settlement(db))}.")


def update_unconfirmed_amount(db: Session, exp: Expense, amount: int) -> None:
    """Edit the amount of an expense whose source is not confirmed yet (no flat). If it is settled, its
    settlement follows, and an increase must fit in the Account Balance."""
    if amount is None or amount < 1:
        raise SourceError("Enter an amount of at least ₹1.")
    live = live_settlement(exp)
    if live is not None and amount != live.amount:
        if amount > live.amount:
            _check_balance(db, amount - live.amount, " more")
        before = snap(live)
        live.amount = amount
        exp.amount = amount
        db.flush()
        audit_svc.log_audit(db, entity_type="settlement", entity_id=live.id, action="update", before=before, after=live)
    else:
        exp.amount = amount


def unconfirmed(db: Session) -> list[dict]:
    """Counted expenses with no flat and no confirmed source, each with the pre-selected suggestion."""
    rows = db.scalars(
        select(Expense)
        .where(Expense.counted(), Expense.paid_by_flat_id.is_(None),
               (Expense.payment_source.is_(None)) | (Expense.payment_source == ""))
        .order_by(Expense.expense_date, Expense.id)
    ).all()
    out = []
    for exp in rows:
        live = live_settlement(exp)
        out.append({"expense": exp, "settlement": live,
                    "suggest": SOCIETY if live is not None else "",
                    "reason": (f"it has a settlement noted “{live.note}”" if live is not None and live.note
                               else "it has a settlement" if live is not None else "")})
    return out


def backfill(db: Session) -> int:
    """Idempotent: every expense that has a flat but no source gets 'flat'. Empty-flat rows are never touched."""
    rows = db.scalars(
        select(Expense).where(Expense.paid_by_flat_id.isnot(None),
                              (Expense.payment_source.is_(None)) | (Expense.payment_source == ""))
    ).all()
    for exp in rows:
        exp.payment_source = FLAT
    if rows:
        db.flush()
    return len(rows)


def problems(db: Session) -> list[str]:
    """Rows whose source, flat, settlement and amount disagree. Reported, never fixed silently."""
    out: list[str] = []
    for exp in db.scalars(select(Expense).where(Expense.counted()).order_by(Expense.id)).all():
        live = live_settlement(exp)
        tag = f"Expense #{exp.id} “{exp.description}”"
        if exp.payment_source == FLAT and not exp.paid_by_flat_id:
            out.append(f"{tag} is marked as paid by a flat but has no flat.")
        elif exp.payment_source == SOCIETY:
            if exp.paid_by_flat_id:
                out.append(f"{tag} is marked as paid from the society account but also has a flat.")
            if not exp.is_settled or live is None:
                out.append(f"{tag} is paid from the society account but has no live settlement.")
        if live is not None and live.amount != exp.amount:
            out.append(f"{tag}: the settlement ({inr(live.amount)}) differs from the expense ({inr(exp.amount)}).")
        if exp.is_settled and live is None:
            out.append(f"{tag} is marked settled but has no live settlement.")
        if not exp.is_settled and live is not None:
            out.append(f"{tag} has a live settlement but is not marked settled.")
    return out


def log_problems(db: Session) -> list[str]:
    found = problems(db)
    for line in found:
        log.warning("payment source check: %s", line)
    return found
