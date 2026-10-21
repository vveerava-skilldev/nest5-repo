"""User accounts: first-run bootstrap, owner flat links, password reset tokens."""
import hashlib
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.dates import format_ist_stamp
from app.domain.flats import FLAT_ROSTER
from app.models import Flat, PasswordResetToken, User, UserRole
from app.services import audit as audit_svc
from app.services.auth import check_password, generate_temp_password, hash_password

ADMIN_USERNAME = "admin"


def user_snapshot(user: User) -> dict:
    """Audit-safe view of a user (never includes the password hash)."""
    return {
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "role": user.role,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        "flats": [f.number for f in user.flats],
        "tenant_payments": user.tenant_payments,
        "deleted": user.deleted_at is not None,
    }


def owner_username(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".")


def initial_passwords_path() -> Path:
    return Path(settings.instance_dir) / "initial-passwords.txt"


def get_by_username(db: Session, username: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.username) == username.strip().lower()))


def get_by_login(db: Session, login: str) -> User | None:
    """Look a user up by username or email (case-insensitive)."""
    login = login.strip()
    if not login:
        return None
    user = get_by_username(db, login)
    if user:
        return None if user.deleted_at is not None else user
    return db.scalar(select(User).where(func.lower(User.email) == login.lower(), User.deleted_at.is_(None)))


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = get_by_username(db, username or ADMIN_USERNAME)
    if not user or user.deleted_at is not None or not user.is_active or not check_password(password, user.password_hash):
        return None
    return user


def active_admin_count(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count(User.id)).where(
                User.role == UserRole.ADMIN.value, User.is_active.is_(True)
            )
        )
        or 0
    )


def ensure_bootstrap_users(db: Session) -> list[tuple[str, str]]:
    """First run only (empty users table): create `admin` + one owner per roster person.

    The admin keeps today's ADMIN_PASSWORD. Owners get a random temporary password and
    must change it on first login. Returns (username, temp_password) for new owners.
    """
    if db.scalar(select(func.count(User.id))):
        return []
    admin = User(
        username=ADMIN_USERNAME,
        display_name="Administrator",
        email=settings.admin_recovery_email or None,
        password_hash=hash_password(settings.admin_password),
        role=UserRole.ADMIN.value,
        is_active=True,
        must_change_password=False,
    )
    db.add(admin)
    db.flush()
    audit_svc.log_audit(
        db, entity_type="user", entity_id=admin.id, action="create",
        user_label="system", after=user_snapshot(admin),
    )

    flats_by_number = {f.number: f for f in db.scalars(select(Flat)).all()}
    owners: dict[str, list[Flat]] = {}
    names: dict[str, str] = {}
    for profile in FLAT_ROSTER:
        flat = flats_by_number.get(profile.number)
        if not flat:
            continue
        uname = profile.username or owner_username(profile.owner_name)
        names[uname] = profile.owner_name
        owners.setdefault(uname, []).append(flat)

    created: list[tuple[str, str]] = []
    for uname, flats in owners.items():
        temp = generate_temp_password()
        user = User(
            username=uname,
            display_name=names[uname],
            password_hash=hash_password(temp),
            role=UserRole.OWNER.value,
            is_active=True,
            must_change_password=True,
        )
        user.flats = flats
        db.add(user)
        db.flush()
        audit_svc.log_audit(
            db, entity_type="user", entity_id=user.id, action="create",
            user_label="system", after=user_snapshot(user),
        )
        created.append((uname, temp))
    if created:
        _record_initial_passwords(created, owners)
    return created


def _record_initial_passwords(created: list[tuple[str, str]], owners: dict[str, list[Flat]]) -> None:
    path = initial_passwords_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = format_ist_stamp()
    lines = [
        f"# Nest5 owner accounts created {stamp}.",
        "# Temporary passwords — each owner must change theirs at first login.",
        "# Delete this file once everyone has logged in. Admin can reset any password at /admin/users.",
    ]
    for uname, temp in created:
        flats = ", ".join(f.number for f in owners[uname])
        lines.append(f"{uname}\t{temp}\tflats {flats}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[nest5] Created owner logins; temporary passwords saved to {path}", flush=True)
    for line in lines[3:]:
        print("   " + line, flush=True)


def set_password(user: User, new_password: str, *, must_change: bool = False) -> None:
    user.password_hash = hash_password(new_password)
    user.must_change_password = must_change
    user.password_changed_at = datetime.utcnow()


def _token_digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_reset_token(db: Session, user: User) -> str:
    raw = secrets.token_urlsafe(32)
    now = datetime.utcnow()
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_token_digest(raw),
            created_at=now,
            expires_at=now + timedelta(minutes=settings.reset_token_ttl_minutes),
        )
    )
    db.flush()
    return raw


def lookup_reset_token(db: Session, raw: str) -> PasswordResetToken | None:
    """Return the token row if it is unused and unexpired."""
    if not raw:
        return None
    tok = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == _token_digest(raw)))
    if not tok or tok.used_at is not None or tok.expires_at < datetime.utcnow():
        return None
    if not tok.user or not tok.user.is_active or tok.user.deleted_at is not None:
        return None
    return tok


def consume_reset_token(db: Session, tok: PasswordResetToken) -> None:
    now = datetime.utcnow()
    for other in db.scalars(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == tok.user_id, PasswordResetToken.used_at.is_(None)
        )
    ).all():
        other.used_at = now
    tok.used_at = now


def recovery_address(user: User) -> str | None:
    """Admins always recover through ADMIN_RECOVERY_EMAIL; owners use their own email."""
    if user.role == UserRole.ADMIN.value:
        return settings.admin_recovery_email or user.email
    return user.email


# ---- soft delete ------------------------------------------------------------------------------


def soft_delete(db: Session, user: User, actor: str) -> None:
    """Hide the account and block login. Nothing is removed: the row, flat links, payments, submissions,
    audit entries and the username all stay. Open password-reset links are retired (not deleted)."""
    now = datetime.utcnow()
    user.deleted_at, user.deleted_by, user.is_active = now, actor, False
    for tok in db.scalars(
        select(PasswordResetToken).where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
    ).all():
        tok.used_at = now
    db.flush()


def restore(db: Session, user: User) -> str:
    """Bring the same account back (same role and flats) on a new temporary password."""
    from app.services.auth import generate_temp_password

    temp = generate_temp_password()
    user.deleted_at, user.deleted_by, user.is_active = None, None, True
    set_password(user, temp, must_change=True)
    db.flush()
    return temp
