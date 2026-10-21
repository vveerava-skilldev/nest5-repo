"""Payment settings: the UPI ID, mobile number and payee name owners pay to (admin-managed).

Config values (`UPI_VPA`, `UPI_MOBILE`, `UPI_PAYEE_NAME`) are only the initial seed. The live values
sit in app_settings; every change appends a PaymentDetailChange row (never edited or deleted) that
also records the app's computed balances at that moment.

Every payment row copies the payee it was made to (`payee_snapshot`) at record time, so changing
these settings later never rewrites a past payment.
"""
import re
from datetime import datetime

from sqlalchemy import event, func, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AppSetting,
    CorpusPayment,
    MaintenancePayment,
    PaymentDetailChange,
    Project,
    ProjectContribution,
    SubmissionRejection,
)
from app.services import app_settings as app_settings_svc
from app.services import audit as audit_svc

KEY_VPA = "pay_upi_vpa"
KEY_MOBILE = "pay_upi_mobile"
KEY_PAYEE = "pay_payee_name"
BACKFILL_KEY = "pay_payee_backfill_done"

INITIAL_REASON = "Initial values (seeded from configuration)"
MAX_REASON = 255

# local part: letters/digits/._-  ;  handle: letters then letters/digits (e.g. ybl, okhdfcbank, paytm)
VPA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}@[A-Za-z][A-Za-z0-9]{1,31}$")
MOBILE_RE = re.compile(r"^[6-9][0-9]{9}$")
BANK_BALANCE_RE = re.compile(r"^[\s₹,.\-0-9]*\d[\s₹,.\-0-9]*(?:\s*(?:rs\.?|inr))?$", re.I)

PAYEE_TABLES = (MaintenancePayment, CorpusPayment, ProjectContribution, SubmissionRejection)


class PaymentSettingsError(ValueError):
    def __init__(self, errors: list[str] | str):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


# ---- append-only guard -----------------------------------------------------------------------


@event.listens_for(PaymentDetailChange, "before_update")
def _no_update(_mapper, _connection, _target) -> None:
    raise PaymentSettingsError("Payment settings history is append-only and cannot be edited.")


@event.listens_for(PaymentDetailChange, "before_delete")
def _no_delete(_mapper, _connection, _target) -> None:
    raise PaymentSettingsError("Payment settings history is append-only and cannot be deleted.")


# ---- current values --------------------------------------------------------------------------


def seed_values() -> dict:
    return {"vpa": settings.upi_vpa, "mobile": settings.upi_mobile, "payee": settings.upi_payee_name}


def get_details(db: Session) -> dict:
    """Live values (database first, config seed only if never set)."""
    return {
        "vpa": app_settings_svc.get_value(db, KEY_VPA) or settings.upi_vpa,
        "mobile": app_settings_svc.get_value(db, KEY_MOBILE) or settings.upi_mobile,
        "payee": app_settings_svc.get_value(db, KEY_PAYEE) or settings.upi_payee_name,
    }


def latest_change(db: Session) -> PaymentDetailChange | None:
    return db.scalar(select(PaymentDetailChange).order_by(PaymentDetailChange.id.desc()).limit(1))


def history(db: Session) -> list[PaymentDetailChange]:
    return list(db.scalars(select(PaymentDetailChange).order_by(PaymentDetailChange.id.desc())).all())


# ---- balances --------------------------------------------------------------------------------


def snapshot_balances(db: Session) -> dict:
    """The app's computed balances right now (not bank data)."""
    from app.services import balances as balances_svc
    from app.services import corpus_campaigns as campaigns_svc
    from app.services import ledger as ledger_svc

    fund = int(ledger_svc.compute_fund_balance(db))
    corpus = int(campaigns_svc.building_totals(db)["collected"])
    projects = int(
        db.scalar(
            select(func.coalesce(func.sum(ProjectContribution.amount), 0))
            .join(Project, ProjectContribution.project_id == Project.id)
            .where(Project.deleted_at.is_(None))
        )
        or 0
    )
    owed = int(balances_svc.overview(db)["owed"])
    return {
        "fund_balance": fund,
        "corpus_collected": corpus,
        "projects_collected": projects,
        "owed_back": owed,
        "total_balance": fund + corpus + projects,
    }


