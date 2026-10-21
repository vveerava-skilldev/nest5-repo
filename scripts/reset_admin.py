"""Set a new password for the admin account from the command line (local recovery).

Usage (from the nest5-ledger folder, venv active):
    python scripts/reset_admin.py                 # prompts for the new password
    python scripts/reset_admin.py --username admin --password 'new-secret'
"""
import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import User, UserRole  # noqa: E402
from app.services import audit as audit_svc  # noqa: E402
from app.services import users as users_svc  # noqa: E402
from app.services.auth import MIN_PASSWORD_LENGTH  # noqa: E402
from app.services.flats import sync_flat_roster  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset the Nest5 admin password.")
    parser.add_argument("--username", default=users_svc.ADMIN_USERNAME)
    parser.add_argument("--password", help="New password (omit to be prompted)")
    args = parser.parse_args()

    password = args.password
    if not password:
        password = getpass.getpass(f"New password for '{args.username}': ")
        if password != getpass.getpass("Repeat new password: "):
            print("Passwords do not match.")
            return 1
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        return 1

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        sync_flat_roster(db)
        users_svc.ensure_bootstrap_users(db)
        user = users_svc.get_by_username(db, args.username)
        action = "password_reset"
        if not user:
            user = User(
                username=args.username.strip().lower(),
                display_name="Administrator",
                role=UserRole.ADMIN.value,
                password_hash="",
            )
            db.add(user)
            action = "create"
        before = users_svc.user_snapshot(user) if user.id else None
        user.role = UserRole.ADMIN.value
        user.is_active = True
        users_svc.set_password(user, password)
        db.flush()
        audit_svc.log_audit(
            db, entity_type="user", entity_id=user.id, action=action, user_label="cli",
            before=before, after={**users_svc.user_snapshot(user), "via": "scripts/reset_admin.py"},
        )
        db.commit()
        print(f"Password updated for admin user '{user.username}'. Sign in at /login.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
