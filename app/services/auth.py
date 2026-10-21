import base64
import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request

from app.config import settings

# PBKDF2 ships with every CPython build (scrypt depends on the OpenSSL Python was built with).
_PBKDF2_ITERATIONS = 390_000
_SCHEME = "pbkdf2_sha256"


def verify_password(password: str) -> bool:
    return password == settings.admin_password


def require_auth(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Not authenticated")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return "$".join(
        [
            _SCHEME,
            str(_PBKDF2_ITERATIONS),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def check_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, iterations, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != _SCHEME:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), base64.b64decode(salt_b64), int(iterations)
    )
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


def generate_temp_password() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))


MIN_PASSWORD_LENGTH = 8


def password_problem(new_password: str, confirm: str) -> str | None:
    if len(new_password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if new_password != confirm:
        return "Passwords do not match."
    return None