# ---- initial row, backfill, per-payment snapshot ---------------------------------------------


def ensure_initial(db: Session) -> PaymentDetailChange:
    """Seed the live values and the first (initial) history row once. Idempotent."""
    first = db.scalar(select(PaymentDetailChange).order_by(PaymentDetailChange.id).limit(1))
    if first is not None:
        return first
    values = get_details(db)
    for key, val in ((KEY_VPA, values["vpa"]), (KEY_MOBILE, values["mobile"]), (KEY_PAYEE, values["payee"])):
        if db.get(AppSetting, key) is None:
            db.add(AppSetting(key=key, value=val, updated_by="system"))
    snap = snapshot_balances(db)
    first = PaymentDetailChange(
        changed_at=datetime.utcnow(),
        changed_by="system",
        is_initial=True,
        old_upi_vpa=None,
        new_upi_vpa=values["vpa"],
        old_upi_mobile=None,
        new_upi_mobile=values["mobile"],
        old_payee_name=None,
        new_payee_name=values["payee"],
        reason=INITIAL_REASON,
        **snap,
    )
    db.add(first)
    db.flush()
    return first


def backfill_payees(db: Session) -> int:
    """One-time: stamp payment rows that predate v7.3 with the initial seeded payee.

    Only the four payee columns of rows that are real payments are written (maintenance/corpus paid
    rows, every project contribution and every rejected-submission record). Amounts, dates, status
    and every other column are untouched. Runs once (marker in app_settings).
    """
    if app_settings_svc.get_value(db, BACKFILL_KEY) == "1":
        return 0
    first = db.scalar(
        select(PaymentDetailChange).where(PaymentDetailChange.is_initial.is_(True)).order_by(PaymentDetailChange.id)
    ) or ensure_initial(db)
    stamp = {
        "payee_vpa": first.new_upi_vpa,
        "payee_mobile": first.new_upi_mobile,
        "payee_name": first.new_payee_name,
        "payee_settings_id": first.id,
    }
    touched = 0
    for model in PAYEE_TABLES:
        q = update(model).where(model.payee_vpa.is_(None)).values(**stamp)
        if model in (MaintenancePayment, CorpusPayment):
            q = q.where(model.is_paid.is_(True))
        touched += db.execute(q.execution_options(synchronize_session=False)).rowcount or 0
    app_settings_svc.set_value(db, BACKFILL_KEY, "1", updated_by="system")
    return touched


def after_restore_without_history(db: Session) -> int:
    """A restored dump from before v7.3 has no payee columns or settings: re-derive them from the history kept here.

    The live values come back from the newest history row and the restored payment rows get the initial
    payee (the only one that existed when such a dump was taken). Returns the rows stamped.
    """
    ensure_initial(db)
    latest = latest_change(db)
    if latest is not None:
        for key, val in ((KEY_VPA, latest.new_upi_vpa), (KEY_MOBILE, latest.new_upi_mobile), (KEY_PAYEE, latest.new_payee_name)):
            app_settings_svc.set_value(db, key, val, updated_by="system")
    app_settings_svc.set_value(db, BACKFILL_KEY, "0", updated_by="system")
    return backfill_payees(db)


def payee_snapshot(db: Session) -> dict:
    """Values to copy onto a payment row being recorded now."""
    ensure_initial(db)
    values = get_details(db)
    change = latest_change(db)
    return {
        "payee_vpa": values["vpa"],
        "payee_mobile": values["mobile"],
        "payee_name": values["payee"],
        "payee_settings_id": change.id if change else None,
    }


def stamp_initial(db: Session, row) -> None:
    """Pre-v7.3 payments (legacy rows being converted) carry the initial seeded payee."""
    first = db.scalar(
        select(PaymentDetailChange).where(PaymentDetailChange.is_initial.is_(True)).order_by(PaymentDetailChange.id)
    ) or ensure_initial(db)
    row.payee_vpa, row.payee_mobile = first.new_upi_vpa, first.new_upi_mobile
    row.payee_name, row.payee_settings_id = first.new_payee_name, first.id


def stamp_payee(db: Session, row) -> None:
    for k, v in payee_snapshot(db).items():
        setattr(row, k, v)


