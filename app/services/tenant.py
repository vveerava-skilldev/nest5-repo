"""Tenants: a login linked to one flat that only sees the payment sections the admin switched on.

Maintenance is the default. Corpus, projects and expenses can be enabled or disabled per tenant
from Admin > Users at any time; the access guard enforces it server-side.
"""
from app.models import User, UserRole

SECTIONS = ("maintenance", "corpus", "projects", "expenses")
LABELS = {
    "maintenance": "Maintenance",
    "corpus": "Corpus",
    "projects": "Projects",
    "expenses": "Expenses",
}
DEFAULT_SECTIONS = ("maintenance",)
REMINDER_KINDS = {"maintenance": "Maintenance", "corpus": "Corpus", "projects": "Project"}

BASE_READ = {
    "/payments",
    "/payments/maintenance",
    "/maintenance",
    "/account/password",
    "/upi/qr.svg",
    "/owner/submissions",
}
BASE_WRITE = {"/account/password", "/logout", "/owner/maintenance", "/owner/cancel-requests"}
SECTION_READ = {
    "maintenance": set(),
    "corpus": {
        "/payments/corpus", "/corpus", "/corpus/campaigns", "/corpus/campaigns/{campaign_id}",
        "/corpus/goals/{campaign_id}",
    },
    "projects": {"/payments/projects", "/projects", "/projects/{project_id}"},
    "expenses": {"/payments/expenses", "/expenses"},
}
SECTION_WRITE = {
    "maintenance": set(),
    "corpus": {"/owner/corpus/{item_id}/pay", "/corpus/campaigns/{campaign_id}/pay", "/corpus/pay"},
    "projects": {"/owner/projects/pay"},
    "expenses": {"/owner/expenses", "/owner/expenses/{expense_id}/edit"},
}


def parse(value: str | None) -> list[str]:
    chosen = {s.strip() for s in (value or "").split(",")}
    return [s for s in SECTIONS if s in chosen] or list(DEFAULT_SECTIONS)


def serialize(sections) -> str:
    return ",".join(s for s in SECTIONS if s in set(sections)) or ",".join(DEFAULT_SECTIONS)


def sections_for(user: User) -> list[str]:
    return parse(user.tenant_payments)


def is_tenant_role(role: str | None) -> bool:
    return role == UserRole.TENANT.value


def may_access(sections, method: str, path: str) -> bool:
    chosen = set(sections)
    if method in ("GET", "HEAD"):
        allowed = set(BASE_READ).union(*(SECTION_READ[s] for s in chosen))
    else:
        allowed = set(BASE_WRITE).union(*(SECTION_WRITE[s] for s in chosen))
    return path in allowed
