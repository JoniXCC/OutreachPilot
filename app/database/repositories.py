"""Query helpers. Services use these instead of writing SQL inline."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.database.models import (
    AppSetting, Campaign, CampaignStatus, EmailMessage, Lead, LeadStatus, MessageKind,
    MessageStatus, ProcessedMessage, Reply, ReplyCategory, SuppressionEntry, SuppressionReason,
)
from app.utils.helpers import email_domain, normalize_email, utcnow


def start_of_utc_day(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------- campaigns
class CampaignRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def get(self, campaign_id: int) -> Campaign | None:
        return self.s.get(Campaign, campaign_id)

    def get_by_name(self, name: str) -> Campaign | None:
        return self.s.scalar(select(Campaign).where(Campaign.name == name))

    def list(self, status: str | None = None) -> Sequence[Campaign]:
        stmt = select(Campaign).order_by(Campaign.created_at.desc())
        if status:
            stmt = stmt.where(Campaign.status == status)
        return self.s.scalars(stmt).all()

    def active(self) -> Sequence[Campaign]:
        return self.list(CampaignStatus.ACTIVE)


# --------------------------------------------------------------------------- leads
class LeadRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def get(self, lead_id: int) -> Lead | None:
        return self.s.get(Lead, lead_id)

    def list(
        self,
        campaign_id: int | None = None,
        statuses: Sequence[str] | None = None,
        search: str | None = None,
        human_review: bool | None = None,
    ) -> Sequence[Lead]:
        stmt = select(Lead).order_by(Lead.id.desc())
        if campaign_id:
            stmt = stmt.where(Lead.campaign_id == campaign_id)
        if statuses:
            stmt = stmt.where(Lead.status.in_(list(statuses)))
        if human_review is not None:
            stmt = stmt.where(Lead.human_review_required == human_review)
        if search:
            like = f"%{search.lower()}%"
            stmt = stmt.where(or_(
                func.lower(Lead.company_name).like(like),
                func.lower(Lead.contact_name).like(like),
                Lead.email_normalized.like(like),
            ))
        return self.s.scalars(stmt).all()

    def find_by_email(self, campaign_id: int, email: str) -> Lead | None:
        return self.s.scalar(select(Lead).where(
            Lead.campaign_id == campaign_id, Lead.email_normalized == normalize_email(email)
        ))

    def find_by_domain(self, campaign_id: int, domain: str) -> Lead | None:
        if not domain:
            return None
        return self.s.scalar(select(Lead).where(
            Lead.campaign_id == campaign_id, Lead.domain == domain
        ).limit(1))

    def find_by_company_contact(self, campaign_id: int, company_key: str, contact_key: str) -> Lead | None:
        if not company_key or not contact_key:
            return None
        return self.s.scalar(select(Lead).where(
            Lead.campaign_id == campaign_id,
            Lead.company_key == company_key,
            Lead.contact_key == contact_key,
        ).limit(1))

    def find_research_donor(self, domain: str, exclude_id: int) -> Lead | None:
        """Another lead (any campaign) whose company was already researched."""
        if not domain:
            return None
        return self.s.scalar(select(Lead).where(
            Lead.domain == domain, Lead.id != exclude_id,
            Lead.researched_at.is_not(None), Lead.research_json.is_not(None),
        ).limit(1))

    def get_by_thread(self, thread_id: str) -> Lead | None:
        return self.s.scalar(select(Lead).where(Lead.gmail_thread_id == thread_id))

    def needing_research(self, limit: int, campaign_id: int | None = None) -> Sequence[Lead]:
        stmt = (select(Lead).join(Campaign)
                .where(Lead.status == LeadStatus.NEW, Lead.researched_at.is_(None),
                       Lead.do_not_contact.is_(False),
                       Campaign.status.in_([CampaignStatus.ACTIVE, CampaignStatus.DRAFT]))
                .order_by(Lead.id).limit(limit))
        if campaign_id:
            stmt = stmt.where(Lead.campaign_id == campaign_id)
        return self.s.scalars(stmt).all()

    def needing_initial_email(self, limit: int, campaign_id: int | None = None) -> Sequence[Lead]:
        stmt = (select(Lead).join(Campaign)
                .where(Lead.status.in_([LeadStatus.NEW, LeadStatus.RESEARCHED]),
                       Lead.do_not_contact.is_(False),
                       Campaign.status == CampaignStatus.ACTIVE)
                .order_by(Lead.id).limit(limit))
        if campaign_id:
            stmt = stmt.where(Lead.campaign_id == campaign_id)
        return self.s.scalars(stmt).all()

    def due_for_followup(self, now: datetime) -> Sequence[Lead]:
        return self.s.scalars(
            select(Lead).join(Campaign)
            .where(Lead.status.in_([LeadStatus.EMAIL_SENT, LeadStatus.OUT_OF_OFFICE]),
                   Lead.next_followup_at.is_not(None), Lead.next_followup_at <= now,
                   Lead.do_not_contact.is_(False),
                   Campaign.status == CampaignStatus.ACTIVE)
            .order_by(Lead.next_followup_at)
        ).all()

    def tracked_thread_ids(self) -> set[str]:
        """Gmail threads started by this app whose replies we still care about."""
        rows = self.s.scalars(select(Lead.gmail_thread_id).where(
            Lead.gmail_thread_id.is_not(None),
            Lead.status.not_in([LeadStatus.COMPLETED]),
        )).all()
        return {r for r in rows if r}

    def status_counts(self, campaign_id: int | None = None) -> dict[str, int]:
        stmt = select(Lead.status, func.count()).group_by(Lead.status)
        if campaign_id:
            stmt = stmt.where(Lead.campaign_id == campaign_id)
        return {status: count for status, count in self.s.execute(stmt).all()}


# --------------------------------------------------------------------------- messages
class MessageRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def get(self, message_id: int) -> EmailMessage | None:
        return self.s.get(EmailMessage, message_id)

    def by_status(self, statuses: Sequence[str], campaign_id: int | None = None) -> Sequence[EmailMessage]:
        stmt = select(EmailMessage).where(EmailMessage.status.in_(list(statuses))).order_by(EmailMessage.id)
        if campaign_id:
            stmt = stmt.where(EmailMessage.campaign_id == campaign_id)
        return self.s.scalars(stmt).all()

    def pending_approval(self, campaign_id: int | None = None) -> Sequence[EmailMessage]:
        return self.by_status([MessageStatus.PENDING_APPROVAL], campaign_id)

    def ready_to_send(self, now: datetime) -> Sequence[EmailMessage]:
        return self.s.scalars(
            select(EmailMessage)
            .where(EmailMessage.status == MessageStatus.APPROVED,
                   or_(EmailMessage.scheduled_for.is_(None), EmailMessage.scheduled_for <= now))
            .order_by(EmailMessage.scheduled_for.is_(None), EmailMessage.scheduled_for, EmailMessage.id)
        ).all()

    def open_drafts_for_lead(self, lead_id: int, kinds: Sequence[str] | None = None) -> Sequence[EmailMessage]:
        stmt = select(EmailMessage).where(
            EmailMessage.lead_id == lead_id,
            EmailMessage.status.in_([MessageStatus.PENDING_APPROVAL, MessageStatus.APPROVED]),
        )
        if kinds:
            stmt = stmt.where(EmailMessage.kind.in_(list(kinds)))
        return self.s.scalars(stmt).all()

    def cancel_open_for_lead(self, lead_id: int, kinds: Sequence[str] | None = None, reason: str = "") -> int:
        """Cancel drafts/approved-but-unsent messages for a lead. Returns count."""
        count = 0
        for msg in self.open_drafts_for_lead(lead_id, kinds):
            msg.status = MessageStatus.CANCELLED
            msg.error = reason[:500] or None
            count += 1
        return count

    def claim_for_sending(self, message_id: int) -> bool:
        """Atomically move APPROVED -> SENDING. False if someone else got it first."""
        result = self.s.execute(
            update(EmailMessage)
            .where(EmailMessage.id == message_id, EmailMessage.status == MessageStatus.APPROVED)
            .values(status=MessageStatus.SENDING, updated_at=utcnow())
        )
        self.s.flush()
        return result.rowcount == 1

    def sent_count_since(self, since: datetime, campaign_id: int | None = None) -> int:
        stmt = select(func.count()).select_from(EmailMessage).where(
            EmailMessage.status == MessageStatus.SENT, EmailMessage.sent_at >= since
        )
        if campaign_id:
            stmt = stmt.where(EmailMessage.campaign_id == campaign_id)
        return int(self.s.scalar(stmt) or 0)

    def sent_today(self, now: datetime | None = None, campaign_id: int | None = None) -> int:
        return self.sent_count_since(start_of_utc_day(now), campaign_id)

    def last_sent_at(self) -> datetime | None:
        return self.s.scalar(select(func.max(EmailMessage.sent_at)))

    def last_sent_for_lead(self, lead_id: int) -> EmailMessage | None:
        return self.s.scalar(
            select(EmailMessage)
            .where(EmailMessage.lead_id == lead_id, EmailMessage.status == MessageStatus.SENT)
            .order_by(EmailMessage.sent_at.desc()).limit(1)
        )

    def initial_for_lead(self, lead_id: int) -> EmailMessage | None:
        return self.s.scalar(
            select(EmailMessage)
            .where(EmailMessage.lead_id == lead_id, EmailMessage.kind == MessageKind.INITIAL,
                   EmailMessage.status == MessageStatus.SENT)
            .limit(1)
        )

    def has_kind(self, lead_id: int, kind: str, statuses: Sequence[str] | None = None) -> bool:
        stmt = select(func.count()).select_from(EmailMessage).where(
            EmailMessage.lead_id == lead_id, EmailMessage.kind == kind
        )
        if statuses:
            stmt = stmt.where(EmailMessage.status.in_(list(statuses)))
        return bool(self.s.scalar(stmt))

    def is_own_message(self, gmail_message_id: str) -> bool:
        return bool(self.s.scalar(
            select(func.count()).select_from(EmailMessage)
            .where(EmailMessage.gmail_message_id == gmail_message_id)
        ))


# --------------------------------------------------------------------------- replies
class ReplyRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def list(self, campaign_id: int | None = None, limit: int = 500) -> Sequence[Reply]:
        stmt = select(Reply).order_by(Reply.received_at.desc()).limit(limit)
        if campaign_id:
            stmt = stmt.where(Reply.campaign_id == campaign_id)
        return self.s.scalars(stmt).all()

    def category_counts(self, campaign_id: int | None = None) -> dict[str, int]:
        stmt = select(Reply.category, func.count()).group_by(Reply.category)
        if campaign_id:
            stmt = stmt.where(Reply.campaign_id == campaign_id)
        return {c or "UNCLASSIFIED": n for c, n in self.s.execute(stmt).all()}

    def replied_lead_count(self, campaign_id: int | None = None) -> int:
        stmt = select(func.count(func.distinct(Reply.lead_id))).where(
            Reply.category != ReplyCategory.BOUNCE
        )
        if campaign_id:
            stmt = stmt.where(Reply.campaign_id == campaign_id)
        return int(self.s.scalar(stmt) or 0)


# --------------------------------------------------------------------------- suppression
class SuppressionRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def is_suppressed(self, email: str) -> bool:
        email_n = normalize_email(email)
        domain = email_domain(email_n)
        return bool(self.s.scalar(
            select(func.count()).select_from(SuppressionEntry).where(or_(
                SuppressionEntry.email == email_n,
                and_(SuppressionEntry.email.is_(None), SuppressionEntry.domain == domain),
            ))
        ))

    def add_email(self, email: str, reason: str = SuppressionReason.MANUAL, note: str = "") -> SuppressionEntry:
        email_n = normalize_email(email)
        existing = self.s.scalar(select(SuppressionEntry).where(SuppressionEntry.email == email_n))
        if existing:
            return existing
        entry = SuppressionEntry(email=email_n, domain=email_domain(email_n), reason=reason, note=note[:255])
        self.s.add(entry)
        self.s.flush()
        return entry

    def add_domain(self, domain: str, note: str = "") -> SuppressionEntry:
        domain = domain.strip().lower()
        existing = self.s.scalar(select(SuppressionEntry).where(
            SuppressionEntry.email.is_(None), SuppressionEntry.domain == domain
        ))
        if existing:
            return existing
        entry = SuppressionEntry(email=None, domain=domain, reason=SuppressionReason.MANUAL, note=note[:255])
        self.s.add(entry)
        self.s.flush()
        return entry

    def remove(self, entry_id: int) -> bool:
        entry = self.s.get(SuppressionEntry, entry_id)
        if not entry:
            return False
        self.s.delete(entry)
        return True

    def list(self) -> Sequence[SuppressionEntry]:
        return self.s.scalars(select(SuppressionEntry).order_by(SuppressionEntry.created_at.desc())).all()


# --------------------------------------------------------------------------- misc state
class ProcessedMessageRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def is_processed(self, gmail_message_id: str) -> bool:
        return self.s.get(ProcessedMessage, gmail_message_id) is not None

    def processed_ids(self, ids: Sequence[str]) -> set[str]:
        if not ids:
            return set()
        rows = self.s.scalars(select(ProcessedMessage.gmail_message_id)
                              .where(ProcessedMessage.gmail_message_id.in_(list(ids)))).all()
        return set(rows)

    def mark(self, gmail_message_id: str, thread_id: str, outcome: str) -> None:
        if not self.is_processed(gmail_message_id):
            self.s.add(ProcessedMessage(gmail_message_id=gmail_message_id,
                                        gmail_thread_id=thread_id, outcome=outcome[:40]))
            self.s.flush()


class AppSettingRepository:
    SENDING_PAUSED = "sending_paused"
    LAST_INBOX_CHECK = "last_inbox_check_at"
    LAST_LIMIT_HIT = "send_limit_reached_on"

    def __init__(self, session: Session) -> None:
        self.s = session

    def get(self, key: str, default: str = "") -> str:
        row = self.s.get(AppSetting, key)
        return row.value if row else default

    def set(self, key: str, value: str) -> None:
        row = self.s.get(AppSetting, key)
        if row:
            row.value = value
        else:
            self.s.add(AppSetting(key=key, value=value))
        self.s.flush()

    def get_bool(self, key: str) -> bool:
        return self.get(key).lower() in {"1", "true", "yes"}


def recent_window(days: int) -> datetime:
    return utcnow() - timedelta(days=days)