def clear_payee(row) -> None:
    row.payee_vpa = row.payee_mobile = row.payee_name = row.payee_settings_id = None


def paid_to(row) -> str:
    """Short text for lists: the UPI ID the payment was made to ('' for rows with no snapshot)."""
    return getattr(row, "payee_vpa", None) or ""


def paid_to_title(row) -> str:
    parts = [getattr(row, "payee_name", None), getattr(row, "payee_mobile", None)]
    return " · ".join(p for p in parts if p)


# ---- validation and change flow --------------------------------------------------------------


def normalise_mobile(raw: str) -> str:
    digits = re.sub(r"[\s\-]", "", raw or "")
    if digits.startswith("+91"):
        digits = digits[3:]
    elif len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits


def validate(vpa: str, mobile: str, payee: str, reason: str, bank_balance: str = "") -> dict:
    errors: list[str] = []
    vpa = (vpa or "").strip()
    mobile = normalise_mobile((mobile or "").strip())
    payee = " ".join((payee or "").split())
    reason = " ".join((reason or "").split())
    bank_balance = (bank_balance or "").strip()
    if not VPA_RE.match(vpa):
        errors.append("UPI ID must look like name@bank (letters, digits, dot, dash or underscore before the @).")
    if not MOBILE_RE.match(mobile):
        errors.append("Mobile number must be a 10-digit Indian mobile number (starts with 6, 7, 8 or 9).")
    if not 2 <= len(payee) <= 100:
        errors.append("Payee name must be 2 to 100 characters.")
    if len(reason) < 3:
        errors.append("Reason is required (at least 3 characters).")
    elif len(reason) > MAX_REASON:
        errors.append(f"Reason can be at most {MAX_REASON} characters.")
    if bank_balance and (len(bank_balance) > 60 or not BANK_BALANCE_RE.match(bank_balance)):
        errors.append("Actual bank balance must be an amount like 45,300 (or left blank).")
    if errors:
        raise PaymentSettingsError(errors)
    return {"vpa": vpa, "mobile": mobile, "payee": payee, "reason": reason, "bank_balance": bank_balance or None}


def diff(current: dict, new: dict) -> list[dict]:
    labels = (("vpa", "UPI ID"), ("mobile", "Mobile number"), ("payee", "Payee name"))
    return [{"label": lab, "old": current[k], "new": new[k]} for k, lab in labels if current[k] != new[k]]


def apply_change(
    db: Session, *, vpa: str, mobile: str, payee: str, reason: str, bank_balance: str = "", actor: str = "admin"
) -> PaymentDetailChange:
    clean = validate(vpa, mobile, payee, reason, bank_balance)
    ensure_initial(db)
    current = get_details(db)
    if not diff(current, clean):
        raise PaymentSettingsError("Nothing changed: the new values are the same as the current ones.")
    snap = snapshot_balances(db)
    row = PaymentDetailChange(
        changed_at=datetime.utcnow(),
        changed_by=actor,
        is_initial=False,
        old_upi_vpa=current["vpa"],
        new_upi_vpa=clean["vpa"],
        old_upi_mobile=current["mobile"],
        new_upi_mobile=clean["mobile"],
        old_payee_name=current["payee"],
        new_payee_name=clean["payee"],
        reason=clean["reason"],
        bank_balance_entered=clean["bank_balance"],
        **snap,
    )
    db.add(row)
    db.flush()
    for key, val in ((KEY_VPA, clean["vpa"]), (KEY_MOBILE, clean["mobile"]), (KEY_PAYEE, clean["payee"])):
        app_settings_svc.set_value(db, key, val, updated_by=actor)
    audit_svc.log_audit(
        db,
        entity_type="payment_settings",
        entity_id=row.id,
        action="change",
        user_label=actor,
        before={"upi_vpa": current["vpa"], "upi_mobile": current["mobile"], "payee_name": current["payee"]},
        after={
            "upi_vpa": clean["vpa"],
            "upi_mobile": clean["mobile"],
            "payee_name": clean["payee"],
            "reason": clean["reason"],
            "fund_balance": snap["fund_balance"],
            "total_balance": snap["total_balance"],
            "bank_balance_entered": clean["bank_balance"],
        },
    )
    return row
