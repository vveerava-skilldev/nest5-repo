import json
from contextvars import ContextVar
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog

# Set per request by the access guard so existing log_audit calls record who acted.
current_user_label: ContextVar[str | None] = ContextVar("current_user_label", default=None)


def _default_serializer(obj: Any) -> str:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "name"):  # enum
        return obj.name
    return str(obj)


def snapshot(data: Any) -> str | None:
    if data is None:
        return None
    if hasattr(data, "__dict__"):
        payload = {
            k: v
            for k, v in data.__dict__.items()
            if not k.startswith("_")
        }
    else:
        payload = data
    return json.dumps(payload, default=_default_serializer)


def log_audit(
    db: Session,
    *,
    entity_type: str,
    entity_id: int | None,
    action: str,
    user_label: str | None = None,
    before: Any = None,
    after: Any = None,
) -> AuditLog:
    entry = AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        user_label=user_label or current_user_label.get() or "admin",
        before_json=snapshot(before),
        after_json=snapshot(after),
    )
    db.add(entry)
    return entry
