import os
import tempfile

import pytest

# Never let the test suite touch the real nest5.db, instance/ or backups/ folders.
_TMP = tempfile.mkdtemp(prefix="nest5-tests-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/test.db")
os.environ.setdefault("INSTANCE_DIR", os.path.join(_TMP, "instance"))
os.environ.setdefault("BACKUP_DIR", os.path.join(_TMP, "backups"))
for _var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
    os.environ[_var] = ""

ADMIN_PASSWORD = "nest5admin"
OWNER_PASSWORD = "owner-pass-123"


@pytest.fixture
def seeded_db():
    from app.database import SessionLocal
    from app.services import users as users_svc
    from scripts.seed import seed

    seed()
    db = SessionLocal()
    users_svc.ensure_bootstrap_users(db)
    db.commit()
    yield db
    db.close()


def _set_password(db, username, password, *, must_change=False, active=True):
    from app.services import users as users_svc

    user = users_svc.get_by_username(db, username)
    users_svc.set_password(user, password, must_change=must_change)
    user.is_active = active
    db.commit()
    return user


@pytest.fixture
def set_password():
    return _set_password


def make_client(username: str, password: str):
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert resp.status_code == 303, resp.text
    return client


@pytest.fixture
def admin_client(seeded_db):
    _set_password(seeded_db, "admin", ADMIN_PASSWORD)
    return make_client("admin", ADMIN_PASSWORD)


@pytest.fixture
def owner_client(seeded_db):
    _set_password(seeded_db, "asrk.sastry", OWNER_PASSWORD)
    return make_client("asrk.sastry", OWNER_PASSWORD)
