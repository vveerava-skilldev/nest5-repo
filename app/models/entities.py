import enum
from datetime import date, datetime

from sqlalchemy import (
    and_,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    or_,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class FundType(str, enum.Enum):
    MAINTENANCE = "maintenance"
    CORPUS = "corpus"


class PayeeSnapshot:
    """v7.3: the account a payment was made to, copied from the Payment settings in effect when it was
    recorded. Never re-derived from the live settings, so later changes cannot rewrite history."""

    payee_vpa: Mapped[str | None] = mapped_column(String(120), nullable=True)
    payee_mobile: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payee_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    payee_settings_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_detail_changes.id"), nullable=True
    )


class Flat(Base):
    __tablename__ = "flats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    contact_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    owner_notes: Mapped[str | None] = mapped_column(String(255), nullable=True)

    maintenance_payments: Mapped[list["MaintenancePayment"]] = relationship(back_populates="flat")
    corpus_payments: Mapped[list["CorpusPayment"]] = relationship(back_populates="flat")
    project_payments: Mapped[list["ProjectPayment"]] = relationship(back_populates="flat")
    expenses_paid: Mapped[list["Expense"]] = relationship(back_populates="paid_by_flat")


class LedgerPeriod(Base):
    __tablename__ = "ledger_periods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    month: Mapped[int] = mapped_column(Integer, index=True)
    opening_fund_balance: Mapped[int] = mapped_column(Integer, default=0)
    closing_fund_balance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    corpus_opening: Mapped[int] = mapped_column(Integer, default=0)
    corpus_closing: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # v7.3: True while this (auto-created) month's opening balance follows the previous month's closing.
    opening_carried: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    maintenance_payments: Mapped[list["MaintenancePayment"]] = relationship(back_populates="period")


class MaintenanceFeeRevision(Base):
    __tablename__ = "maintenance_fee_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[str] = mapped_column(String(80), default="admin")


class MaintenancePayment(PayeeSnapshot, Base):
    __tablename__ = "maintenance_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    flat_id: Mapped[int] = mapped_column(ForeignKey("flats.id"))
    period_id: Mapped[int] = mapped_column(ForeignKey("ledger_periods.id"))
    amount: Mapped[int] = mapped_column(Integer, default=2000)
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payment_mode: Mapped[str | None] = mapped_column(String(30), nullable=True)
    submitted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    flat: Mapped["Flat"] = relationship(back_populates="maintenance_payments")
    period: Mapped["LedgerPeriod"] = relationship(back_populates="maintenance_payments")
    submitted_by: Mapped["User | None"] = relationship()


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    description: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(100), default="general")
    amount: Mapped[int] = mapped_column(Integer)
    expense_date: Mapped[date] = mapped_column(Date)
    paid_by_flat_id: Mapped[int | None] = mapped_column(ForeignKey("flats.id"), nullable=True)
    period_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_periods.id"), nullable=True)
    is_settled: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # None / 'approved' = counts as before; 'rejected' = kept as a record but never counted anywhere.
    status: Mapped[str | None] = mapped_column(String(20), nullable=True, default="approved")
    rejection_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(80), nullable=True)

    paid_by_flat: Mapped["Flat | None"] = relationship(back_populates="expenses_paid")
    submitted_by: Mapped["User | None"] = relationship()
    settlement: Mapped["Settlement | None"] = relationship(back_populates="expense", uselist=False)

    @property
    def is_rejected(self) -> bool:
        return self.status == "rejected"

    @classmethod
    def counted(cls):
        """SQL filter for expenses that count in totals, balances, settlements and exports."""
        return and_(cls.deleted_at.is_(None), or_(cls.status.is_(None), cls.status != "rejected"))


class ExpenseCategory(Base):
    """User-added expense categories (presets live in app/domain/expense_categories.py)."""

    __tablename__ = "expense_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_active: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    owner_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
    sort_order: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Settlement(Base):
    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    expense_id: Mapped[int] = mapped_column(ForeignKey("expenses.id"), unique=True)
    fund_type: Mapped[FundType] = mapped_column(Enum(FundType), default=FundType.MAINTENANCE)
    amount: Mapped[int] = mapped_column(Integer)
    settled_on: Mapped[date] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    expense: Mapped["Expense"] = relationship(back_populates="settlement")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_cost: Mapped[int] = mapped_column(Integer)
    share_per_flat: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    payments: Mapped[list["ProjectPayment"]] = relationship(back_populates="project")


class ProjectPayment(Base):
    __tablename__ = "project_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    flat_id: Mapped[int] = mapped_column(ForeignKey("flats.id"))
    amount: Mapped[int] = mapped_column(Integer)
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Sum of this share's contributions (kept in step by app.services.projects.sync).
    paid_amount: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)

    project: Mapped["Project"] = relationship(back_populates="payments")
    flat: Mapped["Flat"] = relationship(back_populates="project_payments")
    contributions: Mapped[list["ProjectContribution"]] = relationship(
        back_populates="share", order_by="ProjectContribution.paid_on"
    )

    @property
    def paid(self) -> int:
        if self.paid_amount is None:
            return self.amount if self.is_paid else 0
        return self.paid_amount

    @property
    def remaining(self) -> int:
        return max(self.amount - self.paid, 0)

    @property
    def status(self) -> str:
        if self.amount and self.paid >= self.amount:
            return "paid"
        return "part" if self.paid else "pending"


