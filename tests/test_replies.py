"""Reply monitoring, classification parsing and automatic response rules."""

from datetime import date, timedelta

import pytest

from app.ai.mock_provider import MockProvider
from app.ai.provider import AIError
from app.ai.service import AIService
from app.database.models import (
    AIUsage, EmailMessage, LeadStatus, MessageKind, MessageStatus, Reply, ReplyCategory,
)
from app.database.repositories import SuppressionRepository
from app.email.demo_mailbox import DemoMailbox
from app.email.gmail_client import InboxMessage
from app.email.inbox_monitor import InboxMonitor
from app.email.sender import SendService
from app.email.thread_manager import extract_return_date, strip_quoted_text
from app.outreach.email_generator import EmailGenerator
from app.outreach.reply_classifier import ReplyClassifier, parse_classification
from app.outreach.reply_handler import ReplyProcessor
from app.utils.helpers import utcnow

C = ReplyCategory


# ------------------------------------------------------------------ parser
@pytest.mark.parametrize("raw,category,confidence", [
    ({"category": "INTERESTED", "confidence": 0.96, "summary": "Wants pricing", "requires_human": True},
     C.INTERESTED, 0.96),
    ('```json\n{"category": "not interested", "confidence": "0.9"}\n```', C.NOT_INTERESTED, 0.9),
    ({"category": "OOO", "confidence": 95}, C.OUT_OF_OFFICE, 0.95),           # percent -> fraction
    ({"category": "more-info", "confidence": 0.8}, C.MORE_INFORMATION, 0.8),  # alias
    ({"category": "BANANA", "confidence": 0.99}, C.UNCLEAR, 0.0),              # unknown -> safe
    ({"category": "BOUNCE", "confidence": 0.99}, C.UNCLEAR, 0.0),              # AI may not bounce
    ({"category": "QUESTION", "confidence": "high"}, C.QUESTION, 0.0),         # bad confidence
    ("total garbage", C.UNCLEAR, 0.0),
])
def test_parse_classification(raw, category, confidence):
    result = parse_classification(raw)
    assert result.category == category
    assert result.confidence == pytest.approx(confidence)


def test_parse_classification_forces_human_for_interested():
    result = parse_classification({"category": "INTERESTED", "confidence": 0.9, "requires_human": False,
                                   "return_date": "not a date"})
    assert result.requires_human is True and result.return_date is None


# ------------------------------------------------------------------ text helpers
def test_strip_quoted_text():
    body = "Sounds good, call me Tuesday.\n\nOn Mon, 28 Sep 2026, Jon wrote:\n> Hi Sarah,\n> pitch"
    assert strip_quoted_text(body) == "Sounds good, call me Tuesday."
    assert strip_quoted_text("Ja gerne.\nAm 28.09.2026 schrieb Jon <j@x.de>:\n> alt") == "Ja gerne."


@pytest.mark.parametrize("text,expected", [
    ("I am out of office until 2026-10-05.", date(2026, 10, 5)),
    ("I'm away until October 12th with limited access.", date(2026, 10, 12)),
    ("Ich bin bis 14.10.2026 nicht im Büro.", date(2026, 10, 14)),
    ("Back on 3 January.", date(2027, 1, 3)),
    ("I'm out of the office.", None),
])
def test_extract_return_date(text, expected):
    today = date(2026, 12, 20) if "January" in text else date(2026, 9, 28)
    assert extract_return_date(text, today) == expected


# ------------------------------------------------------------------ classifier short-circuits
class CountingMock(MockProvider):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def generate_text(self, prompt, **kw):
        self.calls += 1
        return super().generate_text(prompt, **kw)


def inbox_msg(body, subject="Re: Hello", from_email="sarah@abcdental.de", headers=None):
    return InboxMessage(id="x", thread_id="t", from_email=from_email, subject=subject,
                        body_text=body, headers=headers or {})


