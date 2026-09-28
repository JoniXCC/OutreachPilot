"""Follow-up scheduling and limits."""

from datetime import timedelta

from app.ai.mock_provider import MockProvider
from app.ai.service import AIService
from app.database.models import CampaignStatus, LeadStatus, MessageKind, MessageStatus
from app.email.demo_mailbox import DemoMailbox
from app.email.sender import SendService
from app.outreach.email_generator import EmailGenerator
from app.outreach.followup_service import FollowupService, next_followup_time
from app.utils.helpers import utcnow


def test_next_followup_time(settings, campaign):
    t0 = utcnow()
    assert next_followup_time(t0, 0, campaign, settings) == t0 + timedelta(days=3)
    assert next_followup_time(t0, 1, campaign, settings) == t0 + timedelta(days=5)
    assert next_followup_time(t0, 2, campaign, settings) is None  # budget exhausted


def test_global_max_followups_caps_campaign(settings, campaign):
    campaign.max_followups = 5
    strict = settings.model_copy(update={"max_followups": 1})
    assert next_followup_time(utcnow(), 1, campaign, strict) is None


class Env:
    def __init__(self, session, settings, tmp_path):
        self.session = session
        self.now = utcnow()
        self.box = DemoMailbox(tmp_path / "box.json")
        self.gen = EmailGenerator(session, settings, AIService(session, MockProvider()))
        self.followups = FollowupService(session, settings, self.gen)
        self.settings = settings

    def sender(self):
        return SendService(self.session, self.settings, self.box, clock=lambda: self.now)

    def approve_all(self):
        from app.database.repositories import MessageRepository
        for m in MessageRepository(self.session).pending_approval():
            m.status, m.approved_by = MessageStatus.APPROVED, "human"
        self.session.commit()

    def advance(self, days):
        self.now += timedelta(days=days, minutes=1)


def test_full_followup_sequence(session, settings, lead_factory, tmp_path):
    env = Env(session, settings, tmp_path)
    lead = lead_factory()
    env.gen.generate_initial(lead)
    env.approve_all()
    env.sender().process_queue()
    initial = lead.messages[0]
    assert lead.status == LeadStatus.EMAIL_SENT

    # Not due yet -> nothing generated
    assert env.followups.process_due(env.now).generated == []

    # Follow-up 1 after 3 days
    env.advance(3)
    assert env.followups.process_due(env.now).generated == [lead.id]
    assert lead.status == LeadStatus.FOLLOWUP_DUE
    # running again does not create a duplicate draft
    assert env.followups.process_due(env.now).generated == []
    env.approve_all()
    env.sender().process_queue()
    f1 = lead.messages[-1]
    assert f1.kind == MessageKind.FOLLOWUP and f1.status == MessageStatus.SENT
    assert f1.gmail_thread_id == initial.gmail_thread_id          # same Gmail thread
    assert f1.subject == f"Re: {initial.subject}"
    assert f1.body != initial.body                                  # not a copy
    assert lead.followup_count == 1

    # Follow-up 2 after another 5 days
    env.advance(5)
    env.followups.process_due(env.now)
    env.approve_all()
    env.sender().process_queue()
    assert lead.followup_count == 2

    # Budget exhausted: no follow-up 3, lead is completed after the final wait
    env.advance(5)
    result = env.followups.process_due(env.now)
    assert result.generated == [] and result.completed == [lead.id]
    assert lead.status == LeadStatus.COMPLETED
    assert len([m for m in lead.messages if m.kind == MessageKind.FOLLOWUP]) == 2


def test_no_followups_after_unsubscribe(session, settings, lead_factory, tmp_path):
    env = Env(session, settings, tmp_path)
    lead = lead_factory()
    env.gen.generate_initial(lead)
    env.approve_all()
    env.sender().process_queue()
    from app.outreach.compliance import suppress_lead
    suppress_lead(session, lead)
    env.advance(10)
    assert env.followups.process_due(env.now).generated == []


def test_do_not_contact_blocks_followups(session, settings, lead_factory, tmp_path):
    env = Env(session, settings, tmp_path)
    lead = lead_factory()
    env.gen.generate_initial(lead)
    env.approve_all()
    env.sender().process_queue()
    lead.do_not_contact = True
    session.commit()
    env.advance(4)
    assert env.followups.process_due(env.now).generated == []


def test_paused_campaign_gets_no_followups(session, settings, campaign, lead_factory, tmp_path):
    env = Env(session, settings, tmp_path)
    lead = lead_factory()
    env.gen.generate_initial(lead)
    env.approve_all()
    env.sender().process_queue()
    campaign.status = CampaignStatus.PAUSED
    session.commit()
    env.advance(4)
    assert env.followups.process_due(env.now).generated == []
    assert lead.next_followup_at is not None  # resumes when campaign is re-activated


def test_approved_followup_blocked_if_lead_replied_meanwhile(session, settings, lead_factory, tmp_path):
    env = Env(session, settings, tmp_path)
    lead = lead_factory()
    env.gen.generate_initial(lead)
    env.approve_all()
    env.sender().process_queue()
    env.advance(3)
    env.followups.process_due(env.now)
    env.approve_all()
    lead.status = LeadStatus.NOT_INTERESTED   # reply arrived before the send job ran
    session.commit()
    env.sender().process_queue()
    follow = lead.messages[-1]
    assert follow.status == MessageStatus.CANCELLED
