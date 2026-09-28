from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.models import EmailMessage, Lead, MessageStatus
from app.database.repositories import (
    AppSettingRepository, MessageRepository, ProcessedMessageRepository, SuppressionRepository,
)
from app.utils.helpers import utcnow


def test_tables_created_and_relationships(session, campaign, lead_factory):
    lead = lead_factory()
    session.add(EmailMessage(lead_id=lead.id, campaign_id=campaign.id, to_email=lead.email,
                             subject="Hi", body="Hello"))
    session.commit()
    session.refresh(lead)
    assert lead.campaign.name == "Dental Chatbot DE"
    assert len(lead.messages) == 1


def test_unique_email_per_campaign(session, campaign, lead_factory):
    lead_factory("a@abc.de")
    with pytest.raises(IntegrityError):
        session.add(Lead(campaign_id=campaign.id, company_name="X", email="A@abc.de",
                         email_normalized="a@abc.de"))
        session.flush()
    session.rollback()


def test_research_json_property(session, lead_factory):
    lead = lead_factory()
    lead.research = {"company_summary": "Clinic", "confidence": 0.8}
    session.commit()
    assert lead.research["confidence"] == 0.8


def test_claim_for_sending_is_atomic(session, campaign, lead_factory):
    lead = lead_factory()
    msg = EmailMessage(lead_id=lead.id, campaign_id=campaign.id, to_email=lead.email,
                       subject="s", body="b", status=MessageStatus.APPROVED)
    session.add(msg)
    session.commit()
    repo = MessageRepository(session)
    assert repo.claim_for_sending(msg.id) is True
    assert repo.claim_for_sending(msg.id) is False  # second worker loses


def test_sent_today_counter(session, campaign, lead_factory):
    lead = lead_factory()
    now = utcnow()
    for i, sent in enumerate([now, now, now - timedelta(days=1)]):
        session.add(EmailMessage(lead_id=lead.id, campaign_id=campaign.id, to_email=lead.email,
                                 subject=f"s{i}", body="b", status=MessageStatus.SENT, sent_at=sent))
    session.commit()
    assert MessageRepository(session).sent_today(now) == 2


def test_app_settings_and_processed(session):
    settings_repo = AppSettingRepository(session)
    settings_repo.set("sending_paused", "true")
    assert settings_repo.get_bool("sending_paused")
    processed = ProcessedMessageRepository(session)
    processed.mark("m1", "t1", "reply")
    processed.mark("m1", "t1", "reply")  # idempotent
    assert processed.processed_ids(["m1", "m2"]) == {"m1"}


def test_suppression_repository(session):
    repo = SuppressionRepository(session)
    repo.add_email("Stop@Example.com", note="asked to stop")
    repo.add_email("stop@example.com")  # duplicate ignored
    repo.add_domain("blocked.io")
    session.commit()
    assert repo.is_suppressed("STOP@example.com")
    assert repo.is_suppressed("anyone@blocked.io")
    assert not repo.is_suppressed("other@example.com")
    assert len(repo.list()) == 2
