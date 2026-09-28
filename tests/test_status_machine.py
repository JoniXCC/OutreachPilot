import pytest

from app.database.models import CampaignStatus, LeadStatus
from app.outreach.status_machine import (
    InvalidTransitionError, can_transition_campaign, can_transition_lead, transition_campaign,
    transition_lead,
)


@pytest.mark.parametrize("current,new", [
    (LeadStatus.NEW, LeadStatus.RESEARCHED),
    (LeadStatus.RESEARCHED, LeadStatus.READY),
    (LeadStatus.READY, LeadStatus.EMAIL_SENT),
    (LeadStatus.EMAIL_SENT, LeadStatus.FOLLOWUP_DUE),
    (LeadStatus.FOLLOWUP_DUE, LeadStatus.EMAIL_SENT),
    (LeadStatus.EMAIL_SENT, LeadStatus.REPLIED),
    (LeadStatus.REPLIED, LeadStatus.INTERESTED),
    (LeadStatus.OUT_OF_OFFICE, LeadStatus.FOLLOWUP_DUE),
    (LeadStatus.EMAIL_SENT, LeadStatus.BOUNCED),
    (LeadStatus.NOT_INTERESTED, LeadStatus.COMPLETED),
])
def test_valid_lead_transitions(current, new):
    assert can_transition_lead(current, new)


@pytest.mark.parametrize("current,new", [
    (LeadStatus.NOT_INTERESTED, LeadStatus.EMAIL_SENT),   # never re-contact
    (LeadStatus.NOT_INTERESTED, LeadStatus.FOLLOWUP_DUE),
    (LeadStatus.BOUNCED, LeadStatus.EMAIL_SENT),
    (LeadStatus.COMPLETED, LeadStatus.NEW),
    (LeadStatus.NEW, LeadStatus.EMAIL_SENT),              # must be drafted first
    (LeadStatus.HUMAN_REVIEW, LeadStatus.NEW),
])
def test_invalid_lead_transitions(current, new):
    assert not can_transition_lead(current, new)


def test_transition_lead_raises(session, lead_factory):
    lead = lead_factory(status=LeadStatus.NOT_INTERESTED)
    with pytest.raises(InvalidTransitionError):
        transition_lead(lead, LeadStatus.EMAIL_SENT)
    assert lead.status == LeadStatus.NOT_INTERESTED


def test_same_status_is_noop(session, lead_factory):
    lead = lead_factory(status=LeadStatus.COMPLETED)
    transition_lead(lead, LeadStatus.COMPLETED)
    assert lead.status == LeadStatus.COMPLETED


def test_campaign_transitions(campaign):
    assert can_transition_campaign(CampaignStatus.DRAFT, CampaignStatus.ACTIVE)
    assert can_transition_campaign(CampaignStatus.ACTIVE, CampaignStatus.PAUSED)
    assert can_transition_campaign(CampaignStatus.PAUSED, CampaignStatus.ACTIVE)
    assert not can_transition_campaign(CampaignStatus.STOPPED, CampaignStatus.ACTIVE)
    assert not can_transition_campaign(CampaignStatus.COMPLETED, CampaignStatus.ACTIVE)

    transition_campaign(campaign, CampaignStatus.STOPPED)
    with pytest.raises(InvalidTransitionError):
        transition_campaign(campaign, CampaignStatus.ACTIVE)