class ProjectContribution(PayeeSnapshot, Base):
    """One payment (full or part) towards a flat's project share."""

    __tablename__ = "project_contributions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_payment_id: Mapped[int] = mapped_column(ForeignKey("project_payments.id"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    flat_id: Mapped[int] = mapped_column(ForeignKey("flats.id"))
    amount: Mapped[int] = mapped_column(Integer)
    paid_on: Mapped[date] = mapped_column(Date)
    payment_mode: Mapped[str | None] = mapped_column(String(30), nullable=True)
    payment_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    submitted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    share: Mapped["ProjectPayment"] = relationship(back_populates="contributions")
    project: Mapped["Project"] = relationship()
    flat: Mapped["Flat"] = relationship()
    submitted_by: Mapped["User | None"] = relationship()


class CorpusPayment(PayeeSnapshot, Base):
    __tablename__ = "corpus_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    flat_id: Mapped[int] = mapped_column(ForeignKey("flats.id"))
    period_id: Mapped[int | None] = mapped_column(ForeignKey("ledger_periods.id"), nullable=True)
    amount: Mapped[int] = mapped_column(Integer)
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("corpus_campaigns.id"), nullable=True, index=True)
    payment_mode: Mapped[str | None] = mapped_column(String(30), nullable=True)
    submitted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    line_item_id: Mapped[int | None] = mapped_column(ForeignKey("corpus_line_items.id"), nullable=True)

    flat: Mapped["Flat"] = relationship(back_populates="corpus_payments")
    campaign: Mapped["CorpusCampaign | None"] = relationship(back_populates="payments")
    line_item: Mapped["CorpusLineItem | None"] = relationship(back_populates="payments")
    submitted_by: Mapped["User | None"] = relationship()