@pytest.mark.parametrize("msg,category", [
    (inbox_msg("Address not found", "Delivery Status Notification (Failure)", "mailer-daemon@googlemail.com"), C.BOUNCE),
    (inbox_msg("Please unsubscribe me."), C.UNSUBSCRIBE),
    (inbox_msg("Away until 2026-10-05", headers={"auto-submitted": "auto-replied"}), C.OUT_OF_OFFICE),
    (inbox_msg("   "), C.UNCLEAR),
])
def test_rules_short_circuit_without_ai(session, msg, category):
    provider = CountingMock()
    result = ReplyClassifier(AIService(session, provider)).classify(msg, msg.body_text, "Hello", date(2026, 9, 28))
    assert result.category == category and result.source == "rule"
    assert provider.calls == 0


def test_ai_failure_routes_to_human(session):
    class Down(MockProvider):
        def generate_text(self, *a, **k):
            raise AIError("quota")
    result = ReplyClassifier(AIService(session, Down())).classify(
        inbox_msg("Can you tell me more?"), "Can you tell me more?", "Hello", date(2026, 9, 28))
    assert result.category == C.UNCLEAR and result.requires_human


# ------------------------------------------------------------------ end-to-end with demo mailbox
@pytest.fixture
def pipeline(session, settings, tmp_path):
    provider = CountingMock()
    ai = AIService(session, provider)
    gen = EmailGenerator(session, settings, ai)
    box = DemoMailbox(tmp_path / "box.json")
    processor = ReplyProcessor(session, settings, ReplyClassifier(ai), gen)
    return {
        "provider": provider, "box": box, "generator": gen,
        "sender": SendService(session, settings, box),
        "monitor": InboxMonitor(session, settings, box, processor),
    }


def contacted_lead(session, pipeline, lead_factory, email="sarah@abcdental.de"):
    lead = lead_factory(email, company_name=email.split("@")[1], contact_name=email.split("@")[0])
    msg = pipeline["generator"].generate_initial(lead)
    msg.status, msg.approved_by = MessageStatus.APPROVED, "human"
    session.commit()
    assert pipeline["sender"].process_queue().sent == 1
    return lead


def test_no_ai_when_no_new_replies(session, pipeline, lead_factory):
    contacted_lead(session, pipeline, lead_factory)
    calls = pipeline["provider"].calls
    result = pipeline["monitor"].check()
    assert result.new_replies == 0 and "no AI" in result.note
    assert pipeline["provider"].calls == calls


def test_no_gmail_call_without_tracked_threads(session, pipeline):
    result = pipeline["monitor"].check()
    assert result.listed == 0 and "no tracked threads" in result.note


def test_reply_processed_only_once(session, pipeline, lead_factory):
    lead = contacted_lead(session, pipeline, lead_factory)
    pipeline["box"].simulate_scenario(lead.gmail_thread_id, lead.email, "question")
    assert pipeline["monitor"].check().new_replies == 1
    assert pipeline["monitor"].check().new_replies == 0
    assert session.query(Reply).count() == 1


def test_unrelated_threads_ignored(session, pipeline, lead_factory):
    contacted_lead(session, pipeline, lead_factory)
    pipeline["box"].inject_reply("some-other-thread", "random@person.com", "Newsletter")
    assert pipeline["monitor"].check().new_replies == 0


def test_unsubscribe_suppresses_permanently(session, pipeline, lead_factory):
    lead = contacted_lead(session, pipeline, lead_factory)
    pipeline["box"].simulate_scenario(lead.gmail_thread_id, lead.email, "unsubscribe")
    pipeline["monitor"].check()
    assert lead.do_not_contact and lead.status == LeadStatus.NOT_INTERESTED
    assert lead.next_followup_at is None
    assert SuppressionRepository(session).is_suppressed(lead.email)
    # a new lead with the same address can't be imported into any campaign
    from app.leads.importer import LeadInput
    from app.leads.lead_service import LeadService
    _, issue, _ = LeadService(session).add_lead(lead.campaign, LeadInput(email=lead.email.upper()))
    assert "suppression" in issue.reason


