"""Follow-up scheduling - entirely deterministic except the text itself.

* after the initial email: next follow-up = sent_at + FOLLOWUP_1_DAYS
* after follow-up 1:       next follow-up = sent_at + FOLLOWUP_2_DAYS
* after the last follow-up: no more follow-ups, lead is completed once the
  final waiting period passes without a reply
* follow-ups never go to unsubscribed / not-interested / bounced /
  do-not-contact leads (enforced by :func:`check_can_contact`)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config.logging_config import log_event
from app.config.settings import Settings
from app.database.models import Campaign, Lead, LeadStatus, MessageKind
from app.database.repositories import LeadRepository, MessageRepository
from app.outreach.compliance import check_can_contact
from app.outreach.email_generator import EmailGenerator
from app.outreach.status_machine import can_transition_lead
from app.utils.helpers import utcnow


def effective_max_followups(campaign: Campaign, settings: Settings) -> int:
    return min(campaign.max_followups, settings.max_followups)


def next_followup_time(sent_at: datetime, followups_sent: int, campaign: Campaign,
                       settings: Settings) -> datetime | None:
    """When the next follow-up is due, given how many follow-ups were already sent.

    Returns None when the follow-up budget is exhausted.
    """
    if followups_sent >= effective_max_followups(campaign, settings):
        return None
    intervals = campaign.followup_intervals or settings.followup_intervals_days
    days = intervals[min(followups_sent, len(intervals) - 1)]
    return sent_at + timedelta(days=days)


def schedule_next_followup(lead: Lead, sent_at: datetime, settings: Settings) -> None:
    lead.next_followup_at = next_followup_time(sent_at, lead.followup_count, lead.campaign, settings)
    if lead.next_followup_at:
        log_event("followup_scheduled", lead_id=lead.id, due=lead.next_followup_at.isoformat(),
                  number=lead.followup_count + 1)


@dataclass
class FollowupRunResult:
    generated: list[int] = field(default_factory=list)
    completed: list[int] = field(default_factory=list)
    skipped: dict[int, str] = field(default_factory=dict)


class FollowupService:
    def __init__(self, session: Session, settings: Settings, generator: EmailGenerator) -> None:
        self.session = session
        self.settings = settings
        self.generator = generator
        self.leads = LeadRepository(session)
        self.messages = MessageRepository(session)

    def process_due(self, now: datetime | None = None) -> FollowupRunResult:
        """Create follow-up drafts for every lead whose follow-up is due.

        Uses no AI at all unless a follow-up is actually due.
        """
        now = now or utcnow()
        result = FollowupRunResult()
        for lead in self.leads.due_for_followup(now):
            campaign = lead.campaign
            max_followups = effective_max_followups(campaign, self.settings)

            if lead.followup_count >= max_followups:
                # Sequence finished without a reply -> close the lead.
                lead.next_followup_at = None
                if lead.status == LeadStatus.EMAIL_SENT and can_transition_lead(lead.status, LeadStatus.COMPLETED):
                    lead.status = LeadStatus.COMPLETED
                    result.completed.append(lead.id)
                continue

            decision = check_can_contact(self.session, lead, campaign, MessageKind.FOLLOWUP, max_followups)
            if not decision.allowed:
                lead.next_followup_at = None
                result.skipped[lead.id] = decision.reason
                continue
            if self.messages.open_drafts_for_lead(lead.id, [MessageKind.FOLLOWUP]):
                result.skipped[lead.id] = "follow-up already queued"
                continue
            previous = self.messages.last_sent_for_lead(lead.id)
            if previous is None:
                lead.next_followup_at = None
                result.skipped[lead.id] = "no previous email found"
                continue

            message = self.generator.generate_followup(lead, previous)
            if message:
                lead.status = LeadStatus.FOLLOWUP_DUE
                lead.next_followup_at = None  # rescheduled when this follow-up is sent
                result.generated.append(lead.id)
        self.session.commit()
        return result

    def cancel_followups(self, lead: Lead, reason: str) -> int:
        lead.next_followup_at = None
        return self.messages.cancel_open_for_lead(lead.id, [MessageKind.FOLLOWUP], reason)

