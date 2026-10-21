"""Create (or update) a tenant login linked to one flat, from the command line.

A tenant only sees that flat's pending payments, and only the payment sections enabled for them
(Maintenance by default). Change the sections any time under Admin > Users.

Usage (from the nest5-ledger folder, venv active):
    python scripts/create_tenant.py --flat 101                       # user 'tenant101'
    python scripts/create_tenant.py --flat 101 --username tenant101 --name "Tenant, Flat 101" \\
        --sections maintenance,corpus
Prints a one-time temporary password; the tenant must change it at first login.
Running it again for an existing username re-links the flat, resets the temporary password and
re-activates the account (use --keep-password to leave the password alone).
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Flat, User, UserRole  # noqa: E402
from app.services import audit as audit_svc  # noqa: E402
from app.services import tenant as tenant_svc  # noqa: E402
from app.services import users as users_svc  # noqa: E402
from app.services.auth import generate_temp_password  # noqa: E402
from app.services.flats import sync_flat_roster  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Nest5 tenant login.")
    parser.add_argument("--flat", required=True, help="Flat number, e.g. 101")
    parser.add_argument("--username", help="Default: tenant<flat>")
    parser.add_argument("--name", help="Display name")
    parser.add_argument("--email", help="For password reset emails")
    parser.add_argument("--sections", default="maintenance", help="Comma list of: " + ", ".join(tenant_svc.SECTIONS))
    parser.add_argument("--keep-password", action="store_true", help="Do not reset the password of an existing user")
    args = parser.parse_args()

    username = (args.username or f"tenant{args.flat}").strip().lower()
    sections = tenant_svc.parse(args.sections)

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        sync_flat_roster(db)
        flat = db.scalar(select(Flat).where(Flat.number == args.flat))
        if not flat:
            print(f"No flat '{args.flat}'.")
            return 1
        user = users_svc.get_by_username(db, username)
        if user and user.role == UserRole.ADMIN.value:
            print(f"'{username}' is an admin account; pick another username.")
            return 1
        created = user is None
        if created:
            user = User(username=username, password_hash="", role=UserRole.TENANT.value)
            db.add(user)
        before = None if created else users_svc.user_snapshot(user)
        user.role = UserRole.TENANT.value
        user.is_active = True
        user.display_name = args.name or user.display_name or f"Tenant, Flat {flat.number}"
        if args.email:
            user.email = args.email
        user.flats = [flat]
        user.tenant_payments = tenant_svc.serialize(sections)
        temp = None
        if created or not args.keep_password:
            temp = generate_temp_password()
            users_svc.set_password(user, temp, must_change=True)
        db.flush()
        audit_svc.log_audit(
            db, entity_type="user", entity_id=user.id, action="create" if created else "role_change", user_label="cli",
            before=before, after={**users_svc.user_snapshot(user), "via": "scripts/create_tenant.py"},
        )
        db.commit()
        print(f"{'Created' if created else 'Updated'} tenant '{username}' for flat {flat.number}; sees: {', '.join(sections)}.")
        if temp:
            print(f"Temporary password (shown once, must be changed at first login): {temp}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
