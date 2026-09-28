import json

import pytest

from app.ai.mock_provider import MockProvider
from app.ai.provider import AIError, AIResponse
from app.ai.service import AIService
from app.database.models import CampaignStatus, LeadStatus, MessageKind, MessageStatus
from app.outreach.approval_service import ApprovalError, ApprovalService
from app.outreach.campaign_service import CampaignInput, CampaignService
from app.outreach.email_generator import (
    OPT_OUT_LINE, EmailGenerator, approval_state, check_quality, clean_ai_body,
)


class ScriptedProvider(MockProvider):
    """Returns queued JSON payloads; raises when the queue is empty."""

    def __init__(self, payloads):
        super().__init__()
        self.payloads = list(payloads)
        self.calls = 0

    def generate_text(self, prompt, **kw):
        self.calls += 1
        if not self.payloads:
            raise AIError("no more scripted responses")
        return AIResponse(json.dumps(self.payloads.pop(0)), "mock", "scripted")


GOOD_BODY = ("I was looking at ABC Dental and noticed you run three clinics around Berlin with "
             "online booking. We built an AI assistant that answers common patient questions "
             "automatically on your website, which can reduce repetitive phone calls for your "
             "reception team without changing how your practice already works day to day. "
             "Would you be open to a 10-minute demo next week?")


def generator(session, settings, payloads):
    return EmailGenerator(session, settings, AIService(session, ScriptedProvider(payloads)))


# ------------------------------------------------------------------ quality gate
def test_quality_gate_accepts_good_email():
    report = check_quality("Patient questions at ABC Dental", GOOD_BODY, "10-minute demo three clinics")
    assert report.flags == []


@pytest.mark.parametrize("body,flag", [
    ("Hi [First Name], " + GOOD_BODY, "placeholder"),
    (GOOD_BODY + " We increased bookings by 47% for our clients.", "unverified_number"),
    (GOOD_BODY + " See https://example.com", "link"),
    (GOOD_BODY + " Our revolutionary platform is a game-changer.", "hype_language"),
    (GOOD_BODY + " Our clients love it.", "unverified_claim"),
    ("Too short.", "too_short"),
    (GOOD_BODY * 3, "too_long"),
])
def test_quality_gate_flags(body, flag):
    assert flag in check_quality("Subject", body, "10-minute demo").flags


def test_clean_ai_body_strips_greeting_and_signature():
    raw = "Hi Sarah,\n\nThis is the body.\n\nBest,\nJon"
    assert clean_ai_body(raw) == "This is the body."


# ------------------------------------------------------------------ generation
def test_generate_initial_safe_mode(session, settings, lead_factory):
    lead = lead_factory()
    gen = generator(session, settings, [{"subject": "Patient questions at ABC Dental",
                                         "body": GOOD_BODY, "personalization_reason": "three clinics"}])
    msg = gen.generate_initial(lead)
    assert msg.status == MessageStatus.PENDING_APPROVAL and msg.approved_by is None
    assert msg.body.startswith("Hi Sarah,")
    assert "Jon Tester" in msg.body and OPT_OUT_LINE in msg.body
    assert lead.status == LeadStatus.READY
    # asking again returns the existing draft instead of spending another AI call
    assert gen.generate_initial(lead).id == msg.id


def test_generation_retries_then_falls_back_to_template(session, settings, lead_factory):
    lead = lead_factory()
    bad = {"subject": "Hi", "body": "Hi [Name], we grew revenue 300% " + GOOD_BODY}
    gen = generator(session, settings, [bad, bad])
    msg = gen.generate_initial(lead)
    assert "template_fallback" in msg.quality_flags
    assert "[Name]" not in msg.body and "300%" not in msg.body
    assert msg.ai_generated is False


def test_generation_survives_ai_outage(session, settings, lead_factory):
    lead = lead_factory()
    msg = generator(session, settings, []).generate_initial(lead)
    assert msg is not None and "template_fallback" in msg.quality_flags


def test_no_email_for_do_not_contact(session, settings, lead_factory):
    lead = lead_factory(do_not_contact=True)
    assert generator(session, settings, []).generate_initial(lead) is None


def test_automatic_mode_requires_global_and_campaign_opt_in(settings, campaign):
    assert approval_state(settings, campaign, MessageKind.INITIAL)[0] == MessageStatus.PENDING_APPROVAL
    campaign.auto_send = True
    assert approval_state(settings, campaign, MessageKind.INITIAL)[0] == MessageStatus.PENDING_APPROVAL
    unsafe = settings.model_copy(update={"safe_mode": False})
    assert approval_state(unsafe, campaign, MessageKind.INITIAL) == (MessageStatus.APPROVED, "auto")
    # replies to interested leads are never auto-sent
    assert approval_state(unsafe, campaign, MessageKind.REPLY)[0] == MessageStatus.PENDING_APPROVAL


# ------------------------------------------------------------------ approval queue
def test_approve_edit_reject_regenerate(session, settings, lead_factory):
    lead = lead_factory()
    gen = generator(session, settings, [
        {"subject": "First", "body": GOOD_BODY, "personalization_reason": "x"},
        {"subject": "Second", "body": GOOD_BODY, "personalization_reason": "y"},
    ])
    approvals = ApprovalService(session)
    msg = gen.generate_initial(lead)

    new = approvals.regenerate(msg, gen)
    assert msg.status == MessageStatus.CANCELLED and new.subject == "Second"

    approvals.edit(new, "Edited subject", new.body + "\nextra")
    assert "edited" in new.quality_flags
    approvals.approve(new)
    assert new.status == MessageStatus.APPROVED and new.approved_by == "human"
    with pytest.raises(ApprovalError):
        approvals.reject(new)  # no longer pending


def test_reject_returns_lead_to_pool(session, settings, lead_factory):
    lead = lead_factory()
    msg = generator(session, settings, [{"subject": "S", "body": GOOD_BODY}]).generate_initial(lead)
    ApprovalService(session).reject(msg)
    assert msg.status == MessageStatus.REJECTED and lead.status == LeadStatus.NEW


def test_cannot_approve_for_suppressed_lead(session, settings, lead_factory):
    from app.database.repositories import SuppressionRepository
    lead = lead_factory()
    msg = generator(session, settings, [{"subject": "S", "body": GOOD_BODY}]).generate_initial(lead)
    SuppressionRepository(session).add_email(lead.email)
    with pytest.raises(ApprovalError):
        ApprovalService(session).approve(msg)


# ------------------------------------------------------------------ campaigns
def test_campaign_service_create_limits_and_icp(session, settings):
    svc = CampaignService(session, settings)
    c = svc.create(CampaignInput(name="Test", company_name="Co", product_name="Widget",
                                 max_emails_per_day=150, max_followups=5))
    assert c.max_emails_per_day == settings.max_emails_per_day  # clamped to global limit
    assert c.max_followups == settings.max_followups
    assert c.status == CampaignStatus.DRAFT
    with pytest.raises(ValueError):
        svc.create(CampaignInput(name="Test", company_name="Co", product_name="Widget"))
    icp = svc.generate_icp(c, AIService(session, MockProvider()))
    assert icp["summary"] and len(icp["pain_points"]) <= 3


def test_stopping_campaign_cancels_queue(session, settings, campaign, lead_factory):
    lead = lead_factory()
    msg = generator(session, settings, [{"subject": "S", "body": GOOD_BODY}]).generate_initial(lead)
    CampaignService(session, settings).set_status(campaign, CampaignStatus.STOPPED)
    assert msg.status == MessageStatus.CANCELLED
