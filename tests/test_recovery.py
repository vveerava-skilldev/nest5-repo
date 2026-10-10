import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import PasswordResetToken
from app.services import mailer
from app.services import users as users_svc
from tests.conftest import ADMIN_PASSWORD, make_client


def _last_link() -> str:
    text = Path(mailer.outbox_path()).read_text(encoding="utf-8")
    return re.findall(r"(/reset-password\?token=[\w\-]+)", text)[-1]


def test_reset_token_works_once(seeded_db, set_password):
    user = set_password(seeded_db, "sastry", "old-password-1")
    user.email = "sastry@example.com"
    seeded_db.commit()

    client = TestClient(app)
    r = client.post("/forgot-password", data={"login": "sastry@example.com"})
    assert r.status_code == 200 and "outbox.log" in r.text
    outbox = Path(mailer.outbox_path()).read_text(encoding="utf-8")
    assert "to: sastry@example.com" in outbox
    link = _last_link()
    token = link.split("token=")[1]

    assert client.get(link).status_code == 200
    r = client.post("/reset-password", data={"token": token, "new_password": "short", "confirm_password": "short"})
    assert r.status_code == 400
    r = client.post(
        "/reset-password",
        data={"token": token, "new_password": "fresh-password-9", "confirm_password": "fresh-password-9"},
    )
    assert r.status_code == 200 and "Password updated" in r.text
    make_client("sastry", "fresh-password-9")

    # Single use.
    assert client.get(link).status_code == 400
    r = client.post(
        "/reset-password",
        data={"token": token, "new_password": "another-pass-1", "confirm_password": "another-pass-1"},
    )
    assert r.status_code == 400
    make_client("sastry", "fresh-password-9")


def test_reset_token_expires(seeded_db):
    user = users_svc.get_by_username(seeded_db, "sastry")
    raw = users_svc.create_reset_token(seeded_db, user)
    seeded_db.commit()
    assert users_svc.lookup_reset_token(seeded_db, raw) is not None
    tok = seeded_db.scalars(select(PasswordResetToken).order_by(PasswordResetToken.id.desc())).first()
    tok.expires_at = datetime.utcnow() - timedelta(seconds=1)
    seeded_db.commit()
    client = TestClient(app)
    assert client.get(f"/reset-password?token={raw}").status_code == 400
    r = client.post("/reset-password", data={"token": raw, "new_password": "whatever-123", "confirm_password": "whatever-123"})
    assert r.status_code == 400


def test_admin_recovery_goes_to_recovery_email(seeded_db, set_password):
    client = TestClient(app)
    r = client.post("/forgot-password", data={"admin_recovery": "1"})
    assert r.status_code == 200
    outbox = Path(mailer.outbox_path()).read_text(encoding="utf-8")
    last_entry = outbox.split("===== ")[-1]
    assert f"to: {settings.admin_recovery_email}" in last_entry
    assert settings.admin_recovery_email == "vveerava@gmail.com"
    token = _last_link().split("token=")[1]
    r = client.post(
        "/reset-password",
        data={"token": token, "new_password": "recovered-admin-1", "confirm_password": "recovered-admin-1"},
    )
    assert r.status_code == 200
    make_client("admin", "recovered-admin-1")
    set_password(seeded_db, "admin", ADMIN_PASSWORD)


def test_unknown_user_gets_same_message(seeded_db):
    client = TestClient(app)
    r = client.post("/forgot-password", data={"login": "nobody-here"})
    assert r.status_code == 200 and "If that account exists" in r.text


def test_cli_reset_admin(seeded_db, monkeypatch, set_password):
    from scripts import reset_admin

    monkeypatch.setattr(sys, "argv", ["reset_admin.py", "--password", "cli-admin-pass"])
    assert reset_admin.main() == 0
    make_client("admin", "cli-admin-pass")
    set_password(seeded_db, "admin", ADMIN_PASSWORD)
