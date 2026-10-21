"""What the admin has not set up yet. Admin pages hide whole sections for data that does not exist
and show a single small 'Add ...' action instead (owners and tenants keep their own rules)."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CorpusCampaign, CorpusCampaignShare, CorpusPayment, Expense, Project


def has_corpus_goals(db: Session) -> bool:
    """True when a real goal exists. An unused General corpus (no shares, no payments) does not count."""
    for c in db.scalars(select(CorpusCampaign)).all():
        if not c.is_general or c.target_amount:
            return True
        shares = db.scalar(
            select(func.coalesce(func.sum(CorpusCampaignShare.share_amount), 0)).where(
                CorpusCampaignShare.campaign_id == c.id
            )
        )
        paid = db.scalar(select(func.count()).select_from(CorpusPayment).where(CorpusPayment.campaign_id == c.id))
        if shares or paid:
            return True
    return False


def has_projects(db: Session) -> bool:
    return db.scalar(select(func.count()).select_from(Project).where(Project.deleted_at.is_(None))) > 0


def has_expenses(db: Session) -> bool:
    return db.scalar(select(func.count()).select_from(Expense).where(Expense.counted())) > 0


def flags(db: Session) -> dict:
    return {
        "goals": has_corpus_goals(db),
        "projects": has_projects(db),
        "expenses": has_expenses(db),
    }


ADD_LINKS = (
    ("goals", "Add corpus goal", "/corpus?add=goal#add-goal"),
    ("projects", "Add project", "/projects?add=project#add-project"),
    ("expenses", "Add expense", "/expenses?add=expense#add-expense"),
)


def missing_actions(db: Session) -> list[dict]:
    f = flags(db)
    return [{"label": label, "href": href} for key, label, href in ADD_LINKS if not f[key]]
