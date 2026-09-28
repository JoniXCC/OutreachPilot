"""Shared pytest fixtures: in-memory DB, test settings, fake AI and Gmail."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.database.database import create_db_engine, init_db, make_session_factory
from app.database.models import Campaign, CampaignStatus, Lead, LeadStatus
from app.utils.helpers import normalize_domain, normalize_email, normalize_name


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        demo_mode=True,
        safe_mode=True,
        ai_provider="mock",
        ai_fallback_provider=None,
        database_url="sqlite://",
        demo_mailbox_path=tmp_path / "mailbox.json",
        log_file=tmp_path / "app.log",
        max_emails_per_day=5,
        min_seconds_between_emails=0,
        max_followups=2,
        followup_1_days=3,
        followup_2_days=5,
        research_min_seconds_between_requests=0,
        research_respect_robots_txt=False,
        sender_name="Jon Tester",
    )


@pytest.fixture
def session(settings: Settings) -> Iterator[Session]:
    engine = create_db_engine("sqlite://")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        yield s
    engine.dispose()


@pytest.fixture
def campaign(session: Session) -> Campaign:
    c = Campaign(
        name="Dental Chatbot DE",
        company_name="SmileBot",
        sender_name="Jon Tester",
        product_name="AI chatbot for independent dental clinics",
        product_description="Answers common patient questions on the clinic website 24/7.",
        value_proposition="Automatically answers common patient questions and reduces receptionist workload.",
        target_industry="Dental clinics",
        target_company_size="2-20 employees",
        target_locations="Germany",
        target_roles="Owner, Practice Manager",
        call_to_action="Would you be open to a 10-minute demo?",
        status=CampaignStatus.ACTIVE,
        max_emails_per_day=10,
    )
    session.add(c)
    session.commit()
    return c


def make_lead(session: Session, campaign: Campaign, email: str = "sarah@abcdental.de", **kw) -> Lead:
    company = kw.pop("company_name", "ABC Dental")
    contact = kw.pop("contact_name", "Sarah Klein")
    website = kw.pop("website", "https://abcdental.de")
    lead = Lead(
        campaign_id=campaign.id,
        company_name=company,
        contact_name=contact,
        contact_role=kw.pop("contact_role", "Practice Manager"),
        email=email,
        email_normalized=normalize_email(email),
        website=website,
        domain=normalize_domain(email),
        company_key=normalize_name(company),
        contact_key=normalize_name(contact),
        status=kw.pop("status", LeadStatus.NEW),
        **kw,
    )
    session.add(lead)
    session.commit()
    return lead


@pytest.fixture
def lead_factory(session: Session, campaign: Campaign):
    def _factory(email: str = "sarah@abcdental.de", **kw) -> Lead:
        return make_lead(session, campaign, email, **kw)
    return _factory
