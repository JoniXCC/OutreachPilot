"""Responsible-outreach guard rails.

:func:`check_can_contact` is the single source of truth for "may we email this
lead right now?" and is called immediately before every send, so no code path
can bypass suppression, do-not-contact or campaign stop controls.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config.logging_config import log_event
from app.database.models import (
    Campaign, CampaignStatus, Lead, LeadStatus, MessageKind, SuppressionReason,
)
from app.database.repositories import MessageRepository, SuppressionRepository
from app.outreach.status_machine import NO_CONTACT_STATUSES, can_transition_lead

# Human-written replies to people who wrote to us are allowed while a
# campaign is paused - but never to suppressed / do-not-contact leads.
_CONVERSATIONAL_KINDS = {MessageKind.REPLY, MessageKind.REFERRAL_REQUEST}


@dataclass(frozen=True)
class ContactDecision:
    allowed: bool
    reason: str = ""


def check_can_contact(session: Session, lead: Lead, campaign: Campaign,
                      kind: str = MessageKind.INITIAL, max_followups: int | None = None) -> ContactDecision:
    if lead.do_not_contact:
        return ContactDecision(False, "lead is marked do-not-contact")
    if SuppressionRepository(session).is_suppressed(lead.email):
        return ContactDecision(False, "address is on the suppression list")
    if lead.status in NO_CONTACT_STATUSES:
        return ContactDecision(False, f"lead status is {lead.status}")
    if kind in _CONVERSATIONAL_KINDS:
        if campaign.status == CampaignStatus.STOPPED:
            return ContactDecision(False, "campaign is stopped")
        return ContactDecision(True)
    if campaign.status != CampaignStatus.ACTIVE:
        return ContactDecision(False, f"campaign is {campaign.status}")
    if kind == MessageKind.FOLLOWUP:
        limit = campaign.max_followups if max_followups is None else min(max_followups, campaign.max_followups)
        if lead.followup_count >= limit:
            return ContactDecision(False, f"maximum follow-ups ({limit}) reached")
        if lead.status not in (LeadStatus.EMAIL_SENT, LeadStatus.FOLLOWUP_DUE, LeadStatus.OUT_OF_OFFICE):
            return ContactDecision(False, f"follow-ups not allowed in status {lead.status}")
    if kind == MessageKind.INITIAL and lead.status not in (LeadStatus.NEW, LeadStatus.RESEARCHED,
                                                            LeadStatus.READY, LeadStatus.HUMAN_REVIEW):
        return ContactDecision(False, f"initial email already sent (status {lead.status})")
    return ContactDecision(True)


def suppress_lead(session: Session, lead: Lead, reason: str = SuppressionReason.UNSUBSCRIBE,
                  note: str = "") -> None:
    """Permanently stop all outreach to a lead's address (unless manually reset)."""
    lead.do_not_contact = True
    lead.next_followup_at = None
    SuppressionRepository(session).add_email(lead.email, reason=reason, note=note)
    cancelled = MessageRepository(session).cancel_open_for_lead(lead.id, reason=f"suppressed: {reason}")
    target = LeadStatus.BOUNCED if reason == SuppressionReason.BOUNCED else LeadStatus.NOT_INTERESTED
    if can_transition_lead(lead.status, target):
        lead.status = target
    log_event("lead_suppressed", lead_id=lead.id, reason=str(reason), cancelled_messages=cancelled)