def test_not_interested_stops_followups(session, pipeline, lead_factory):
    lead = contacted_lead(session, pipeline, lead_factory)
    pipeline["box"].simulate_scenario(lead.gmail_thread_id, lead.email, "not_interested")
    pipeline["monitor"].check()
    assert lead.status == LeadStatus.NOT_INTERESTED and lead.next_followup_at is None
    assert not lead.do_not_contact


def test_interested_escalates_to_human_with_draft(session, pipeline, lead_factory):
    lead = contacted_lead(session, pipeline, lead_factory)
    pipeline["box"].simulate_scenario(lead.gmail_thread_id, lead.email, "interested")
    pipeline["monitor"].check()
    assert lead.status == LeadStatus.INTERESTED and lead.human_review_required
    drafts = [m for m in lead.messages if m.kind == MessageKind.REPLY]
    assert len(drafts) == 1 and drafts[0].status == MessageStatus.PENDING_APPROVAL
    assert drafts[0].gmail_thread_id == lead.gmail_thread_id


def test_interested_draft_never_auto_sent_even_in_automatic_mode(session, settings, pipeline, lead_factory):
    lead = contacted_lead(session, pipeline, lead_factory)
    lead.campaign.auto_send = True
    pipeline["monitor"].settings = settings.model_copy(update={"safe_mode": False})
    pipeline["box"].simulate_scenario(lead.gmail_thread_id, lead.email, "interested")
    pipeline["monitor"].check()
    assert all(m.status == MessageStatus.PENDING_APPROVAL for m in lead.messages if m.kind == MessageKind.REPLY)


def test_out_of_office_reschedules_followup(session, pipeline, lead_factory):
    lead = contacted_lead(session, pipeline, lead_factory)
    pipeline["box"].simulate_scenario(lead.gmail_thread_id, lead.email, "out_of_office")
    pipeline["monitor"].check()
    assert lead.status == LeadStatus.OUT_OF_OFFICE
    expected = (utcnow() + timedelta(days=7)).date()
    assert lead.next_followup_at.date() == expected


def test_wrong_person_asks_for_referral_only_once(session, pipeline, lead_factory):
    lead = contacted_lead(session, pipeline, lead_factory)
    box = pipeline["box"]
    box.simulate_scenario(lead.gmail_thread_id, lead.email, "wrong_person")
    pipeline["monitor"].check()
    box.simulate_scenario(lead.gmail_thread_id, lead.email, "wrong_person")
    pipeline["monitor"].check()
    referrals = [m for m in lead.messages if m.kind == MessageKind.REFERRAL_REQUEST]
    assert len(referrals) == 1
    assert lead.status == LeadStatus.WRONG_PERSON


def test_bounce_marks_lead_bounced(session, pipeline, lead_factory):
    lead = contacted_lead(session, pipeline, lead_factory)
    pipeline["box"].simulate_scenario(lead.gmail_thread_id, lead.email, "bounce")
    pipeline["monitor"].check()
    assert lead.status == LeadStatus.BOUNCED and lead.do_not_contact


def test_low_confidence_goes_to_human_review(session, pipeline, lead_factory):
    lead = contacted_lead(session, pipeline, lead_factory)
    pipeline["box"].simulate_scenario(lead.gmail_thread_id, lead.email, "unclear")
    pipeline["monitor"].check()
    assert lead.status == LeadStatus.HUMAN_REVIEW and lead.human_review_required
    # no automatic outbound message was created
    assert not [m for m in lead.messages if m.kind != MessageKind.INITIAL]


def test_only_latest_message_sent_to_ai(session, pipeline, lead_factory):
    lead = contacted_lead(session, pipeline, lead_factory)
    pipeline["box"].simulate_scenario(lead.gmail_thread_id, lead.email, "question")
    pipeline["monitor"].check()
    reply = session.query(Reply).one()
    assert ">" not in reply.body and "wrote:" not in reply.body
    assert session.query(AIUsage).filter_by(purpose="classify_reply").count() == 1
