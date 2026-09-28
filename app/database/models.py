"""SQLAlchemy ORM models.

Enums are stored as plain strings so the SQLite file stays human-readable and
the dashboard can filter on them directly.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.utils.helpers import from_json, to_json, utcnow


# --------------------------------------------------------------------------- enums
class CampaignStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"


class LeadStatus(StrEnum):
    NEW = "NEW"
    RESEARCHED = "RESEARCHED"
    READY = "READY"
    EMAIL_SENT = "EMAIL_SENT"
    FOLLOWUP_DUE = "FOLLOWUP_DUE"
    REPLIED = "REPLIED"
    INTERESTED = "INTERESTED"
    NOT_INTERESTED = "NOT_INTERESTED"
    WRONG_PERSON = "WRONG_PERSON"
    OUT_OF_OFFICE = "OUT_OF_OFFICE"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    BOUNCED = "BOUNCED"
    COMPLETED = "COMPLETED"


class MessageKind(StrEnum):
    INITIAL = "INITIAL"
    FOLLOWUP = "FOLLOWUP"
    REPLY = "REPLY"  # reply to a lead's message (drafted for / by a human)
    REFERRAL_REQUEST = "REFERRAL_REQUEST"  # "could you point me to the right person?"


class MessageStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    SENDING = "SENDING"
    SENT = "SENT"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ReplyCategory(StrEnum):
    INTERESTED = "INTERESTED"
    MORE_INFORMATION = "MORE_INFORMATION"
    QUESTION = "QUESTION"
    NOT_INTERESTED = "NOT_INTERESTED"
    WRONG_PERSON = "WRONG_PERSON"
    OUT_OF_OFFICE = "OUT_OF_OFFICE"
    UNSUBSCRIBE = "UNSUBSCRIBE"
    UNCLEAR = "UNCLEAR"
    BOUNCE = "BOUNCE"  # detected deterministically, never by the AI


class SuppressionReason(StrEnum):
    UNSUBSCRIBE = "UNSUBSCRIBE"
    BOUNCED = "BOUNCED"
    MANUAL = "MANUAL"


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


# --------------------------------------------------------------------------- campaign
class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    company_name: Mapped[str] = mapped_column(String(120), nullable=False)
    sender_name: Mapped[str] = mapped_column(String(120), default="")
    product_name: Mapped[str] = mapped_column(String(160), nullable=False)
    product_description: Mapped[str] = mapped_column(Text, default="")
    value_proposition: Mapped[str] = mapped_column(Text, default="")
    target_industry: Mapped[str] = mapped_column(String(160), default="")
    target_company_size: Mapped[str] = mapped_column(String(80), default="")
    target_locations: Mapped[str] = mapped_column(String(255), default="")
    target_roles: Mapped[str] = mapped_column(String(255), default="")
    tone: Mapped[str] = mapped_column(String(120), default="friendly, professional, concise")
    call_to_action: Mapped[str] = mapped_column(String(255), default="")

    max_emails_per_day: Mapped[int] = mapped_column(Integer, default=20)
    max_followups: Mapped[int] = mapped_column(Integer, default=2)
    followup_1_days: Mapped[int] = mapped_column(Integer, default=3)
    followup_2_days: Mapped[int] = mapped_column(Integer, default=5)
    auto_send: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[str] = mapped_column(String(16), default=CampaignStatus.DRAFT, index=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    icp_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    leads: Mapped[list[Lead]] = relationship(back_populates="campaign", cascade="all, delete-orphan")

    @property
    def icp(self) -> dict[str, Any]:
        return from_json(self.icp_json, {}) or {}

    @icp.setter
    def icp(self, value: dict[str, Any] | None) -> None:
        self.icp_json = to_json(value) if value else None

    @property
    def followup_intervals(self) -> list[int]:
        return [self.followup_1_days, self.followup_2_days]

    def __repr__(self) -> str:
        return f"<Campaign {self.id} {self.name!r} {self.status}>"


# --------------------------------------------------------------------------- lead
class Lead(TimestampMixin, Base):
    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("campaign_id", "email_normalized", name="uq_lead_campaign_email"),
        Index("ix_lead_campaign_status", "campaign_id", "status"),
        Index("ix_lead_campaign_domain", "campaign_id", "domain"),
        Index("ix_lead_company_contact", "campaign_id", "company_key", "contact_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)

    company_name: Mapped[str] = mapped_column(String(160), nullable=False)
    website: Mapped[str] = mapped_column(String(255), default="")
    domain: Mapped[str] = mapped_column(String(160), default="")
    industry: Mapped[str] = mapped_column(String(120), default="")
    location: Mapped[str] = mapped_column(String(160), default="")
    contact_name: Mapped[str] = mapped_column(String(160), default="")
    contact_role: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    email_normalized: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    company_key: Mapped[str] = mapped_column(String(160), default="")
    contact_key: Mapped[str] = mapped_column(String(160), default="")
    source: Mapped[str] = mapped_column(String(40), default="manual")
    notes: Mapped[str] = mapped_column(Text, default="")

    research_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    personalization_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    research_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    researched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    research_error: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default=LeadStatus.NEW, index=True)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_followup_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    followup_count: Mapped[int] = mapped_column(Integer, default=0)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    reply_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    human_review_required: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    campaign: Mapped[Campaign] = relationship(back_populates="leads")
    messages: Mapped[list[EmailMessage]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", order_by="EmailMessage.id"
    )
    replies: Mapped[list[Reply]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", order_by="Reply.id"
    )

    @property
    def research(self) -> dict[str, Any]:
        return from_json(self.research_json, {}) or {}

    @research.setter
    def research(self, value: dict[str, Any] | None) -> None:
        self.research_json = to_json(value) if value else None

    def __repr__(self) -> str:
        return f"<Lead {self.id} {self.email_normalized} {self.status}>"


# --------------------------------------------------------------------------- messages
class EmailMessage(TimestampMixin, Base):
    """An outbound email (draft -> approved -> sent)."""

    __tablename__ = "email_messages"
    __table_args__ = (Index("ix_message_status_scheduled", "status", "scheduled_for"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)

    kind: Mapped[str] = mapped_column(String(20), default=MessageKind.INITIAL)
    sequence: Mapped[int] = mapped_column(Integer, default=0)  # 0 = initial, 1..n = follow-ups
    to_email: Mapped[str] = mapped_column(String(254), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    personalization_reason: Mapped[str] = mapped_column(Text, default="")
    quality_flags: Mapped[str] = mapped_column(Text, default="")  # comma-separated warnings
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=True)

    status: Mapped[str] = mapped_column(String(20), default=MessageStatus.PENDING_APPROVAL, index=True)
    approved_by: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "human" | "auto"
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    gmail_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    rfc_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reply_id: Mapped[int | None] = mapped_column(ForeignKey("replies.id"), nullable=True)

    lead: Mapped[Lead] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        return f"<EmailMessage {self.id} {self.kind} {self.status}>"


class Reply(Base):
    """An inbound message on a tracked thread, plus its classification."""

    __tablename__ = "replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    gmail_message_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    gmail_thread_id: Mapped[str] = mapped_column(String(64), index=True)
    rfc_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    from_email: Mapped[str] = mapped_column(String(254), default="")
    subject: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(Text, default="")  # latest message only, quotes stripped
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    category: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_human: Mapped[bool] = mapped_column(Boolean, default=False)
    return_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    classified_by: Mapped[str | None] = mapped_column(String(20), nullable=True)  # "ai" | "rule"
    action_taken: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    lead: Mapped[Lead] = relationship(back_populates="replies")


class SuppressionEntry(Base):
    """Addresses/domains that must never be contacted again."""

    __tablename__ = "suppression_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True)
    domain: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(String(20), default=SuppressionReason.MANUAL)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProcessedMessage(Base):
    """Idempotency log for Gmail messages the inbox monitor has handled."""

    __tablename__ = "processed_messages"

    gmail_message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    gmail_thread_id: Mapped[str] = mapped_column(String(64), default="")
    outcome: Mapped[str] = mapped_column(String(40), default="")
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    """Runtime key/value state (kill switch, last inbox check, ...)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AICacheEntry(Base):
    """Cached AI responses keyed by a hash of provider+model+prompt."""

    __tablename__ = "ai_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(40), default="")
    response: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AIUsage(Base):
    """One row per AI call (or cache hit) - powers the cost panel in the dashboard."""

    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(80), default="")
    purpose: Mapped[str] = mapped_column(String(40), default="", index=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