class CorpusCampaign(Base):
    """A corpus collection drive: target amount, per-flat shares, due date; paid in parts."""

    __tablename__ = "corpus_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_amount: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[str] = mapped_column(String(80), default="admin")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The open-ended "General corpus" (former one-off corpus line items); at most one.
    is_general: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)

    shares: Mapped[list["CorpusCampaignShare"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    payments: Mapped[list["CorpusPayment"]] = relationship(back_populates="campaign")
    line_items: Mapped[list["CorpusLineItem"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", order_by="CorpusLineItem.id"
    )


class CorpusCategory(Base):
    """Admin-managed corpus goal categories (Building repair, Lift, Painting, General, ...)."""

    __tablename__ = "corpus_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CorpusLineItem(Base):
    """Optional sub-goal under a corpus goal; payments may name the line item they are for."""

    __tablename__ = "corpus_line_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("corpus_campaigns.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    target_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    campaign: Mapped["CorpusCampaign"] = relationship(back_populates="line_items")
    payments: Mapped[list["CorpusPayment"]] = relationship(back_populates="line_item")


class CorpusCampaignShare(Base):
    __tablename__ = "corpus_campaign_shares"
    __table_args__ = (UniqueConstraint("campaign_id", "flat_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("corpus_campaigns.id"), index=True)
    flat_id: Mapped[int] = mapped_column(ForeignKey("flats.id"))
    share_amount: Mapped[int] = mapped_column(Integer)

    campaign: Mapped["CorpusCampaign"] = relationship(back_populates="shares")
    flat: Mapped["Flat"] = relationship()


class CalendarEvent(Base):
    """Admin-managed building event shown on the in-portal Calendar (meeting, AGM, vendor visit, ...)."""

    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    event_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    start_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    end_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    event_type: Mapped[str] = mapped_column(String(40), default="Other")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    visible_to_owners: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


class AppSetting(Base):
    """Small admin switches (key -> value), e.g. whether owners may see the building-wide payments page."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


class CancelRequest(Base):
    """Owner request to cancel a record they submitted; admin approves or rejects."""

    __tablename__ = "cancel_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_type: Mapped[str] = mapped_column(String(40), index=True)
    record_id: Mapped[int] = mapped_column(Integer, index=True)
    flat_id: Mapped[int | None] = mapped_column(ForeignKey("flats.id"), nullable=True)
    requested_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    summary: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    flat: Mapped["Flat | None"] = relationship()
    requested_by: Mapped["User"] = relationship()


class SubmissionReview(Base):
    """Admin has looked at one owner submission (one `owner_submit` audit entry); it leaves the Actions board."""

    __tablename__ = "submission_reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(40))
    record_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    reviewed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


class SubmissionRejection(PayeeSnapshot, Base):
    """An owner payment the admin rejected. The payment itself is removed from every count (same
    effect as an approved cancel); this row keeps what it was so the owner sees it as Rejected."""

    __tablename__ = "submission_rejections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_type: Mapped[str] = mapped_column(String(40), index=True)
    record_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flat_id: Mapped[int | None] = mapped_column(ForeignKey("flats.id"), nullable=True)
    submitted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    summary: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paid_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rejected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    rejected_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


class ScheduleFrequency(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"
    CUSTOM = "custom"


class Asset(Base):
    """Asset register entry (Lift, Generator, Water tank, ...); its recurring tasks are schedule rows."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    asset_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vendor_contact: Mapped[str | None] = mapped_column(String(255), nullable=True)
    install_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    warranty_expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    cost: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    tasks: Mapped[list["AssetMaintenanceSchedule"]] = relationship(back_populates="asset_ref")
    logs: Mapped[list["AssetMaintenanceLog"]] = relationship(back_populates="asset")


class AssetMaintenanceSchedule(Base):
    """A recurring task; `asset` keeps the name text, `asset_id` links it to the register."""

    __tablename__ = "asset_maintenance_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id"), nullable=True, index=True)
    asset: Mapped[str] = mapped_column(String(100))
    task: Mapped[str] = mapped_column(String(255))
    frequency: Mapped[ScheduleFrequency] = mapped_column(
        Enum(ScheduleFrequency), default=ScheduleFrequency.MONTHLY
    )
    custom_interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_done_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    asset_ref: Mapped["Asset | None"] = relationship(back_populates="tasks")


class AssetMaintenanceLog(Base):
    """One check/service done on an asset (optionally for one of its tasks)."""

    __tablename__ = "asset_maintenance_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("asset_maintenance_schedules.id"), nullable=True)
    done_on: Mapped[date] = mapped_column(Date)
    done_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[str | None] = mapped_column(String(80), nullable=True)

    asset: Mapped["Asset"] = relationship(back_populates="logs")
    task: Mapped["AssetMaintenanceSchedule | None"] = relationship()


class Complaint(Base):
    __tablename__ = "complaints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reported_on: Mapped[date] = mapped_column(Date)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="open")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    OWNER = "owner"
    TENANT = "tenant"


user_flats = Table(
    "user_flats",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
    Column("flat_id", Integer, ForeignKey("flats.id"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default=UserRole.OWNER.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dashboard_view: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Tenants only: comma list of payment sections the admin has switched on (default: maintenance).
    tenant_payments: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Soft delete: the row, flat links and every record that points here stay; the account is also inactive.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(80), nullable=True)

    flats: Mapped[list["Flat"]] = relationship(secondary=user_flats, order_by="Flat.number")

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(80), index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(40))
    user_label: Mapped[str] = mapped_column(String(80), default="admin")
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class RecurringTask(Base):
    """Admin-only recurring to-do (e.g. Watchman salary): every `interval_months` on `day_of_month`."""

    __tablename__ = "recurring_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    day_of_month: Mapped[int] = mapped_column(Integer)
    due_mode: Mapped[str] = mapped_column(String(10), default="by")  # "by" the day, or "before" it (due on the day)
    interval_months: Mapped[int] = mapped_column(Integer, default=1)
    start_year: Mapped[int] = mapped_column(Integer)
    start_month: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed_key: Mapped[str | None] = mapped_column(String(40), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[str] = mapped_column(String(80), default="admin")

    occurrences: Mapped[list["RecurringTaskOccurrence"]] = relationship(
        back_populates="task", order_by="RecurringTaskOccurrence.due_date"
    )


class RecurringTaskOccurrence(Base):
    """One month's instance of a recurring task; done when `done_on` is set. Never creates an expense."""

    __tablename__ = "recurring_task_occurrences"
    __table_args__ = (UniqueConstraint("task_id", "year", "month", name="uq_recurring_occurrence_month"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("recurring_tasks.id"), index=True)
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    done_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    done_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    task: Mapped["RecurringTask"] = relationship(back_populates="occurrences")


class PaymentDetailChange(Base):
    """Append-only history of the UPI ID / mobile / payee name shown on the Pay now sheet."""

    __tablename__ = "payment_detail_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)  # UTC
    changed_by: Mapped[str] = mapped_column(String(80), default="admin")
    is_initial: Mapped[bool] = mapped_column(Boolean, default=False)
    old_upi_vpa: Mapped[str | None] = mapped_column(String(120), nullable=True)
    new_upi_vpa: Mapped[str] = mapped_column(String(120))
    old_upi_mobile: Mapped[str | None] = mapped_column(String(20), nullable=True)
    new_upi_mobile: Mapped[str] = mapped_column(String(20))
    old_payee_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    new_payee_name: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(String(255))
    # Computed by the app at the moment of the change.
    fund_balance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    corpus_collected: Mapped[int | None] = mapped_column(Integer, nullable=True)
    projects_collected: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owed_back: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_balance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # What the admin typed for the real bank / UPI account balance, kept exactly as entered.
    bank_balance_entered: Mapped[str | None] = mapped_column(String(60), nullable=True)
