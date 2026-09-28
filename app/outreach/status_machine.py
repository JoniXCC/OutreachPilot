"""Deterministic state machines for leads and campaigns.

Keeping transitions explicit prevents bugs such as a NOT_INTERESTED lead being
moved back into the sending pipeline.
"""

from __future__ import annotations

from app.database.models import Campaign, CampaignStatus, Lead, LeadStatus

S = LeadStatus

_REPLY_OUTCOMES = {S.REPLIED, S.INTERESTED, S.NOT_INTERESTED, S.WRONG_PERSON,
                   S.OUT_OF_OFFICE, S.HUMAN_REVIEW, S.BOUNCED}

LEAD_TRANSITIONS: dict[LeadStatus, set[LeadStatus]] = {
    S.NEW: {S.RESEARCHED, S.READY, S.HUMAN_REVIEW, S.NOT_INTERESTED, S.COMPLETED},
    S.RESEARCHED: {S.READY, S.HUMAN_REVIEW, S.NOT_INTERESTED, S.COMPLETED},
    S.READY: {S.EMAIL_SENT, S.RESEARCHED, S.NEW, S.HUMAN_REVIEW, S.NOT_INTERESTED, S.COMPLETED},
    S.EMAIL_SENT: {S.FOLLOWUP_DUE, S.COMPLETED} | _REPLY_OUTCOMES,
    S.FOLLOWUP_DUE: {S.EMAIL_SENT, S.COMPLETED} | _REPLY_OUTCOMES,
    S.REPLIED: {S.COMPLETED} | (_REPLY_OUTCOMES - {S.REPLIED}),
    S.OUT_OF_OFFICE: {S.FOLLOWUP_DUE, S.EMAIL_SENT, S.COMPLETED} | _REPLY_OUTCOMES,
    S.WRONG_PERSON: {S.EMAIL_SENT, S.COMPLETED} | _REPLY_OUTCOMES,
    S.INTERESTED: {S.REPLIED, S.HUMAN_REVIEW, S.NOT_INTERESTED, S.EMAIL_SENT, S.COMPLETED},
    # A human reviewing a lead may move it anywhere except back to the start.
    S.HUMAN_REVIEW: set(S) - {S.NEW, S.HUMAN_REVIEW},
    S.NOT_INTERESTED: {S.COMPLETED},
    S.BOUNCED: {S.COMPLETED},
    S.COMPLETED: set(),
}

#: Statuses in which the lead must never receive automated outreach.
NO_CONTACT_STATUSES = frozenset({S.NOT_INTERESTED, S.BOUNCED, S.COMPLETED})

#: Statuses where automated follow-ups may still be sent.
FOLLOWUP_ELIGIBLE_STATUSES = frozenset({S.EMAIL_SENT, S.FOLLOWUP_DUE, S.OUT_OF_OFFICE})

CAMPAIGN_TRANSITIONS: dict[CampaignStatus, set[CampaignStatus]] = {
    CampaignStatus.DRAFT: {CampaignStatus.ACTIVE, CampaignStatus.STOPPED},
    CampaignStatus.ACTIVE: {CampaignStatus.PAUSED, CampaignStatus.STOPPED, CampaignStatus.COMPLETED},
    CampaignStatus.PAUSED: {CampaignStatus.ACTIVE, CampaignStatus.STOPPED, CampaignStatus.COMPLETED},
    CampaignStatus.STOPPED: {CampaignStatus.DRAFT},  # must be explicitly re-drafted
    CampaignStatus.COMPLETED: set(),
}


class InvalidTransitionError(ValueError):
    pass


def can_transition_lead(current: str, new: str) -> bool:
    if current == new:
        return True
    return LeadStatus(new) in LEAD_TRANSITIONS.get(LeadStatus(current), set())


def transition_lead(lead: Lead, new: LeadStatus | str) -> None:
    """Move a lead to a new status or raise :class:`InvalidTransitionError`."""
    new = LeadStatus(new)
    if not can_transition_lead(lead.status, new):
        raise InvalidTransitionError(f"Lead {lead.id}: {lead.status} -> {new} is not allowed")
    lead.status = new


def can_transition_campaign(current: str, new: str) -> bool:
    if current == new:
        return True
    return CampaignStatus(new) in CAMPAIGN_TRANSITIONS.get(CampaignStatus(current), set())


def transition_campaign(campaign: Campaign, new: CampaignStatus | str) -> None:
    new = CampaignStatus(new)
    if not can_transition_campaign(campaign.status, new):
        raise InvalidTransitionError(f"Campaign {campaign.id}: {campaign.status} -> {new} is not allowed")
    campaign.status = new
